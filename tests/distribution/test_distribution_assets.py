from __future__ import annotations

import json
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]


def test_claude_desktop_example_is_valid_and_has_no_credentials() -> None:
    example = json.loads(
        (ROOT / "packaging" / "claude-desktop" / "claude_desktop_config.example.json").read_text(
            encoding="utf-8"
        )
    )

    server = example["mcpServers"]["uribo-mcp"]
    assert server["command"] == "uv"
    assert server["args"] == ["--directory", server["args"][1], "run", "uribo-mcp", "serve"]
    assert set(server["env"]) == {"URIBO_MCP_CONFIG"}
    assert "password" not in json.dumps(example).lower()
    assert "token" not in json.dumps(example).lower()


def test_codex_example_uses_argument_array_and_config_environment() -> None:
    text = (ROOT / "packaging" / "codex" / "config.toml.example").read_text(encoding="utf-8")

    assert "[mcp_servers.uribo-mcp]" in text
    assert "args = [" in text
    assert "[mcp_servers.uribo-mcp.env]" in text
    assert "URIBO_MCP_CONFIG" in text
    assert "password" not in text.lower()
    assert "token" not in text.lower()


def test_mcpb_manifest_is_valid_and_does_not_embed_credentials() -> None:
    manifest = json.loads((ROOT / "packaging" / "mcpb" / "manifest.json").read_text(encoding="utf-8"))

    assert manifest["manifest_version"] == "0.4"
    assert manifest["server"]["type"] == "uv"
    assert manifest["server"]["entry_point"] == "src/uribo_mcp/__main__.py"
    assert manifest["server"]["mcp_config"]["args"] == [
        "--directory",
        "${__dirname}",
        "run",
        "uribo-mcp",
        "serve",
    ]
    assert manifest["server"]["mcp_config"]["env"] == {
        "URIBO_MCP_CONFIG": "${user_config.config_path}"
    }
    rendered = json.dumps(manifest).lower()
    assert "password" not in rendered
    assert "cookie" not in rendered
    assert "secret" not in rendered


def test_scripts_are_present_and_do_not_modify_user_client_configurations() -> None:
    generator = (ROOT / "scripts" / "New-ClientConfigs.ps1").read_text(encoding="utf-8")
    doctor = (ROOT / "scripts" / "Test-UriboMcpSetup.ps1").read_text(encoding="utf-8")
    bundle = (ROOT / "scripts" / "Build-Mcpb.ps1").read_text(encoding="utf-8")

    assert "ConvertTo-Json" in generator
    assert "GetFullPath" in generator
    assert "URIBO_MCP_CONFIG" in generator
    assert "claude_desktop_config.json" in generator
    assert "codex.toml.fragment" in generator
    assert "exit 1" in doctor
    assert "mcpb" in bundle
    for text in (generator, doctor, bundle):
        assert "%APPDATA%\\Claude" not in text
        assert "claude mcp add" not in text or "register-claude-code-user.ps1" in generator


@pytest.mark.skipif(
    sys.platform != "win32",
    reason="Windows client-material generation",
)
@pytest.mark.parametrize("shell", ["pwsh", "powershell"])
def test_generator_and_doctor_preserve_absolute_unicode_paths(tmp_path: Path, shell: str) -> None:
    # "powershell" is Windows PowerShell 5.1, the only one a stock Windows has.
    powershell = shutil.which(shell)
    if not powershell:
        pytest.skip(f"{shell} is not installed")

    project = tmp_path / "神戸大学 プロジェクト"
    project.mkdir()
    shutil.copy2(ROOT / "pyproject.toml", project / "pyproject.toml")
    config = project / "config.local.toml"
    shutil.copy2(ROOT / "config.example.toml", config)
    generated = tmp_path / "設定 出力"
    generator = ROOT / "scripts" / "New-ClientConfigs.ps1"
    doctor = ROOT / "scripts" / "Test-UriboMcpSetup.ps1"

    subprocess.run(
        [
            powershell,
            "-NoProfile",
            "-ExecutionPolicy",
            "Bypass",
            "-File",
            str(generator),
            "-ProjectRoot",
            str(project),
            "-ConfigPath",
            str(config),
            "-OutputDirectory",
            str(generated),
        ],
        check=True,
        capture_output=True,
    )

    desktop = json.loads((generated / "claude_desktop_config.json").read_text(encoding="utf-8"))
    server = desktop["mcpServers"]["uribo-mcp"]
    assert server["args"][1] == str(project)
    assert server["env"]["URIBO_MCP_CONFIG"] == str(config)
    assert "神戸大学" in server["args"][1]
    assert (generated / "register-claude-code-user.ps1").is_file()
    assert (generated / "register-codex-user.ps1").is_file()

    doctor_result = subprocess.run(
        [
            powershell,
            "-NoProfile",
            "-ExecutionPolicy",
            "Bypass",
            "-File",
            str(doctor),
            "-ProjectRoot",
            str(project),
            "-ConfigPath",
            str(config),
            "-GeneratedDirectory",
            str(generated),
        ],
        capture_output=True,
    )
    # Windows PowerShell 5.1 writes its console output in the OEM code page.
    output = doctor_result.stdout.decode("utf-8", errors="replace")
    assert doctor_result.returncode == 0, output
    assert '"Passed":  true' in output or '"Passed": true' in output


def test_documentation_covers_all_three_clients_and_mcpb() -> None:
    documentation = (ROOT / "docs" / "client-setup.md").read_text(encoding="utf-8")

    for required in (
        "Claude Code",
        "Claude Desktop",
        "Codex",
        "MCPB",
        "URIBO_MCP_CONFIG",
        "--scope user",
        "mcpb pack",
    ):
        assert required in documentation
