"""Synchronize LMS BEEF (Moodle) courses into the shared uribo database.

Items are stored with ``source='moodle'``.  When a Moodle course has the same
timetable code and year as an existing BEEF+ course, its items are attached to
that BEEF+ course row so ``list_course_content``/``search_course_content`` for
the subject return material from both LMSs.  A ``source='moodle'`` course row
is always kept as the snapshot anchor.
"""

from __future__ import annotations

import hashlib
import logging
import re
from collections.abc import Callable
from datetime import date
from pathlib import PurePosixPath
from typing import Any
from urllib.parse import unquote, urlparse

from uribo_mcp.config import Settings
from uribo_mcp.ingest import CourseIngestor
from uribo_mcp.models import CourseRecord, ItemRecord
from uribo_mcp.moodle.client import MoodleReadOnlyClient
from uribo_mcp.moodle.parser import (
    CATEGORY_BY_MODULE,
    DETAIL_MODULES,
    calendar_deadlines,
    parse_activity_page,
    parse_course_page,
    parse_enrolled_course,
    parse_forum_discussion,
    parse_forum_discussions,
)
from uribo_mcp.repository import Repository

LOGGER = logging.getLogger(__name__)
SOURCE = "moodle"
MEDIA_SUFFIXES = frozenset(
    {".mov", ".mp4", ".m4v", ".avi", ".wmv", ".webm", ".mkv", ".mp3", ".wav", ".m4a", ".aac"}
)
SNAPSHOT_CATEGORIES = ["material", "announcement", "assignment", "test", "survey", "attendance"]
MAX_DISCUSSIONS_PER_FORUM = 30


def attachment_key(cmid: str, url: str) -> str:
    """Stable per-file key that survives Moodle's revision bump in the URL."""
    path = re.sub(r"/content/\d+/", "/content/", unquote(urlparse(url).path))
    path = re.sub(r"^.*?/pluginfile\.php/", "", path)
    digest = hashlib.sha256(path.encode("utf-8")).hexdigest()[:16]
    return f"moodle:{cmid}:{digest}"


def _is_media(url: str) -> bool:
    return PurePosixPath(urlparse(url).path).suffix.lower() in MEDIA_SUFFIXES


def _file_attachment(cmid: str, url: str, name: str | None = None) -> dict[str, Any]:
    file_name = MoodleReadOnlyClient.file_name_from_url(url) or name or "attachment"
    return {
        "stable_id": attachment_key(cmid, url),
        "name": file_name,
        "url": url,
        "resource_id": cmid,
        "metadata": {"lms": "moodle"},
    }


