"""Windowless entry point for Windows Task Scheduler refreshes."""

from __future__ import annotations

import argparse
import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from uribo_mcp.config import load_settings
from uribo_mcp.sync import recover_stale_jobs, start_refresh


def _append_log(log_path: Path, event: dict[str, Any]) -> None:
    log_path.parent.mkdir(parents=True, exist_ok=True)
    with log_path.open("a", encoding="utf-8") as stream:
        stream.write(json.dumps(event, ensure_ascii=False, default=str) + "\n")


def run_scheduled_refresh(
    *,
    root: Path,
    config_path: Path | None,
    scope: str,
) -> dict[str, Any]:
    """Start one non-blocking refresh and record the scheduler decision."""
    settings = load_settings(config_path, root=root)
    settings.ensure_directories()
    recover_stale_jobs(settings)
    result = start_refresh(settings, scope=scope)
    event = {
        "timestamp": datetime.now(UTC).isoformat(),
        "scope": scope,
        "status": "started" if result.get("started") else "skipped",
        "reason": result.get("reason"),
        "job_id": (result.get("job") or {}).get("id"),
    }
    _append_log(settings.paths.jobs_log_dir / "scheduler.log", event)
    return result


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Start an uriboMCP refresh without opening a console window."
    )
    parser.add_argument("--root", required=True, type=Path)
    parser.add_argument("--config", type=Path)
    parser.add_argument("--scope", default="all")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    root = args.root.expanduser().resolve()
    config_path = args.config.expanduser().resolve() if args.config else None
    try:
        run_scheduled_refresh(
            root=root,
            config_path=config_path,
            scope=args.scope,
        )
    except Exception as exc:
        _append_log(
            root / "data" / "logs" / "scheduler.log",
            {
                "timestamp": datetime.now(UTC).isoformat(),
                "scope": args.scope,
                "status": "error",
                "error": f"{type(exc).__name__}: {exc}",
            },
        )
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
