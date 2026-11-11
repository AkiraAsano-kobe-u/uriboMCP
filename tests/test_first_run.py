from __future__ import annotations

import json
import tomllib
import urllib.error
import urllib.request
from pathlib import Path, PureWindowsPath

import pytest

from uribo_mcp import first_run
from uribo_mcp.auth import PASSWORD_KEY, USERNAME_KEY
from uribo_mcp.cli import main
from uribo_mcp.config import load_settings
from uribo_mcp.errors import ConfigurationError
from uribo_mcp.first_run_ui import TOKEN_HEADER, SetupSession, setup_url, start_server


class MemoryStore:
    def __init__(self) -> None:
        self.values: dict[str, str] = {}

    def get(self, key: str) -> str | None:
        return self.values.get(key)

    def set(self, key: str, value: str) -> None:
        self.values[key] = value

    def delete(self, key: str) -> None:
        self.values.pop(key, None)


@pytest.fixture(autouse=True)
def _isolated(monkeypatch) -> None:
    # Never read the developer's real keyring or config from a test.
    monkeypatch.delenv("URIBO_MCP_CONFIG", raising=False)
    monkeypatch.setattr(first_run, "credentials_stored", lambda: False)


# ----------------------------------------------------------------- planning
def test_plan_places_everything_under_the_storage_folder(tmp_path: Path) -> None:
    plan = first_run.build_plan(tmp_path / "保存 先")

    assert plan.data_dir == tmp_path / "保存 先" / "data"
    assert plan.cache_dir == plan.data_dir / "cache"
    assert plan.export_root == tmp_path / "保存 先" / "exports"
    assert plan.credentials_dir == tmp_path / "保存 先" / "credentials"
    assert plan.sources == first_run.DEFAULT_SOURCES
    assert "mail" not in plan.sources and "moodle" not in plan.sources


def test_plan_accepts_individual_locations(tmp_path: Path) -> None:
    plan = first_run.build_plan(
        tmp_path / "base",
        cache_dir=tmp_path / "big-disk" / "cache",
        export_root=tmp_path / "exports-elsewhere",
        sources="newsnet, moodle",
    )

    assert plan.data_dir == tmp_path / "base" / "data"
    assert plan.cache_dir == tmp_path / "big-disk" / "cache"
    assert plan.export_root == tmp_path / "exports-elsewhere"
    # BEEF+ is always synced, and the order is the canonical one.
    assert plan.sources == ("beef", "newsnet", "moodle")


@pytest.mark.parametrize("bad", ["", "   ", "relative/folder", "data\x00dir"])
def test_plan_rejects_unusable_storage_paths(bad: str) -> None:
    with pytest.raises(ConfigurationError):
        first_run.build_plan(bad)


def test_unknown_source_is_rejected_with_the_valid_choices() -> None:
    with pytest.raises(ConfigurationError, match="Choose from: beef"):
        first_run.parse_sources("beef,gradebook")


def test_cloud_synced_storage_is_flagged(tmp_path: Path) -> None:
    plan = first_run.build_plan(tmp_path / "OneDrive - Kobe University" / "uribo")

    assert any("クラウド同期" in warning for warning in first_run.plan_warnings(plan))
    assert first_run.plan_warnings(first_run.build_plan(tmp_path / "plain")) == []


def test_rendered_config_round_trips_windows_and_japanese_paths() -> None:
    base = PureWindowsPath(r"C:\Users\学生 太郎\uriboMCP-data")
    plan = first_run.SetupPlan(
        storage_dir=base,
        data_dir=base / "data",
        cache_dir=PureWindowsPath(r"D:\cache \"quoted\""),
        export_root=base / "exports",
        credentials_dir=base / "credentials",
        sources=("beef", "csi"),
    )

    parsed = tomllib.loads(first_run.render_config(plan))

    assert parsed["paths"]["data_dir"] == r"C:\Users\学生 太郎\uriboMCP-data\data"
    assert parsed["paths"]["cache_dir"] == r"D:\cache \"quoted\""
    assert parsed["sync"]["enabled_sources"] == ["beef", "csi"]


# ----------------------------------------------------------------- applying
def test_apply_writes_a_config_the_loader_reads_back(tmp_path: Path) -> None:
    root = tmp_path / "project"
    root.mkdir()
    plan = first_run.build_plan(tmp_path / "store", sources="csi")

    result = first_run.apply_setup(plan, config_path=root / "config.local.toml", root=root)
    settings = load_settings(root=root)

    assert result["ok"] is True
    assert settings.config_path == root / "config.local.toml"
    assert settings.paths.data_dir == (tmp_path / "store" / "data").resolve()
    assert settings.paths.export_root == (tmp_path / "store" / "exports").resolve()
    assert settings.sync.enabled_sources == ("beef", "csi")
    assert (tmp_path / "store" / "data" / "logs").is_dir()
    assert not (root / "data").exists(), "nothing may be created inside the project folder"
    assert result["mcp_server"] == {
        "command": "uv",
        "args": ["--directory", str(root), "run", "uribo-mcp", "serve"],
        "env": {"URIBO_MCP_CONFIG": str(root / "config.local.toml")},
    }
    assert result["kobe_credentials_stored"] is False
    assert any("Never ask for the password" in step for step in result["next_steps"])