def _month_window(today: date, before: int = 1, after: int = 5) -> list[tuple[int, int]]:
    index = today.year * 12 + today.month - 1
    return [(value // 12, value % 12 + 1) for value in range(index - before, index + after + 1)]


class MoodleSynchronizer:
    def __init__(
        self,
        *,
        settings: Settings,
        repository: Repository,
        client: MoodleReadOnlyClient,
        pause: Callable[[], None] | None = None,
        today: date | None = None,
    ) -> None:
        self.settings = settings
        self.repository = repository
        self.client = client
        self.pause = pause or (lambda: None)
        self.today = today or date.today()
        self.ingestor = CourseIngestor(
            settings=settings, repository=repository, client=client, pause=self.pause  # type: ignore[arg-type]
        )

    # ----------------------------------------------------------------- public
    def run(self, *, course_ids: list[str] | None = None) -> dict[str, Any]:
        self.client.fetch_my_courses()
        courses = [
            parse_enrolled_course(raw, base_url=self.client.base_url)
            for raw in self.client.enrolled_courses()
        ]
        wanted = set(course_ids or [])
        events: list[dict[str, Any]] = []
        for year, month in _month_window(self.today):
            try:
                events.extend(self.client.calendar_month(year, month))
            except Exception as exc:  # deadlines are best-effort
                LOGGER.warning("Moodle calendar %s-%02d failed: %s", year, month, exc)
        deadlines = calendar_deadlines(events)
        results: dict[str, Any] = {}
        seen_idnumbers: list[str] = []
        for course in courses:
            seen_idnumbers.append(course["idnumber"])
            if wanted and not wanted.intersection(
                {course["idnumber"], str(course["moodle_id"]), course.get("course_code") or ""}
            ):
                continue
            try:
                results[course["idnumber"]] = self.sync_course(course, deadlines=deadlines)
            except PermissionError:
                raise  # session expired: abort the whole source, keep snapshot
            except Exception as exc:
                LOGGER.warning("Moodle course %s failed: %s", course["idnumber"], exc)
                results[course["idnumber"]] = {"status": "error", "error": f"{type(exc).__name__}: {exc}"}
        unavailable: list[int] = []
        if not wanted:
            unavailable = self.repository.mark_courses_missing(
                source=SOURCE, seen_idnumbers=seen_idnumbers
            )
            for course_id in unavailable:
                self.repository.mark_items_missing(source=SOURCE, seen_source_ids=[], course_id=course_id)
        return {
            "base_url": self.client.base_url,
            "enrolled_courses": len(courses),
            "calendar_events": len(events),
            "courses_unavailable": len(unavailable),
            "courses": results,
        }

    def linked_course(self, course: dict[str, Any]) -> dict[str, Any] | None:
        code, year = course.get("course_code"), course.get("academic_year")
        if not code or not year:
            return None
        row = self.repository.conn.execute(
            """SELECT id, course_name FROM courses
               WHERE source='beef' AND available=1 AND idnumber LIKE ?
               ORDER BY id LIMIT 1""",
            (f"{year}{code}%",),
        ).fetchone()
        return dict(row) if row else None

    def sync_course(self, course: dict[str, Any], *, deadlines: dict[str, dict[str, Any]]) -> dict[str, Any]:
        html = self.client.fetch_course(course["moodle_id"])
        self.pause()
        page = parse_course_page(html, base_url=self.client.base_url)
        linked = self.linked_course(course)
        anchor = self.repository.upsert_course(
            CourseRecord(
                source=SOURCE,
                idnumber=course["idnumber"],
                course_name=course["course_name"],
                course_code=course.get("course_code"),
                academic_year=course.get("academic_year"),
                term=course.get("term"),
                summary=page.get("summary"),
                source_url=course["url"],
                metadata={
                    "lms": "BEEF (Moodle)",
                    "moodle_course_id": course["moodle_id"],
                    "fullname": course["fullname"],
                    "linked_beef_course_id": linked["id"] if linked else None,
                },
            )
        )
        course_id = linked["id"] if linked else anchor.id
        course_name = linked["course_name"] if linked else course["course_name"]
        counts = {
            "course_id": course_id,
            "linked_to_beefplus": bool(linked),
            "items_created": 0,
            "items_updated": 0,
            "attachments_downloaded": 0,
            "attachments_reused": 0,
            "attachments_failed": 0,
            "media_skipped": 0,
        }
        seen: list[str] = []
        for activity in page["activities"]:
            module = activity["module"]
            if module == "forum":
                for item, attachments in self._forum_items(course, activity):
                    self._store(item, attachments, course_id, course_name, course, counts, seen)
                continue
            category = CATEGORY_BY_MODULE.get(module)
            if not category:
                continue
            item, attachments = self._activity_item(course, activity, category, deadlines)
            self._store(item, attachments, course_id, course_name, course, counts, seen)
        # The course page loaded completely (a session error would have raised),
        # so anything of this course not seen now was removed or hidden.  Items
        # may sit under either course row if the BEEF+ link appeared/disappeared.
        counts["items_unavailable"] = sum(
            self.repository.mark_items_missing(
                source=SOURCE,
                seen_source_ids=seen,
                course_id=candidate,
                categories=SNAPSHOT_CATEGORIES,
            )
            for candidate in {course_id, anchor.id}
        )
        return counts

    # --------------------------------------------------------------- internals
    def _activity_item(
        self,
        course: dict[str, Any],
        activity: dict[str, Any],
        category: str,
        deadlines: dict[str, dict[str, Any]],
    ) -> tuple[dict[str, Any], list[dict[str, Any]]]:
        cmid = activity["cmid"]
        module = activity["module"]
        body = activity.get("body")
        detail: dict[str, Any] = {}
        attachments: list[dict[str, Any]] = [
            _file_attachment(cmid, link["url"], link["name"]) for link in activity.get("inline_files") or []
        ]
        metadata: dict[str, Any] = {
            "lms": "moodle",
            "module": module,
            "cmid": cmid,
            "moodle_course_id": course["moodle_id"],
            "idnumber": course["idnumber"],
            "section": activity.get("section_name"),
            "file_type": activity.get("file_type"),
        }
        if category in {"test", "survey", "attendance"}:
            metadata["read_policy"] = "metadata_only"
        else:
            try:
                if module == "resource":
                    url = self.client.resolve_resource_file(cmid)
                    self.pause()
                    if url:
                        attachments.append(_file_attachment(cmid, url, activity.get("title")))
                elif module in DETAIL_MODULES and activity.get("url"):
                    detail = parse_activity_page(
                        self.client.fetch_page(activity["url"]),
                        module=module,
                        base_url=self.client.base_url,
                    )
                    self.pause()
                    body = detail.get("body") or body
                    attachments.extend(
                        _file_attachment(cmid, link["url"], link["name"]) for link in detail.get("files") or []
                    )
            except PermissionError:
                raise
            except Exception as exc:
                LOGGER.warning("Moodle detail fetch failed for cm %s: %s", cmid, exc)
        dates = deadlines.get(cmid) or {}
        deadline = detail.get("deadline") or dates.get("deadline")
        if detail.get("submission_status"):
            metadata["submission_status"] = detail["submission_status"]
        if dates.get("available_from"):
            metadata["available_from"] = dates["available_from"]
        if detail.get("dates"):
            metadata["dates"] = detail["dates"]
        item = {
            "source": SOURCE,
            "source_id": f"{category}/{course['idnumber']}/{cmid}",
            "category": category,
            "title": activity["title"],
            "body": body,
            "url": activity.get("url") or f"{course['url']}#module-{cmid}",
            "deadline": deadline,
            "metadata": metadata,
        }
        return item, attachments

    def _forum_items(
        self, course: dict[str, Any], activity: dict[str, Any]
    ) -> list[tuple[dict[str, Any], list[dict[str, Any]]]]:
        if not activity.get("url"):
            return []
        try:
            discussions = parse_forum_discussions(
                self.client.fetch_page(activity["url"]), base_url=self.client.base_url
            )[:MAX_DISCUSSIONS_PER_FORUM]
            self.pause()
        except PermissionError:
            raise
        except Exception as exc:
            LOGGER.warning("Moodle forum %s failed: %s", activity["cmid"], exc)
            return []
        out = []
        for discussion in discussions:
            detail: dict[str, Any] = {}
            try:
                detail = parse_forum_discussion(
                    self.client.fetch_page(discussion["url"]), base_url=self.client.base_url
                )
                self.pause()
            except PermissionError:
                raise
            except Exception as exc:
                LOGGER.warning("Moodle discussion %s failed: %s", discussion["discussion_id"], exc)
            key = f"d{discussion['discussion_id']}"
            out.append(
                (
                    {
                        "source": SOURCE,
                        "source_id": f"announcement/{course['idnumber']}/{key}",
                        "category": "announcement",
                        "title": discussion["title"],
                        "body": detail.get("body"),
                        "url": discussion["url"],
                        "published_at": detail.get("published_at") or discussion.get("published_at"),
                        "metadata": {
                            "lms": "moodle",
                            "module": "forum",
                            "forum_cmid": activity["cmid"],
                            "forum": activity["title"],
                            "moodle_course_id": course["moodle_id"],
                            "idnumber": course["idnumber"],
                        },
                    },
                    [_file_attachment(key, link["url"], link["name"]) for link in detail.get("files") or []],
                )
            )
        return out

    def _store(
        self,
        item: dict[str, Any],
        attachments: list[dict[str, Any]],
        course_id: int,
        course_name: str,
        course: dict[str, Any],
        counts: dict[str, Any],
        seen: list[str],
    ) -> None:
        unique = {attachment["stable_id"]: attachment for attachment in attachments}
        downloadable = [a for a in unique.values() if not _is_media(a["url"])]
        skipped = [a["name"] for a in unique.values() if _is_media(a["url"])]
        counts["media_skipped"] += len(skipped)
        metadata = dict(item.get("metadata") or {})
        if skipped:
            metadata["media_not_downloaded"] = skipped
        metadata["attachment_manifest"] = [
            {"stable_id": a["stable_id"], "name": a["name"]} for a in unique.values()
        ]
        record = ItemRecord(
            source=SOURCE,
            source_id=item["source_id"],
            category=item["category"],
            title=item["title"],
            body=item.get("body"),
            url=item.get("url"),
            course_name=course_name,
            course_id=course_id,
            published_at=item.get("published_at"),
            deadline=item.get("deadline"),
            metadata=metadata,
        )
        result = self.repository.upsert_item(record)
        seen.append(record.source_id)
        if result.created:
            counts["items_created"] += 1
        elif result.changed_fields:
            counts["items_updated"] += 1
        status = str(metadata.get("submission_status") or "")
        if record.category == "assignment" and re.search(r"提出済|Submitted for grading", status):
            self.repository.conn.execute(
                "UPDATE items SET is_done=1 WHERE id=? AND is_done=0", (result.id,)
            )
            self.repository.conn.commit()
        if record.category in {"test", "survey", "attendance"}:
            return
        attachment_counts = self.ingestor.sync_item_attachments(
            item_id=result.id,
            course_id=course_id,
            idnumber=course["idnumber"],
            attachments=downloadable,
        )
        counts["attachments_downloaded"] += attachment_counts["downloaded"]
        counts["attachments_reused"] += attachment_counts["reused"]
        counts["attachments_failed"] += attachment_counts["failed"]


__all__ = ["MoodleSynchronizer", "SOURCE", "attachment_key"]
