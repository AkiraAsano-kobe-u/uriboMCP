"""First-run setup: choose where local data lives and which sources to sync.

The setup writes ``config.local.toml`` with absolute paths. It is driven three
ways that share this module: flags (for an installing agent), terminal prompts,
and the local browser form in :mod:`uribo_mcp.first_run_ui`.
"""

from __future__ import annotations

import json
import os
import shutil
import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from uribo_mcp.errors import ConfigurationError


@dataclass(frozen=True)
class SourceOption:
    id: str
    label: str
    note: str = ""
    default: bool = True
    required: bool = False


# Order is the order shown to the user and written to the config.
SOURCES: tuple[SourceOption, ...] = (
    SourceOption("beef", "BEEF+", "授業資料・課題・お知らせ", required=True),
    SourceOption("uribo", "うりぼーネット", "掲示"),
    SourceOption("csi", "システム情報学部サイト", "在学生向けお知らせ"),
    SourceOption("pdf_calendar", "学年暦", "PDF"),
    SourceOption("newsnet", "神戸大学ニュースネット"),
    SourceOption("venture", "BEEF+ Venture", "特定の1コースのみ対応", default=False),
    SourceOption("moodle", "LMS BEEF", "Moodle", default=False),
    SourceOption("mail", "大学メール", "Google OAuth クライアントが必要", default=False),
)
SOURCE_IDS: tuple[str, ...] = tuple(option.id for option in SOURCES)
DEFAULT_SOURCES: tuple[str, ...] = tuple(option.id for option in SOURCES if option.default)
REQUIRED_SOURCES: frozenset[str] = frozenset(option.id for option in SOURCES if option.required)

CONFIG_NAME = "config.local.toml"


def default_storage_dir() -> Path:
    """A folder in the user profile, outside cloud-synced Documents/Desktop."""
    return Path.home() / "uriboMCP-data"


@dataclass(frozen=True)
class SetupPlan:
    storage_dir: Path
    data_dir: Path
    cache_dir: Path
    export_root: Path
    credentials_dir: Path
    sources: tuple[str, ...]

    def directories(self) -> tuple[Path, ...]:
        return (self.data_dir, self.cache_dir, self.export_root, self.credentials_dir)


def _absolute(raw: str | Path, *, name: str) -> Path:
    text = str(raw).strip().strip('"')
    if not text:
        raise ConfigurationError(f"{name} must not be empty")
    if any(ord(ch) < 32 or ord(ch) == 127 for ch in text):
        raise ConfigurationError(f"{name} contains a control character")
    path = Path(os.path.expandvars(text)).expanduser()
    if not path.is_absolute():
        raise ConfigurationError(f"{name} must be an absolute path: {text}")
    return Path(os.path.normpath(path))


def parse_sources(raw: str | list[str] | tuple[str, ...] | None) -> tuple[str, ...]:
    """Validate source ids, always keep required ones, return them in display order."""
    if raw is None:
        return DEFAULT_SOURCES
    values = raw.split(",") if isinstance(raw, str) else list(raw)
    chosen = {str(value).strip() for value in values if str(value).strip()}
    unknown = sorted(chosen - set(SOURCE_IDS))
    if unknown:
        raise ConfigurationError(
            f"Unknown source(s): {', '.join(unknown)}. Choose from: {', '.join(SOURCE_IDS)}"
        )
    chosen |= REQUIRED_SOURCES
    return tuple(source for source in SOURCE_IDS if source in chosen)


def build_plan(
    storage_dir: str | Path,
    *,
    data_dir: str | Path | None = None,
    cache_dir: str | Path | None = None,
    export_root: str | Path | None = None,
    credentials_dir: str | Path | None = None,
    sources: str | list[str] | tuple[str, ...] | None = None,
) -> SetupPlan:
    """Resolve every location; anything not given lives under ``storage_dir``."""
    storage = _absolute(storage_dir, name="storage_dir")
    data = _absolute(data_dir, name="data_dir") if data_dir else storage / "data"
    return SetupPlan(
        storage_dir=storage,
        data_dir=data,
        cache_dir=_absolute(cache_dir, name="cache_dir") if cache_dir else data / "cache",
        export_root=(
            _absolute(export_root, name="export_root") if export_root else storage / "exports"
        ),
        credentials_dir=(
            _absolute(credentials_dir, name="credentials_dir")
            if credentials_dir
            else storage / "credentials"
        ),
        sources=parse_sources(sources),
    )