def test_existing_config_needs_force_and_keeps_other_settings(tmp_path: Path) -> None:
    config = tmp_path / "config.local.toml"
    config.write_text(
        '[paths]\ndata_dir = "data"\n\n[sync]\nrequest_interval_seconds = 7.5\n'
        'enabled_sources = ["beef"]\n\n[documents]\nmax_pdf_pages = 12\n\n[features]\nocr = false\n',
        encoding="utf-8",
    )
    plan = first_run.build_plan(tmp_path / "new-store")

    with pytest.raises(ConfigurationError, match="--force"):
        first_run.apply_setup(plan, config_path=config, root=tmp_path)
    assert 'data_dir = "data"' in config.read_text(encoding="utf-8")

    result = first_run.apply_setup(plan, config_path=config, root=tmp_path, force=True)
    written = tomllib.loads(config.read_text(encoding="utf-8"))

    assert written["paths"]["data_dir"] == str(tmp_path / "new-store" / "data")
    assert written["sync"]["request_interval_seconds"] == 7.5
    assert written["documents"]["max_pdf_pages"] == 12
    assert written["features"]["ocr"] is False
    backup = Path(result["backup_path"])
    assert 'data_dir = "data"' in backup.read_text(encoding="utf-8")


def test_existing_config_prefills_a_rerun(tmp_path: Path) -> None:
    config = tmp_path / "config.local.toml"
    config.write_text(
        '[paths]\ndata_dir = "data"\nexport_root = "exports"\ncredentials_dir = "credentials"\n'
        '[sync]\nenabled_sources = ["csi", "beef", "mail"]\n',
        encoding="utf-8",
    )

    plan = first_run.plan_from_existing(config, tmp_path)

    assert plan is not None
    assert plan.storage_dir == tmp_path
    assert plan.data_dir == tmp_path / "data"
    assert plan.cache_dir == tmp_path / "data" / "cache"
    assert plan.sources == ("beef", "csi", "mail")
    assert first_run.plan_from_existing(tmp_path / "missing.toml", tmp_path) is None


# ---------------------------------------------------------------------- CLI
def test_cli_setup_from_flags_is_non_interactive(tmp_path: Path, capsys) -> None:
    root = tmp_path / "project"
    root.mkdir()
    store = tmp_path / "data store"

    code = main(
        ["--root", str(root), "setup", "--no-ui", "--storage-dir", str(store), "--sources", "csi"]
    )
    result = json.loads(capsys.readouterr().out)

    assert code == 0
    assert result["paths"]["data_dir"] == str(store / "data")
    assert result["enabled_sources"] == ["beef", "csi"]
    assert (root / "config.local.toml").is_file()

    # A second run must not silently replace the file.
    code = main(["--root", str(root), "setup", "--no-ui", "--storage-dir", str(tmp_path / "other")])
    assert code == 2
    assert "--force" in json.loads(capsys.readouterr().out)["error"]
    assert str(store) in (root / "config.local.toml").read_text(encoding="utf-8")


def test_cli_setup_without_terminal_or_folder_explains_what_is_missing(
    tmp_path: Path, capsys, monkeypatch
) -> None:
    monkeypatch.setattr("sys.stdin.isatty", lambda: False, raising=False)

    code = main(["--root", str(tmp_path), "setup", "--no-ui"])

    assert code == 2
    assert "--storage-dir" in json.loads(capsys.readouterr().out)["error"]
    assert not (tmp_path / "config.local.toml").exists()


def test_terminal_prompts_keep_defaults_on_enter(tmp_path: Path) -> None:
    initial = first_run.build_plan(tmp_path / "default")
    answers = iter([str(tmp_path / "chosen"), "", "csi,moodle"])

    plan = first_run.prompt_plan(initial, input_fn=lambda _prompt: next(answers), print_fn=lambda *_: None)

    assert plan.data_dir == tmp_path / "chosen" / "data"
    assert plan.export_root == tmp_path / "chosen" / "exports"
    assert plan.sources == ("beef", "csi", "moodle")


# ----------------------------------------------------------------- browser UI
@pytest.fixture
def ui(tmp_path: Path):
    root = tmp_path / "project"
    root.mkdir()
    store = MemoryStore()
    session = SetupSession(
        root=root,
        config_path=root / "config.local.toml",
        initial=first_run.build_plan(tmp_path / "store"),
        secret_store=store,
    )
    server = start_server(session)
    try:
        yield session, server, store
    finally:
        server.shutdown()
        server.server_close()


