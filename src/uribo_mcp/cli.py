"""Command-line entry point used by all three MCP desktop clients."""

from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
import time
from pathlib import Path

from uribo_mcp.auth import interactive_setup
from uribo_mcp.config import load_settings
from uribo_mcp.db import database


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="uribo-mcp")
    parser.add_argument("--config", type=Path)
    parser.add_argument("--root", type=Path)
    commands = parser.add_subparsers(dest="command")
    commands.add_parser("serve", help="run the stdio MCP server")

    auth = commands.add_parser("auth", help="configure local credentials")
    auth_commands = auth.add_subparsers(dest="auth_command", required=True)
    auth_commands.add_parser("setup", help="store Kobe credentials in the OS keyring")
    auth_commands.add_parser("google", help="run Google OAuth for Gmail")
    auth_commands.add_parser("status", help="show setup state without secret values")

    setup = commands.add_parser(
        "setup",
        help="choose storage folders and sources, and store the university login",
        description=(
            "Without options a local browser form opens for the storage folder, the "
            "sources and the university login. With --no-ui the same values come from "
            "flags (or terminal prompts) and the login is left to `auth setup`."
        ),
    )
    setup.add_argument("--storage-dir", type=str, help="base folder for all local data")
    setup.add_argument("--data-dir", type=str, help="database and logs (default: STORAGE/data)")
    setup.add_argument("--cache-dir", type=str, help="downloaded files (default: DATA/cache)")
    setup.add_argument("--export-dir", type=str, help="exports (default: STORAGE/exports)")
    setup.add_argument(
        "--credentials-dir", type=str, help="cookies and OAuth files (default: STORAGE/credentials)"
    )
    setup.add_argument("--sources", type=str, help="comma-separated source ids to sync")
    setup.add_argument("--no-ui", action="store_true", help="do not open the browser form")
    setup.add_argument("--no-browser", action="store_true", help="print the form URL only")
    setup.add_argument("--force", action="store_true", help="replace an existing config")
    setup.add_argument("--timeout", type=float, default=900.0, help="seconds to wait for the form")

    refresh = commands.add_parser("refresh", help="start a background refresh")
    refresh.add_argument("--scope", default="all")
    refresh.add_argument("--course-id", action="append", dest="course_ids")
    refresh.add_argument("--wait", action="store_true")

    worker = commands.add_parser("_worker")
    worker.add_argument("--job-id", required=True)
    worker.add_argument("--config", type=Path)
    worker.add_argument("--root", type=Path)

    legacy = commands.add_parser(
        "import-legacy", help="copy data (not credentials) from an old app.db"
    )
    legacy.add_argument("--from", dest="source", required=True, type=Path)

    commands.add_parser("doctor", help="run read-only setup checks")
    return parser


def _settings(args):
    root = args.root.resolve() if getattr(args, "root", None) else None
    return load_settings(getattr(args, "config", None), root=root)


def _print(value) -> None:
    text = json.dumps(value, ensure_ascii=False, indent=2, default=str)
    try:
        text.encode(sys.stdout.encoding or "utf-8")
    except (UnicodeEncodeError, LookupError):
        # A piped Windows console may not be able to show every character;
        # escaped JSON stays valid for whatever reads it.
        text = json.dumps(value, ensure_ascii=True, indent=2, default=str)
    print(text)


def run_setup(args) -> int:
    """Write config.local.toml from flags, terminal prompts or the browser form."""
    from uribo_mcp import first_run
    from uribo_mcp.errors import UriboMcpError

    root = (args.root or Path.cwd()).resolve()
    selected = args.config or os.environ.get("URIBO_MCP_CONFIG")
    if selected:
        config_path = Path(selected).expanduser()
        config_path = config_path if config_path.is_absolute() else root / config_path
    else:
        config_path = root / first_run.CONFIG_NAME
    try:
        previous = first_run.plan_from_existing(config_path, root)
        if args.storage_dir:
            initial = first_run.build_plan(
                args.storage_dir,
                data_dir=args.data_dir,
                cache_dir=args.cache_dir,
                export_root=args.export_dir,
                credentials_dir=args.credentials_dir,
                sources=args.sources or (previous.sources if previous else None),
            )
        else:
            base = previous or first_run.build_plan(first_run.default_storage_dir())
            initial = first_run.build_plan(
                base.storage_dir,
                data_dir=args.data_dir or base.data_dir,
                cache_dir=args.cache_dir or base.cache_dir,
                export_root=args.export_dir or base.export_root,
                credentials_dir=args.credentials_dir or base.credentials_dir,
                sources=args.sources or base.sources,
            )
        if not args.no_ui:
            from uribo_mcp.first_run_ui import run_setup_ui

            result = run_setup_ui(
                root=root,
                config_path=config_path,
                initial=initial,
                open_browser=not args.no_browser,
                timeout=args.timeout,
            )
        else:
            if not args.storage_dir:
                if not sys.stdin.isatty():
                    raise first_run.ConfigurationError(
                        "--no-ui without a terminal needs --storage-dir "
                        f"(for example --storage-dir \"{first_run.default_storage_dir()}\")"
                    )
                initial = first_run.prompt_plan(initial)
            result = first_run.apply_setup(
                initial,
                config_path=config_path,
                root=root,
                force=args.force or (previous is not None and not args.storage_dir),
            )
    except UriboMcpError as exc:
        _print({"ok": False, "error": str(exc)})
        return 2
    _print(result)
    return 0 if result.get("ok") else 1


