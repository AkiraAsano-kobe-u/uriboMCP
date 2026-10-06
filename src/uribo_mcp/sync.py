"""Background synchronization jobs shared by all desktop MCP clients."""

from __future__ import annotations

import json
import logging
import os
import subprocess
import sys
import time
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import requests
from filelock import FileLock, Timeout

from uribo_mcp.config import ENV_CONFIG, Settings, load_settings
from uribo_mcp.db import database
from uribo_mcp.errors import AuthenticationRequired
from uribo_mcp.ingest import CourseIngestor, ingest_items
from uribo_mcp.legacy import classifier, dedup, inference
from uribo_mcp.repository import Repository, utc_now
from uribo_mcp.subprocesses import hidden_process_kwargs

LOGGER = logging.getLogger(__name__)
# Sources that sign in through KNOSSOS.  After one of them reports
# AuthenticationRequired, later ones in the same run are skipped so a wrong
# password costs one form submission per run, not one per source.
SSO_SOURCES = frozenset({"beef", "venture", "uribo", "moodle"})
TIMETABLE_FIELDS = (
    "day_of_week",
    "period",
    "course_name",
    "room",
    "quarter",
    "idnumber",
)


def _timetable_values(rows: list[dict[str, Any]]) -> list[tuple[Any, ...]]:
    return sorted(
        (tuple(row.get(field) for field in TIMETABLE_FIELDS) for row in rows),
        key=lambda values: tuple("" if value is None else str(value) for value in values),
    )


def _replace_timetable_if_changed(conn, rows: list[dict[str, Any]]) -> bool:
    existing = [
        {field: row[field] for field in TIMETABLE_FIELDS}
        for row in conn.execute(
            f"SELECT {', '.join(TIMETABLE_FIELDS)} FROM timetable"
        ).fetchall()
    ]
    if _timetable_values(existing) == _timetable_values(rows):
        return False
    conn.execute("DELETE FROM timetable")
    conn.executemany(
        """INSERT INTO timetable
           (day_of_week,period,course_name,room,quarter,idnumber)
           VALUES (:day_of_week,:period,:course_name,:room,:quarter,:idnumber)""",
        rows,
    )
    conn.commit()
    return True


def _beef_item_course(
    item: dict[str, Any],
    info_map: dict[str, str],
) -> str | None:
    if item["category"] == "announcement":
        information_id = str(item["source_id"]).rsplit("/", 1)[-1]
        mapped = info_map.get(information_id)
        if mapped:
            return mapped
    parts = str(item["source_id"]).split("/")
    if len(parts) >= 3 and parts[1] not in {"info", "unknown"}:
        return parts[1]
    return None


def _job_row(conn, job_id: str) -> dict[str, Any] | None:
    row = conn.execute("SELECT * FROM sync_jobs WHERE id=?", (job_id,)).fetchone()
    if not row:
        return None
    value = dict(row)
    value["parameters"] = json.loads(value.pop("parameters_json") or "{}")
    value["result"] = (
        json.loads(value.pop("result_json")) if value.get("result_json") else None
    )
    value.pop("result_json", None)
    return value


def get_refresh_status(settings: Settings, job_id: str | None = None) -> dict[str, Any]:
    with database(settings.paths.db_path, readonly=True) as conn:
        if job_id:
            job = _job_row(conn, job_id)
            return {"found": bool(job), "job": job}
        rows = conn.execute(
            "SELECT id FROM sync_jobs ORDER BY requested_at DESC LIMIT 20"
        ).fetchall()
        return {"jobs": [_job_row(conn, row["id"]) for row in rows]}


