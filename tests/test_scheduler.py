from pathlib import Path

from uribo_mcp import scheduler
from uribo_mcp.config import load_settings


def test_scheduled_refresh_starts_job_and_writes_log(
    tmp_path: Path, monkeypatch
) -> None:
    settings = load_settings(root=tmp_path)
    recovered: list[Path] = []

    monkeypatch.setattr(scheduler, "load_settings", lambda *_args, **_kwargs: settings)
    monkeypatch.setattr(
        scheduler,
        "recover_stale_jobs",
        lambda value: recovered.append(value.paths.root),
    )
    monkeypatch.setattr(
        scheduler,
        "start_refresh",
        lambda _settings, *, scope: {
            "started": True,
            "job": {"id": "scheduled-job"},
            "scope": scope,
        },
    )

    result = scheduler.run_scheduled_refresh(
        root=tmp_path,
        config_path=None,
        scope="beef",
    )

    assert result["started"] is True
    assert recovered == [tmp_path.resolve()]
    log = (settings.paths.jobs_log_dir / "scheduler.log").read_text(encoding="utf-8")
    assert '"scope": "beef"' in log
    assert '"status": "started"' in log
    assert '"job_id": "scheduled-job"' in log