def _request(server, path: str, *, token: str | None = None, body=None, headers=None):
    request = urllib.request.Request(
        f"http://127.0.0.1:{server.server_address[1]}{path}",
        data=json.dumps(body).encode("utf-8") if body is not None else None,
        method="POST" if body is not None else "GET",
    )
    if body is not None:
        request.add_header("Content-Type", "application/json")
    if token:
        request.add_header(TOKEN_HEADER, token)
    for key, value in (headers or {}).items():
        request.add_header(key, value)
    try:
        with urllib.request.urlopen(request, timeout=5) as response:
            return response.status, response.read().decode("utf-8"), dict(response.headers)
    except urllib.error.HTTPError as error:
        return error.code, error.read().decode("utf-8"), dict(error.headers)


def test_ui_requires_the_session_token_and_a_loopback_host(ui) -> None:
    session, server, _store = ui

    assert _request(server, "/")[0] == 403
    assert _request(server, "/?t=wrong")[0] == 403
    assert _request(server, "/api/state")[0] == 403
    assert _request(server, "/api/state", token="wrong")[0] == 403
    assert _request(server, "/api/state", token=session.token, headers={"Host": "evil.example"})[0] == 403
    assert (
        _request(
            server,
            "/api/save",
            token=session.token,
            body={},
            headers={"Origin": "https://evil.example"},
        )[0]
        == 403
    )

    status, page, headers = _request(server, setup_url(server, session).split(str(server.server_address[1]))[1])
    assert status == 200
    assert session.token in page and "__TOKEN__" not in page and "__NONCE__" not in page
    assert "default-src 'none'" in headers["Content-Security-Policy"]
    assert headers["Cache-Control"] == "no-store"


def test_ui_state_lists_defaults_without_secrets(ui, tmp_path: Path) -> None:
    session, server, store = ui
    store.set(USERNAME_KEY, "1234567x")
    store.set(PASSWORD_KEY, "hunter2-secret")

    status, body, _ = _request(server, "/api/state", token=session.token)
    state = json.loads(body)

    assert status == 200
    assert state["storage_dir"] == str(tmp_path / "store")
    assert state["credentials_stored"] is True
    assert "hunter2-secret" not in body and "1234567x" not in body
    by_id = {source["id"]: source for source in state["sources"]}
    assert by_id["beef"]["required"] and by_id["beef"]["checked"]
    assert not by_id["mail"]["checked"]


def test_ui_save_writes_config_and_stores_the_login_only_in_the_keyring(ui, tmp_path: Path) -> None:
    session, server, store = ui
    chosen = tmp_path / "選んだ 保存先"

    status, body, _ = _request(
        server,
        "/api/save",
        token=session.token,
        body={
            "storage_dir": str(chosen),
            "export_root": "",
            "sources": ["csi", "newsnet"],
            "username": " 1234567x ",
            "password": "hunter2-secret",
        },
    )

    assert status == 200, body
    assert "hunter2-secret" not in body
    assert store.get(USERNAME_KEY) == "1234567x"
    assert store.get(PASSWORD_KEY) == "hunter2-secret"
    assert session.done.is_set()
    assert session.result is not None
    assert "hunter2-secret" not in json.dumps(session.result)
    assert session.result["kobe_credentials_stored"] is True
    assert session.result["enabled_sources"] == ["beef", "csi", "newsnet"]
    assert not any("password" in step for step in session.result["next_steps"])
    config = (session.root / "config.local.toml").read_text(encoding="utf-8")
    assert "hunter2-secret" not in config and "1234567x" not in config
    assert tomllib.loads(config)["paths"]["data_dir"] == str(chosen / "data")
    assert (chosen / "credentials").is_dir()


def test_ui_save_needs_a_login_unless_one_is_already_stored(ui, tmp_path: Path) -> None:
    session, server, store = ui
    body = {"storage_dir": str(tmp_path / "store"), "export_root": "", "sources": ["csi"]}

    status, text, _ = _request(server, "/api/save", token=session.token, body=body)
    assert status == 400 and "ログインID" in json.loads(text)["error"]

    status, text, _ = _request(
        server, "/api/save", token=session.token, body={**body, "username": "1234567x"}
    )
    assert status == 400 and "両方" in json.loads(text)["error"]
    assert not session.done.is_set()
    assert not session.config_path.exists()
    assert store.values == {}

    store.set(USERNAME_KEY, "1234567x")
    store.set(PASSWORD_KEY, "already-there")
    status, _text, _ = _request(server, "/api/save", token=session.token, body=body)
    assert status == 200
    assert store.get(PASSWORD_KEY) == "already-there"


def test_ui_save_reports_bad_input_without_finishing(ui) -> None:
    session, server, _store = ui

    status, text, _ = _request(
        server,
        "/api/save",
        token=session.token,
        body={"storage_dir": "relative", "sources": ["csi"], "username": "u", "password": "p"},
    )

    assert status == 400 and "absolute" in json.loads(text)["error"]
    assert not session.done.is_set()