def start_refresh(
    settings: Settings,
    *,
    scope: str = "all",
    course_ids: list[str] | None = None,
) -> dict[str, Any]:
    allowed_scopes = {"all", "courses", *settings.sync.enabled_sources}
    if scope not in allowed_scopes:
        raise ValueError(
            f"Unknown refresh scope {scope!r}; choose one of {sorted(allowed_scopes)}"
        )
    parameters = {"course_ids": sorted(set(course_ids or []))}
    with database(settings.paths.db_path) as conn:
        active = conn.execute(
            """SELECT * FROM sync_jobs
               WHERE status IN ('queued','running')
               ORDER BY requested_at DESC LIMIT 1"""
        ).fetchone()
        if active:
            return {
                "started": False,
                "reason": "refresh_already_active",
                "job": _job_row(conn, active["id"]),
            }
        job_id = uuid.uuid4().hex
        conn.execute(
            """INSERT INTO sync_jobs(id, scope, parameters_json, status, current_step)
               VALUES (?, ?, ?, 'queued', 'waiting for worker')""",
            (job_id, scope, json.dumps(parameters, ensure_ascii=False)),
        )
        conn.commit()

    log_path = settings.paths.jobs_log_dir / f"{job_id}.log"
    command = [
        sys.executable,
        "-m",
        "uribo_mcp",
        "_worker",
        "--job-id",
        job_id,
        "--root",
        str(settings.paths.root),
    ]
    if settings.config_path:
        command.extend(["--config", str(settings.config_path)])
    environment = os.environ.copy()
    if settings.config_path:
        environment[ENV_CONFIG] = str(settings.config_path)
    with log_path.open("a", encoding="utf-8") as log:
        process = subprocess.Popen(
            command,
            cwd=settings.paths.root,
            env=environment,
            stdin=subprocess.DEVNULL,
            stdout=log,
            stderr=log,
            close_fds=True,
            **hidden_process_kwargs(),
        )
    with database(settings.paths.db_path) as conn:
        conn.execute("UPDATE sync_jobs SET pid=? WHERE id=?", (process.pid, job_id))
        conn.commit()
        job = _job_row(conn, job_id)
    return {"started": True, "job": job, "log_path": str(log_path)}


def run_job(settings: Settings, job_id: str) -> dict[str, Any]:
    lock = FileLock(str(settings.paths.lock_path))
    with database(settings.paths.db_path) as conn:
        job = _job_row(conn, job_id)
        if not job:
            raise KeyError(f"Unknown sync job: {job_id}")
    try:
        with lock.acquire(timeout=0):
            _set_job(settings, job_id, status="running", progress=0.01, step="starting")
            result = synchronize(
                settings,
                scope=str(job["scope"]),
                course_ids=list(job["parameters"].get("course_ids") or []),
                progress=lambda value, step: _set_job(
                    settings, job_id, progress=value, step=step
                ),
            )
            _set_job(
                settings,
                job_id,
                status="completed",
                progress=1.0,
                step="completed",
                result=result,
                finished=True,
            )
            return result
    except Timeout as exc:
        _set_job(
            settings,
            job_id,
            status="failed",
            step="another sync owns the lock",
            error="Another refresh is already running",
            finished=True,
        )
        raise RuntimeError("Another refresh is already running") from exc
    except Exception as exc:
        LOGGER.exception("sync job %s failed", job_id)
        _set_job(
            settings,
            job_id,
            status="failed",
            step="failed",
            error=f"{type(exc).__name__}: {exc}",
            finished=True,
        )
        raise


def _set_job(
    settings: Settings,
    job_id: str,
    *,
    status: str | None = None,
    progress: float | None = None,
    step: str | None = None,
    error: str | None = None,
    result: dict[str, Any] | None = None,
    finished: bool = False,
) -> None:
    assignments: list[str] = []
    values: list[Any] = []
    if status:
        assignments.append("status=?")
        values.append(status)
        if status == "running":
            assignments.append("started_at=COALESCE(started_at, ?)")
            values.append(utc_now())
    if progress is not None:
        assignments.append("progress=?")
        values.append(max(0.0, min(1.0, progress)))
    if step is not None:
        assignments.append("current_step=?")
        values.append(step)
    if error is not None:
        assignments.append("error_message=?")
        values.append(error[:4000])
    if result is not None:
        assignments.append("result_json=?")
        values.append(json.dumps(result, ensure_ascii=False, default=str))
    if finished:
        assignments.append("finished_at=?")
        values.append(utc_now())
    if not assignments:
        return
    values.append(job_id)
    with database(settings.paths.db_path) as conn:
        conn.execute(
            f"UPDATE sync_jobs SET {', '.join(assignments)} WHERE id=?", values
        )
        conn.commit()


