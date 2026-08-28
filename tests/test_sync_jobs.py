from datetime import UTC, datetime
from pathlib import Path

from uribo_mcp.config import load_settings
from uribo_mcp.db import database
from uribo_mcp.sync import (
    get_refresh_status,
    recover_stale_jobs,
    run_job,
    start_refresh,
)


class FakeProcess:
    pid = 4242


def test_start_refresh_is_nonblocking_and_deduplicates(
    tmp_path: Path, monkeypatch
) -> None:
    settings = load_settings(root=tmp_path)
    monkeypatch.setattr(
        "uribo_mcp.sync.subprocess.Popen", lambda *args, **kwargs: FakeProcess()
    )
    first = start_refresh(settings, scope="courses", course_ids=["C1"])
    assert first["started"]
    assert first["job"]["pid"] == 4242
    second = start_refresh(settings, scope="all")
    assert not second["started"]
    state = get_refresh_status(settings, first["job"]["id"])
    assert state["job"]["status"] == "queued"
    with database(settings.paths.db_path) as connection:
        assert connection.execute("SELECT COUNT(*) FROM sync_jobs").fetchone()[0] == 1


def test_worker_persists_structured_result(tmp_path: Path, monkeypatch) -> None:
    settings = load_settings(root=tmp_path)
    monkeypatch.setattr(
        "uribo_mcp.sync.subprocess.Popen", lambda *args, **kwargs: FakeProcess()
    )
    started = start_refresh(settings, scope="csi")
    monkeypatch.setattr(
        "uribo_mcp.sync.synchronize",
        lambda *args, **kwargs: {"csi": {"status": "ok", "created": 2}},
    )
    run_job(settings, started["job"]["id"])
    state = get_refresh_status(settings, started["job"]["id"])["job"]
    assert state["status"] == "completed"
    assert state["result"]["csi"]["created"] == 2


def test_recover_stale_jobs_uses_sqlite_timestamp_format(
    tmp_path: Path, monkeypatch
) -> None:
    class FixedDateTime(datetime):
        @classmethod
        def now(cls, tz=None):
            return cls(2026, 7, 30, 6, 30, tzinfo=tz or UTC)

    settings = load_settings(root=tmp_path)
    monkeypatch.setattr("uribo_mcp.sync.datetime", FixedDateTime)
    with database(settings.paths.db_path) as connection:
        connection.execute(
            """INSERT INTO sync_jobs
               (id,scope,parameters_json,status,current_step,requested_at)
               VALUES ('fresh','csi','{}','running','syncing','2026-07-30 06:00:00'),
                      ('stale','csi','{}','running','syncing','2026-07-29 23:00:00')"""
        )
        connection.commit()
    assert recover_stale_jobs(settings) == 1
    with database(settings.paths.db_path) as connection:
        rows = {
            row["id"]: row["status"]
            for row in connection.execute(
                "SELECT id,status FROM sync_jobs ORDER BY id"
            ).fetchall()
        }
    assert rows == {"fresh": "running", "stale": "failed"}