def plan_warnings(plan: SetupPlan) -> list[str]:
    """Human-readable cautions that do not block the setup."""
    warnings: list[str] = []
    synced = [
        path
        for path in (plan.data_dir, plan.cache_dir, plan.credentials_dir)
        if any(part.lower().startswith(("onedrive", "dropbox", "google drive")) for part in path.parts)
    ]
    if synced:
        warnings.append(
            "クラウド同期フォルダ内です。データベースが壊れることがあるため、同期されない場所を推奨します"
        )
    if "mail" in plan.sources:
        warnings.append(
            "大学メールの同期には credentials フォルダへの google_client_secret.json 配置と "
            "`uribo-mcp auth google` が必要です"
        )
    return warnings


def _toml_string(value: str) -> str:
    # JSON string escapes are a subset of TOML basic-string escapes.
    return json.dumps(value, ensure_ascii=False)


def _toml_value(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int | float):
        return repr(value)
    if isinstance(value, str):
        return _toml_string(value)
    if isinstance(value, list):
        return "[" + ", ".join(_toml_value(item) for item in value) + "]"
    raise ConfigurationError(f"Unsupported value in existing config: {value!r}")


def render_config(plan: SetupPlan, existing: dict[str, Any] | None = None) -> str:
    """Render the config, keeping unrelated keys of an existing config."""
    data: dict[str, dict[str, Any]] = {}
    for section, values in (existing or {}).items():
        if not isinstance(values, dict):
            raise ConfigurationError(f"[{section}] must be a TOML table")
        data[section] = dict(values)
    data.setdefault("paths", {}).update(
        data_dir=str(plan.data_dir),
        cache_dir=str(plan.cache_dir),
        export_root=str(plan.export_root),
        credentials_dir=str(plan.credentials_dir),
    )
    data.setdefault("sync", {})["enabled_sources"] = list(plan.sources)
    order = ["paths", "sync"] + [name for name in data if name not in {"paths", "sync"}]
    lines = ["# Written by `uribo-mcp setup`. Re-run it to change these values.", ""]
    for section in order:
        lines.append(f"[{section}]")
        lines.extend(f"{key} = {_toml_value(value)}" for key, value in data[section].items())
        lines.append("")
    text = "\n".join(lines)
    tomllib.loads(text)  # never write a file the loader cannot read back
    return text


def read_existing(config_path: Path) -> dict[str, Any] | None:
    if not config_path.is_file():
        return None
    try:
        return tomllib.loads(config_path.read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError) as exc:
        raise ConfigurationError(f"Failed to read config: {config_path}: {exc}") from exc


def plan_from_existing(config_path: Path, root: Path) -> SetupPlan | None:
    """The plan an existing config describes, used to prefill a re-run."""
    existing = read_existing(config_path)
    if existing is None:
        return None
    paths = existing.get("paths", {}) if isinstance(existing.get("paths", {}), dict) else {}
    sync = existing.get("sync", {}) if isinstance(existing.get("sync", {}), dict) else {}

    def resolve(key: str, fallback: str) -> Path:
        path = Path(str(paths.get(key, fallback))).expanduser()
        return Path(os.path.normpath(path if path.is_absolute() else root / path))

    data = resolve("data_dir", "data")
    enabled = sync.get("enabled_sources")
    try:
        sources = parse_sources(enabled if isinstance(enabled, list) else None)
    except ConfigurationError:
        sources = DEFAULT_SOURCES
    return SetupPlan(
        storage_dir=data.parent,
        data_dir=data,
        cache_dir=resolve("cache_dir", str(data / "cache")),
        export_root=resolve("export_root", "exports"),
        credentials_dir=resolve("credentials_dir", "credentials"),
        sources=sources,
    )


def mcp_server_entry(root: Path, config_path: Path) -> dict[str, Any]:
    """The stdio server definition every MCP client needs."""
    return {
        "command": "uv",
        "args": ["--directory", str(root), "run", "uribo-mcp", "serve"],
        "env": {"URIBO_MCP_CONFIG": str(config_path)},
    }