def recover_stale_jobs(settings: Settings) -> int:
    cutoff = (
        datetime.now(UTC) - timedelta(hours=settings.sync.job_stale_hours)
    ).replace(microsecond=0).strftime("%Y-%m-%d %H:%M:%S")
    with database(settings.paths.db_path) as conn:
        cursor = conn.execute(
            """UPDATE sync_jobs
               SET status='failed', finished_at=?, error_message='worker became stale'
               WHERE status IN ('queued','running') AND requested_at<?""",
            (utc_now(), cutoff),
        )
        conn.commit()
        return cursor.rowcount


def synchronize(
    settings: Settings,
    *,
    scope: str = "all",
    course_ids: list[str] | None = None,
    progress=None,
) -> dict[str, Any]:
    """Run selected sources. Source failures are isolated and recorded."""
    progress = progress or (lambda value, step: None)
    requested = (
        list(settings.sync.enabled_sources)
        if scope == "all"
        else ["beef"] if scope == "courses" else [scope]
    )
    # The frequent "beef" schedule covers both LMSs once Moodle is enabled, so
    # course material and deadlines from LMS BEEF (Moodle) stay as fresh as BEEF+.
    if scope == "beef" and "moodle" in settings.sync.enabled_sources:
        requested.append("moodle")
    if scope == "all" and "beef" not in requested:
        requested.append("beef")
    results: dict[str, Any] = {}
    sso_blocked: str | None = None
    for index, source in enumerate(requested):
        progress(index / max(len(requested), 1), f"syncing {source}")
        if sso_blocked and source in SSO_SOURCES:
            reason = f"skipped: KNOSSOS sign-in failed earlier in this run ({sso_blocked})"
            results[source] = {"status": "skipped", "error": reason}
            _log_source(settings, source, "error", {}, reason)
            continue
        try:
            if source == "beef":
                result = _sync_beef(settings, course_ids=course_ids)
            elif source == "venture":
                result = _sync_venture(settings)
            elif source == "csi":
                result = _sync_csi(settings)
            elif source == "pdf_calendar":
                result = _sync_pdf_calendar(settings)
            elif source == "newsnet":
                result = _sync_newsnet(settings)
            elif source == "uribo":
                result = _sync_uribo(settings)
            elif source == "mail":
                result = _sync_mail(settings)
            elif source == "moodle":
                result = _sync_moodle(settings, course_ids=course_ids)
            else:
                raise ValueError(f"Unknown source: {source}")
            results[source] = {"status": "ok", **result}
            _log_source(settings, source, "ok", result)
        except Exception as exc:
            if isinstance(exc, AuthenticationRequired) and source in SSO_SOURCES:
                sso_blocked = source
            LOGGER.exception("source %s failed", source)
            results[source] = {
                "status": "error",
                "error": f"{type(exc).__name__}: {exc}",
            }
            _log_source(settings, source, "error", {}, str(exc))
        if index + 1 < len(requested):
            time.sleep(settings.sync.source_interval_seconds)
    with database(settings.paths.db_path) as conn:
        keywords = _classification_keywords(conn)
        results["classification"] = classifier.apply(conn, keywords=keywords)
        results["dedup"] = dedup.apply(conn)
    progress(0.99, "finalizing")
    return results


def _session_get(url: str):
    return requests.get(
        url,
        timeout=60,
        headers={"User-Agent": "uribo-mcp/0.1 (personal course sync)"},
    )


def _upsert_snapshot(
    settings: Settings,
    *,
    source: str,
    items: list[dict[str, Any]],
    mark_missing: bool = False,
) -> dict[str, Any]:
    with database(settings.paths.db_path) as conn:
        repository = Repository(conn)
        result = ingest_items(repository, items)
        result["unavailable"] = (
            repository.mark_items_missing(
                source=source, seen_source_ids=result["seen_source_ids"]
            )
            if mark_missing
            else 0
        )
        result.pop("seen_source_ids", None)
        return result


def _sync_csi(settings: Settings) -> dict[str, Any]:
    from uribo_mcp.legacy.csi import fetch_html, parse_announcements

    return _upsert_snapshot(
        settings,
        source="csi",
        items=parse_announcements(fetch_html(_session_get)),
        mark_missing=True,
    )


def _sync_newsnet(settings: Settings) -> dict[str, Any]:
    import feedparser

    from uribo_mcp.legacy.newsnet import fetch_feed, parse_feed

    return _upsert_snapshot(
        settings,
        source="newsnet",
        items=parse_feed(fetch_feed(_session_get), feedparser.parse),
    )