def doctor(settings) -> dict:
    from uribo_mcp.first_run import credentials_stored
    from uribo_mcp.live import google_token_summary

    browser_candidates = [
        shutil.which("chrome"),
        shutil.which("msedge"),
        Path(os.environ.get("PROGRAMFILES", "")) / "Google/Chrome/Application/chrome.exe",
        Path(os.environ.get("PROGRAMFILES(X86)", ""))
        / "Microsoft/Edge/Application/msedge.exe",
        Path(os.environ.get("LOCALAPPDATA", ""))
        / "Google/Chrome/Application/chrome.exe",
    ]
    browser = next(
        (
            str(candidate)
            for candidate in browser_candidates
            if candidate and Path(candidate).is_file()
        ),
        None,
    )
    with database(settings.paths.db_path) as conn:
        schema_version = conn.execute(
            "SELECT value FROM schema_meta WHERE key='schema_version'"
        ).fetchone()["value"]
    checks = {
        "database": {"ok": settings.paths.db_path.is_file(), "path": settings.paths.db_path},
        "schema_version": {"ok": bool(schema_version), "value": schema_version},
        "python": {
            "ok": sys.version_info >= (3, 11),
            "version": sys.version.split()[0],
            "executable": sys.executable,
        },
        "chrome": {
            "ok": bool(browser),
            "path": browser,
            "note": "Selenium Manager can also locate an installed Chrome/Edge browser.",
        },
        "tesseract": {
            "ok": bool(shutil.which(settings.documents.tesseract_command)),
            "required": False,
            "note": "Only scanned PDFs/images need OCR.",
        },
        "pdftoppm": {
            "ok": bool(shutil.which("pdftoppm")),
            "required": False,
            "note": "Only PDF OCR needs Poppler; native PDF text still works.",
        },
        "google": google_token_summary(settings),
        "config": {
            "ok": settings.config_path is not None,
            "required": False,
            "path": settings.config_path,
            "note": "Run `uribo-mcp setup` to choose where data is stored.",
        },
        "kobe_credentials": {
            "ok": bool(credentials_stored()),
            "required": False,
            "note": "Stored by `uribo-mcp setup` or `uribo-mcp auth setup`; values are never shown.",
        },
    }
    checks["ok"] = all(
        value.get("ok", True)
        for value in checks.values()
        if isinstance(value, dict) and value.get("required", True)
    )
    return checks


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    # No subcommand is the MCP client-friendly default.
    if args.command in {None, "serve"}:
        from uribo_mcp.server import run_server

        run_server(_settings(args))
        return 0
    if args.command == "_worker":
        from uribo_mcp.sync import worker_main

        return worker_main(
            job_id=args.job_id,
            config_path=args.config,
            root=args.root,
        )
    if args.command == "setup":
        return run_setup(args)
    settings = _settings(args)
    if args.command == "auth":
        if args.auth_command == "setup":
            interactive_setup()
            _print({"ok": True, "stored_in": "OS keyring"})
        elif args.auth_command == "google":
            from uribo_mcp.live import gmail_service

            gmail_service(settings, interactive=True)
            _print({"ok": True, "service": "gmail"})
        else:
            from uribo_mcp.live import google_token_summary

            _print(
                {
                    "credentials_directory": settings.paths.credentials_dir,
                    "google": google_token_summary(settings),
                    "kobe_credentials": "stored values are intentionally not displayed",
                }
            )
        return 0
    if args.command == "refresh":
        from uribo_mcp.sync import get_refresh_status, start_refresh

        started = start_refresh(
            settings, scope=args.scope, course_ids=args.course_ids
        )
        _print(started)
        if args.wait and started.get("started"):
            job_id = started["job"]["id"]
            while True:
                state = get_refresh_status(settings, job_id)["job"]
                if state["status"] in {"completed", "failed"}:
                    _print(state)
                    return 0 if state["status"] == "completed" else 1
                time.sleep(2)
        return 0
    if args.command == "import-legacy":
        from uribo_mcp.migration import import_legacy_database

        _print(import_legacy_database(args.source, settings.paths.db_path))
        return 0
    if args.command == "doctor":
        result = doctor(settings)
        _print(result)
        return 0 if result["ok"] else 1
    return 2


__all__ = ["doctor", "main", "run_setup"]
