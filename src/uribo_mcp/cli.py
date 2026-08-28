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
    print(json.dumps(value, ensure_ascii=False, indent=2, default=str))


def doctor(settings) -> dict:
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


__all__ = ["doctor", "main"]