def _sync_pdf_calendar(settings: Settings) -> dict[str, Any]:
    import pdfplumber

    from uribo_mcp.legacy.pdf_calendar import current_term, fetch_pdf, parse_calendar

    year, first_half = current_term()
    return _upsert_snapshot(
        settings,
        source="pdf_calendar",
        items=parse_calendar(fetch_pdf(_session_get, year, first_half), pdfplumber.open),
        mark_missing=True,
    )


def _sync_uribo(settings: Settings) -> dict[str, Any]:
    from uribo_mcp.legacy.uribo import parse_keiji_list
    from uribo_mcp.live import fetch_uribo_html

    return _upsert_snapshot(
        settings,
        source="uribo",
        items=parse_keiji_list(fetch_uribo_html(settings)),
        mark_missing=True,
    )


def _sync_mail(settings: Settings) -> dict[str, Any]:
    from datetime import datetime as dt

    from uribo_mcp.legacy.mail import build_query, fetch_messages
    from uribo_mcp.live import gmail_service

    with database(settings.paths.db_path) as conn:
        row = conn.execute(
            "SELECT value FROM user_preferences WHERE key='mail_last_epoch'"
        ).fetchone()
        after = int(row["value"]) if row else None
    items = fetch_messages(
        gmail_service(settings),
        build_query(after),
        pause=lambda: time.sleep(settings.sync.request_interval_seconds),
    )
    result = _upsert_snapshot(settings, source="mail", items=items)
    epochs = []
    for item in items:
        try:
            epochs.append(int(dt.fromisoformat(item["published_at"]).timestamp()))
        except (TypeError, ValueError):
            continue
    if epochs:
        with database(settings.paths.db_path) as conn:
            conn.execute(
                """INSERT INTO user_preferences(key,value,updated_at)
                   VALUES('mail_last_epoch',?,datetime('now'))
                   ON CONFLICT(key) DO UPDATE SET
                     value=excluded.value, updated_at=excluded.updated_at""",
                (str(max(epochs)),),
            )
            conn.commit()
    return result