def credentials_stored() -> bool | None:
    """Whether Kobe credentials are in the OS keyring; ``None`` if it is unreadable."""
    from uribo_mcp.auth import PASSWORD_KEY, USERNAME_KEY, KeyringSecretStore

    try:
        store = KeyringSecretStore()
        return bool(store.get(USERNAME_KEY) and store.get(PASSWORD_KEY))
    except Exception:
        return None


def store_credentials(username: str, password: str, secret_store=None) -> None:
    from uribo_mcp.auth import PASSWORD_KEY, USERNAME_KEY, KeyringSecretStore
    from uribo_mcp.errors import AuthenticationRequired

    username = username.strip()
    if not username or not password:
        raise AuthenticationRequired("Username and password must not be empty")
    store = secret_store or KeyringSecretStore()
    store.set(USERNAME_KEY, username)
    store.set(PASSWORD_KEY, password)


def apply_setup(
    plan: SetupPlan,
    *,
    config_path: Path,
    root: Path,
    force: bool = False,
    credentials_present: bool | None = None,
) -> dict[str, Any]:
    """Create the folders and write the config. Never touches credentials."""
    existing = read_existing(config_path)
    if existing is not None and not force:
        raise ConfigurationError(
            f"{config_path} already exists. Re-run with --force to replace it "
            "(the previous file is kept as .bak)."
        )
    text = render_config(plan, existing)
    for directory in (*plan.directories(), plan.data_dir / "logs"):
        directory.mkdir(parents=True, exist_ok=True)
    config_path.parent.mkdir(parents=True, exist_ok=True)
    backup = None
    if existing is not None:
        backup = config_path.with_name(config_path.name + ".bak")
        shutil.copy2(config_path, backup)
    temp = config_path.with_name(config_path.name + ".tmp")
    temp.write_text(text, encoding="utf-8", newline="\n")
    temp.replace(config_path)
    stored = credentials_stored() if credentials_present is None else credentials_present
    next_steps = []
    if not stored:
        next_steps.append(
            "Have the user store their Kobe University login themselves: run "
            "`uv run uribo-mcp setup` (browser form) or `uv run uribo-mcp auth setup` "
            "(terminal). Never ask for the password in chat."
        )
    next_steps += [
        "Run `uv run uribo-mcp doctor`.",
        "Run `uv run uribo-mcp refresh --wait` for the first sync.",
        "Register `mcp_server` with the MCP client.",
    ]
    return {
        "ok": True,
        "config_path": str(config_path),
        "backup_path": str(backup) if backup else None,
        "paths": {
            "data_dir": str(plan.data_dir),
            "cache_dir": str(plan.cache_dir),
            "export_root": str(plan.export_root),
            "credentials_dir": str(plan.credentials_dir),
        },
        "enabled_sources": list(plan.sources),
        "kobe_credentials_stored": stored,
        "warnings": plan_warnings(plan),
        "mcp_server": mcp_server_entry(root, config_path),
        "next_steps": next_steps,
    }


def prompt_plan(initial: SetupPlan, *, input_fn=input, print_fn=print) -> SetupPlan:
    """Terminal prompts; Enter keeps the shown value."""
    storage = input_fn(f"保存先フォルダ [{initial.storage_dir}]: ").strip()
    base = build_plan(storage) if storage else initial
    export = input_fn(f"教材の書き出し先 [{base.export_root}]: ").strip()
    print_fn("同期する情報: " + ", ".join(f"{o.id}（{o.label}）" for o in SOURCES))
    sources = input_fn(f"カンマ区切り [{','.join(initial.sources)}]: ").strip()
    return build_plan(
        base.storage_dir,
        data_dir=base.data_dir,
        cache_dir=base.cache_dir,
        export_root=export or base.export_root,
        credentials_dir=base.credentials_dir,
        sources=sources or initial.sources,
    )


__all__ = [
    "CONFIG_NAME",
    "DEFAULT_SOURCES",
    "SOURCES",
    "SOURCE_IDS",
    "SetupPlan",
    "apply_setup",
    "build_plan",
    "credentials_stored",
    "default_storage_dir",
    "mcp_server_entry",
    "parse_sources",
    "plan_from_existing",
    "plan_warnings",
    "prompt_plan",
    "read_existing",
    "render_config",
    "store_credentials",
]