def _sync_beef(
    settings: Settings, *, course_ids: list[str] | None = None
) -> dict[str, Any]:
    from uribo_mcp.course.parser import (
        parse_announcement_detail,
        parse_assignment_detail,
    )
    from uribo_mcp.legacy.beef import (
        BASE,
        fetch_pages,
        info_idnumbers,
        parse_information_list,
        parse_tasks,
        parse_timetable,
        parse_updateinfo,
    )
    from uribo_mcp.live import authenticated_lms

    urls = {
        "timetable": f"{BASE}/lms/timetable",
        "task": f"{BASE}/lms/task",
        "information": f"{BASE}/lms/course/information/list",
        "updateinfo": f"{BASE}/updateinfo",
    }
    with authenticated_lms(settings, base_url=BASE, source="beef") as (driver, client):
        pages = fetch_pages(
            driver,
            urls,
            logged_in=lambda value: value.current_url.startswith(BASE),
        )
        client.configure_announcement_form(pages["information"])
        timetable = parse_timetable(pages["timetable"])
        tasks = parse_tasks(pages["task"])
        info_map = info_idnumbers(pages["updateinfo"], pages["information"])
        items = (
            tasks
            + parse_information_list(pages["information"], info_map)
            + parse_updateinfo(pages["updateinfo"])
        )
        selected = sorted(
            set(course_ids or [])
            or {str(row["idnumber"]) for row in timetable if row.get("idnumber")}
        )
        selected_courses = set(selected)
        enriched_items = []
        for item in items:
            enriched = dict(item)
            attachments: list[dict[str, Any]] = []
            item_course = _beef_item_course(item, info_map)
            # Course pages are the richer canonical source for their own assignments
            # and announcements. Processing the global-list copy as well causes the
            # same item and attachments to oscillate between two representations.
            if item_course in selected_courses:
                continue
            try:
                if item["category"] == "assignment" and item.get("url"):
                    idnumber = item["source_id"].split("/")[1]
                    report_id = item["source_id"].rsplit("/", 1)[-1]
                    detail = parse_assignment_detail(
                        client.fetch_assignment_detail(item["url"]),
                        idnumber=idnumber,
                        report_id=report_id,
                        base_url=BASE,
                    )
                    enriched["body"] = detail.get("body") or enriched.get("body")
                    enriched["deadline"] = (
                        detail.get("deadline") or enriched.get("deadline")
                    )
                    enriched["metadata"] = {
                        "idnumber": idnumber,
                        "submission_status": detail.get("submission_status"),
                        "available_until": detail.get("available_until"),
                    }
                    attachments = list(detail.get("attachments") or [])
                    time.sleep(settings.sync.request_interval_seconds)
                elif item["category"] == "announcement":
                    information_id = item["source_id"].rsplit("/", 1)[-1]
                    idnumber = info_map.get(information_id)
                    if idnumber:
                        detail = parse_announcement_detail(
                            client.fetch_announcement_detail(
                                information_id=information_id,
                                idnumber=idnumber,
                            ),
                            idnumber=idnumber,
                            info_id=information_id,
                            base_url=BASE,
                        )
                        enriched["body"] = detail.get("body") or enriched.get("body")
                        enriched["published_at"] = (
                            detail.get("published_at")
                            or enriched.get("published_at")
                        )
                        enriched["metadata"] = {"idnumber": idnumber}
                        attachments = list(detail.get("attachments") or [])
                        time.sleep(settings.sync.request_interval_seconds)
            except Exception as exc:
                LOGGER.warning(
                    "BEEF detail fetch failed for %s: %s",
                    item.get("source_id"),
                    exc,
                )
            if attachments:
                enriched["metadata"] = {
                    **(enriched.get("metadata") or {}),
                    "attachment_manifest": [
                        {
                            key: attachment.get(key)
                            for key in (
                                "stable_id",
                                "name",
                                "content_id",
                                "resource_id",
                                "object_name",
                                "scan_status",
                                "open_end_at",
                            )
                        }
                        for attachment in attachments
                    ],
                }
                enriched["_detail_attachments"] = attachments
            enriched_items.append(enriched)
        items = enriched_items
        with database(settings.paths.db_path) as conn:
            repository = Repository(conn)
            _replace_timetable_if_changed(conn, timetable)
            course_lookup: dict[str, int] = {}
            for row in timetable:
                idnumber = row.get("idnumber")
                if not idnumber:
                    continue
                saved = repository.upsert_course(
                    {
                        "source": "beef",
                        "idnumber": idnumber,
                        "course_name": row["course_name"],
                        "term": row.get("quarter"),
                        "schedule_text": f"{row['day_of_week']}曜日 {row['period']}限",
                        "source_url": f"{BASE}/lms/course?idnumber={idnumber}",
                    }
                )
                course_lookup[idnumber] = saved.id
            missing_course_ids = repository.mark_courses_missing(
                source="beef",
                seen_idnumbers=course_lookup,
            )
            for missing_course_id in missing_course_ids:
                repository.mark_items_missing(
                    source="beef",
                    seen_source_ids=[],
                    course_id=missing_course_id,
                )
            legacy_counts = ingest_items(
                repository, items, course_lookup=course_lookup
            )
            inference.apply_task_inference(
                conn,
                [item["source_id"] for item in tasks],
                source="beef",
            )
            ingestor = CourseIngestor(
                settings=settings, repository=repository, client=client
            )
            detail_attachment_counts = {"downloaded": 0, "reused": 0, "failed": 0}
            for item in items:
                attachments = list(item.get("_detail_attachments") or [])
                if not attachments:
                    continue
                row = conn.execute(
                    "SELECT id,course_id FROM items WHERE source=? AND source_id=?",
                    (item["source"], item["source_id"]),
                ).fetchone()
                idnumber = str(
                    (item.get("metadata") or {}).get("idnumber") or ""
                )
                if not row or not row["course_id"] or not idnumber:
                    continue
                attachment_result = ingestor.sync_item_attachments(
                    item_id=row["id"],
                    course_id=row["course_id"],
                    idnumber=idnumber,
                    attachments=attachments,
                )
                detail_attachment_counts["downloaded"] += attachment_result[
                    "downloaded"
                ]
                detail_attachment_counts["reused"] += attachment_result["reused"]
                detail_attachment_counts["failed"] += attachment_result["failed"]
            course_results = {}
            names = {
                str(row["idnumber"]): str(row["course_name"])
                for row in timetable
                if row.get("idnumber")
            }
            for idnumber in selected:
                course_results[idnumber] = ingestor.sync_course(
                    idnumber=idnumber, known_name=names.get(idnumber)
                )
            legacy_counts.pop("seen_source_ids", None)
            return {
                **legacy_counts,
                "timetable_rows": len(timetable),
                "courses_unavailable": len(missing_course_ids),
                "detail_attachments": detail_attachment_counts,
                "courses": course_results,
            }


def _sync_venture(settings: Settings) -> dict[str, Any]:
    from uribo_mcp.legacy.beef import (
        VENTURE_BASE,
        VENTURE_IDNUMBER,
        fetch_pages,
        parse_venture_pages,
    )
    from uribo_mcp.live import authenticated_lms

    urls = {
        "task": f"{VENTURE_BASE}/lms/task",
        "information": f"{VENTURE_BASE}/lms/course/information/list",
        "updateinfo": f"{VENTURE_BASE}/updateinfo",
    }
    landing = f"/lms/course?idnumber={VENTURE_IDNUMBER}"
    with authenticated_lms(
        settings,
        base_url=VENTURE_BASE,
        source="venture",
        landing_path=landing,
    ) as (driver, client):
        pages = fetch_pages(
            driver,
            urls,
            logged_in=lambda value: value.current_url.startswith(VENTURE_BASE),
        )
        client.configure_announcement_form(pages["information"])
        items = parse_venture_pages(pages)["items"]
        with database(settings.paths.db_path) as conn:
            repository = Repository(conn)
            legacy = ingest_items(repository, items)
            course = CourseIngestor(
                settings=settings, repository=repository, client=client
            ).sync_course(idnumber=VENTURE_IDNUMBER, source="venture")
            legacy.pop("seen_source_ids", None)
            return {**legacy, "courses": {VENTURE_IDNUMBER: course}}


def _sync_moodle(
    settings: Settings, *, course_ids: list[str] | None = None
) -> dict[str, Any]:
    from uribo_mcp.live import open_moodle
    from uribo_mcp.moodle.sync import MoodleSynchronizer

    client = open_moodle(settings)
    with database(settings.paths.db_path) as conn:
        return MoodleSynchronizer(
            settings=settings,
            repository=Repository(conn),
            client=client,
            pause=lambda: time.sleep(settings.sync.request_interval_seconds),
        ).run(course_ids=course_ids)


def _classification_keywords(conn) -> list[str]:
    row = conn.execute(
        "SELECT value FROM user_preferences WHERE key='classification_keywords'"
    ).fetchone()
    if not row:
        return []
    try:
        value = json.loads(row["value"])
        return [str(item) for item in value] if isinstance(value, list) else []
    except json.JSONDecodeError:
        return [part.strip() for part in row["value"].split(",") if part.strip()]


def _log_source(
    settings: Settings,
    source: str,
    status: str,
    result: dict[str, Any],
    error: str | None = None,
) -> None:
    new_items = int(result.get("created", 0))
    updated = int(result.get("updated", 0))
    with database(settings.paths.db_path) as conn:
        conn.execute(
            """INSERT INTO scrape_log
               (source,status,new_items,updated_items,error_message,run_at)
               VALUES (?,?,?,?,?,?)""",
            (source, status, new_items, updated, error, utc_now()),
        )
        conn.commit()


def configure_logging(settings: Settings, *, job_id: str | None = None) -> None:
    log_path = settings.paths.jobs_log_dir / f"{job_id or 'server'}.log"
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
        handlers=[
            logging.FileHandler(log_path, encoding="utf-8"),
            logging.StreamHandler(sys.stderr),
        ],
        force=True,
    )


def worker_main(
    *,
    job_id: str,
    config_path: str | Path | None = None,
    root: str | Path | None = None,
) -> int:
    settings = load_settings(config_path, root=Path(root).resolve() if root else None)
    configure_logging(settings, job_id=job_id)
    recover_stale_jobs(settings)
    run_job(settings, job_id)
    return 0


__all__ = [
    "get_refresh_status",
    "recover_stale_jobs",
    "run_job",
    "start_refresh",
    "synchronize",
    "worker_main",
]
