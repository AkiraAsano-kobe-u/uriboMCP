"""Synchronize LMS BEEF (Moodle) courses into the shared uribo database.

Items are stored with ``source='moodle'`` and ``source_id`` of the form
``<category>/<moodle idnumber>/<key>``.  When exactly one BEEF+ course has the
same year and timetable code, items are attached to that BEEF+ course row so
course search covers both LMSs; a ``source='moodle'`` course row is always
kept as well.

Snapshot rule: a row is only marked unavailable when the page that would list
it was fetched successfully *and recognised* (an HTTP 200 error page is not a
course page).  Any transient failure keeps the previous row untouched (body,
deadline and attachments included).  A known deadline is never blanked just
because no source mentioned it this time (calendar failure, or an event that
moved outside the fetched calendar window).

Only ``MoodleSessionExpired`` aborts the whole source.  Other refusals (for
example a redirect outside the read-only allowlist) fail just that activity or
course and keep its rows.
"""

from __future__ import annotations

import hashlib
import logging
import re
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import date
from pathlib import PurePosixPath
from typing import Any
from urllib.parse import unquote, urlparse

from uribo_mcp.config import Settings
from uribo_mcp.ingest import AttachmentIngestor
from uribo_mcp.models import CourseRecord, ItemRecord
from uribo_mcp.moodle.client import MoodleReadOnlyClient, MoodleSessionExpired
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
SNAPSHOT_CATEGORIES = ("material", "announcement", "assignment", "test", "survey", "attendance")
METADATA_ONLY = frozenset({"test", "survey", "attendance"})
MAX_DISCUSSIONS_PER_FORUM = 30


def attachment_key(owner: str, url: str) -> str:
    """Stable per-file key that survives Moodle's revision bump in the URL."""
    path = re.sub(r"/content/\d+/", "/content/", unquote(urlparse(url).path))
    path = re.sub(r"^.*?/pluginfile\.php/", "", path)
    digest = hashlib.sha256(path.encode("utf-8")).hexdigest()[:16]
    return f"moodle:{owner}:{digest}"


def _is_media(url: str) -> bool:
    return PurePosixPath(urlparse(url).path).suffix.lower() in MEDIA_SUFFIXES


def _file_attachment(owner: str, url: str, name: str | None = None) -> dict[str, Any]:
    return {
        "stable_id": attachment_key(owner, url),
        "name": MoodleReadOnlyClient.file_name_from_url(url) or name or "attachment",
        "url": url,
        "resource_id": owner,
        "metadata": {"lms": "moodle"},
    }


def _month_window(today: date, before: int = 1, after: int = 5) -> list[tuple[int, int]]:
    index = today.year * 12 + today.month - 1
    return [(value // 12, value % 12 + 1) for value in range(index - before, index + after + 1)]


def _prefixes(idnumber: str) -> list[str]:
    return [f"{category}/{idnumber}/" for category in SNAPSHOT_CATEGORIES]


@dataclass
class _Draft:
    """One item to store.  ``complete=False`` means a detail fetch failed."""

    source_id: str
    category: str
    title: str
    url: str | None
    body: str | None = None
    deadline: str | None = None
    published_at: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)
    attachments: list[dict[str, Any]] = field(default_factory=list)
    complete: bool = True


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
        self.repository = repository
        self.client = client
        self.pause = pause or (lambda: None)
        self.today = today or date.today()
        self._calendar_ok = True
        self.attachments = AttachmentIngestor(
            settings=settings, repository=repository, downloader=client, pause=self.pause
        )

    # ----------------------------------------------------------------- public
    def run(self, *, course_ids: list[str] | None = None) -> dict[str, Any]:
        courses = [
            parse_enrolled_course(raw, base_url=self.client.base_url)
            for raw in self.client.enrolled_courses()
        ]
        wanted = set(course_ids or [])
        # scope=beef passes BEEF+ idnumbers (<year><code><4 digits>); match by code.
        wanted |= {
            match.group(1)
            for value in wanted.copy()
            if (match := re.match(r"^\d{4}([0-9][A-Z][0-9A-Z]{3})\d{4}$", value))
        }
        events: list[dict[str, Any]] = []
        calendar_ok = True
        for year, month in _month_window(self.today):
            try:
                events.extend(self.client.calendar_month(year, month))
            except MoodleSessionExpired:
                raise
            except Exception as exc:  # deadlines also come from assign pages
                calendar_ok = False
                LOGGER.warning("Moodle calendar %s-%02d failed: %s", year, month, exc)
        deadlines = calendar_deadlines(events)
        self._calendar_ok = calendar_ok
        results: dict[str, Any] = {}
        for course in courses:
            if wanted and not wanted.intersection(
                {course["idnumber"], str(course["moodle_id"]), course.get("course_code") or ""}
            ):
                continue
            try:
                results[course["idnumber"]] = self.sync_course(course, deadlines=deadlines)
            except MoodleSessionExpired:
                raise  # abort the source, keep every snapshot
            except Exception as exc:
                LOGGER.warning("Moodle course %s failed: %s", course["idnumber"], exc)
                results[course["idnumber"]] = {
                    "status": "error",
                    "error": f"{type(exc).__name__}: {exc}",
                }
        unavailable = [] if wanted else self._retire_unenrolled(courses)
        return {
            "base_url": self.client.base_url,
            "enrolled_courses": len(courses),
            "calendar_events": len(events),
            "calendar_complete": calendar_ok,
            "courses_unavailable": len(unavailable),
            "courses": results,
        }

    def linked_course(self, course: dict[str, Any]) -> dict[str, Any] | None:
        """The single BEEF+ course ``<year><code><4 digits>``; ambiguous -> None."""
        code, year = course.get("course_code"), course.get("academic_year")
        if not code or not year:
            return None
        pattern = re.compile(rf"^{year}{re.escape(code)}\d{{4}}$")
        rows = [
            dict(row)
            for row in self.repository.conn.execute(
                """SELECT id, idnumber, course_name FROM courses
                   WHERE source='beef' AND available=1 AND idnumber LIKE ?""",
                (f"{year}{code}%",),
            )
            if pattern.match(str(row["idnumber"]))
        ]
        if len(rows) != 1:
            if rows:
                LOGGER.warning("Moodle course %s matches %d BEEF+ courses; not linked", code, len(rows))
            return None
        return rows[0]

    def sync_course(self, course: dict[str, Any], *, deadlines: dict[str, dict[str, Any]]) -> dict[str, Any]:
        idnumber = course["idnumber"]
        page = parse_course_page(self.client.fetch_course(course["moodle_id"]), base_url=self.client.base_url)
        self.pause()
        if not page["is_course_page"]:
            raise RuntimeError("Moodle returned a page that is not a course page")
        linked = self.linked_course(course)
        anchor = self.repository.upsert_course(
            CourseRecord(
                source=SOURCE,
                idnumber=idnumber,
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
        counts: dict[str, Any] = {
            "course_id": course_id,
            "linked_to_beefplus": bool(linked),
            "items_created": 0,
            "items_updated": 0,
            "items_kept_after_error": 0,
            "attachments_downloaded": 0,
            "attachments_reused": 0,
            "attachments_failed": 0,
            "media_skipped": 0,
        }
        seen: list[str] = []
        for activity in page["activities"]:
            module = activity["module"]
            if module == "forum":
                drafts, forum_complete = self._forum_drafts(course, activity)
                if not forum_complete:
                    seen.extend(self._existing_forum_ids(idnumber, activity["cmid"]))
            else:
                category = CATEGORY_BY_MODULE.get(module)
                if not category:
                    continue
                drafts = [self._activity_draft(course, activity, category, deadlines)]
            for draft in drafts:
                seen.append(draft.source_id)
                self._store(draft, course_id=course_id, course_name=course_name, idnumber=idnumber, counts=counts)
        # The course page itself loaded, and anything whose detail failed is in
        # ``seen``; scope the snapshot to this Moodle course's own rows only.
        counts["items_unavailable"] = self.repository.mark_items_missing(
            source=SOURCE,
            seen_source_ids=seen,
            categories=list(SNAPSHOT_CATEGORIES),
            source_id_prefixes=_prefixes(idnumber),
        )
        return counts

    # --------------------------------------------------------------- internals
    def _retire_unenrolled(self, courses: list[dict[str, Any]]) -> list[int]:
        missing = self.repository.mark_courses_missing(
            source=SOURCE, seen_idnumbers=[course["idnumber"] for course in courses]
        )
        for course_id in missing:
            row = self.repository.conn.execute(
                "SELECT idnumber FROM courses WHERE id=?", (course_id,)
            ).fetchone()
            if row:
                self.repository.mark_items_missing(
                    source=SOURCE,
                    seen_source_ids=[],
                    source_id_prefixes=_prefixes(str(row["idnumber"])),
                )
        return missing

    def _fallback_deadline(self, source_id: str) -> str | None:
        """Keep a stored deadline only when this run could not have seen it.

        That is: the calendar failed, or the stored date lies outside the
        fetched calendar window.  If the calendar covered that month and no
        longer lists the deadline, the teacher removed it and it is dropped.
        """
        stored = self._stored_deadline(source_id)
        if not stored:
            return None
        if not self._calendar_ok:
            return stored
        try:
            year, month = int(stored[:4]), int(stored[5:7])
        except ValueError:
            return None
        return None if (year, month) in set(_month_window(self.today)) else stored

    def _stored_deadline(self, source_id: str) -> str | None:
        row = self.repository.conn.execute(
            "SELECT deadline FROM items WHERE source=? AND source_id=?", (SOURCE, source_id)
        ).fetchone()
        return row["deadline"] if row else None

    def _existing_forum_ids(self, idnumber: str, forum_cmid: str) -> list[str]:
        # Prefix match in Python: Moodle idnumbers contain "_", a LIKE wildcard.
        prefix = f"announcement/{idnumber}/"
        return [
            str(row["source_id"])
            for row in self.repository.conn.execute(
                """SELECT source_id FROM items
                   WHERE source=? AND available=1
                     AND json_extract(metadata_json, '$.forum_cmid')=?""",
                (SOURCE, str(forum_cmid)),
            )
            if str(row["source_id"]).startswith(prefix)
        ]

    def _activity_draft(
        self,
        course: dict[str, Any],
        activity: dict[str, Any],
        category: str,
        deadlines: dict[str, dict[str, Any]],
    ) -> _Draft:
        cmid, module = activity["cmid"], activity["module"]
        draft = _Draft(
            source_id=f"{category}/{course['idnumber']}/{cmid}",
            category=category,
            title=activity["title"],
            url=activity.get("url") or f"{course['url']}#module-{cmid}",
            body=activity.get("body"),
            metadata={
                "lms": "moodle",
                "module": module,
                "cmid": cmid,
                "moodle_course_id": course["moodle_id"],
                "idnumber": course["idnumber"],
                "section": activity.get("section_name"),
                "file_type": activity.get("file_type"),
            },
            attachments=[
                _file_attachment(cmid, link["url"], link["name"])
                for link in activity.get("inline_files") or []
            ],
        )
        calendar = deadlines.get(cmid) or {}
        if calendar.get("available_from"):
            draft.metadata["available_from"] = calendar["available_from"]
        if calendar.get("expected_completion"):
            draft.metadata["expected_completion"] = calendar["expected_completion"]
        if category in METADATA_ONLY:
            draft.metadata["read_policy"] = "metadata_only"
            draft.deadline = calendar.get("deadline") or self._fallback_deadline(draft.source_id)
            return draft
        detail: dict[str, Any] = {}
        try:
            if module == "resource":
                url = self.client.resolve_resource_file(cmid)
                self.pause()
                if url:
                    draft.attachments.append(_file_attachment(cmid, url, activity.get("title")))
            elif module in DETAIL_MODULES and activity.get("url"):
                detail = parse_activity_page(
                    self.client.fetch_page(str(activity["url"])), base_url=self.client.base_url
                )
                self.pause()
        except MoodleSessionExpired:
            raise
        except Exception as exc:
            LOGGER.warning("Moodle detail fetch failed for cm %s: %s", cmid, exc)
            draft.complete = False
            return draft
        draft.body = detail.get("body") or draft.body
        draft.attachments.extend(
            _file_attachment(cmid, link["url"], link["name"]) for link in detail.get("files") or []
        )
        # Calendar due/close epochs are exact; the page text is the fallback;
        # a previously stored deadline is kept rather than blanked.
        draft.deadline = (
            calendar.get("deadline")
            or detail.get("deadline")
            or self._fallback_deadline(draft.source_id)
        )
        for key in ("submission_status", "dates"):
            if detail.get(key):
                draft.metadata[key] = detail[key]
        return draft

    def _forum_drafts(self, course: dict[str, Any], activity: dict[str, Any]) -> tuple[list[_Draft], bool]:
        """Drafts for a forum; ``False`` when the list may be incomplete."""
        if not activity.get("url"):
            return [], False
        try:
            listing = parse_forum_discussions(
                self.client.fetch_page(str(activity["url"])), base_url=self.client.base_url
            )
            self.pause()
        except MoodleSessionExpired:
            raise
        except Exception as exc:
            LOGGER.warning("Moodle forum %s failed: %s", activity["cmid"], exc)
            return [], False
        discussions = listing["discussions"]
        complete = (
            listing["recognized"]
            and not listing["paged"]
            and len(discussions) <= MAX_DISCUSSIONS_PER_FORUM
        )
        if not listing["recognized"]:
            LOGGER.warning("Moodle forum %s: unrecognised list layout", activity["cmid"])
        drafts: list[_Draft] = []
        for discussion in discussions[:MAX_DISCUSSIONS_PER_FORUM]:
            key = f"d{discussion['discussion_id']}"
            draft = _Draft(
                source_id=f"announcement/{course['idnumber']}/{key}",
                category="announcement",
                title=discussion["title"],
                url=discussion["url"],
                published_at=discussion.get("published_at"),
                metadata={
                    "lms": "moodle",
                    "module": "forum",
                    "forum_cmid": activity["cmid"],
                    "forum": activity["title"],
                    "moodle_course_id": course["moodle_id"],
                    "idnumber": course["idnumber"],
                },
            )
            try:
                detail = parse_forum_discussion(
                    self.client.fetch_page(discussion["url"]), base_url=self.client.base_url
                )
                self.pause()
                if not detail["recognized"]:
                    raise RuntimeError("unrecognised discussion layout")
                draft.body = detail.get("body")
                draft.published_at = detail.get("published_at") or draft.published_at
                draft.attachments = [
                    _file_attachment(key, link["url"], link["name"]) for link in detail.get("files") or []
                ]
            except MoodleSessionExpired:
                raise
            except Exception as exc:
                LOGGER.warning("Moodle discussion %s failed: %s", discussion["discussion_id"], exc)
                draft.complete = False
            drafts.append(draft)
        return drafts, complete

    def _store(
        self,
        draft: _Draft,
        *,
        course_id: int,
        course_name: str,
        idnumber: str,
        counts: dict[str, Any],
    ) -> None:
        if not draft.complete:
            exists = self.repository.conn.execute(
                "SELECT 1 FROM items WHERE source=? AND source_id=?", (SOURCE, draft.source_id)
            ).fetchone()
            counts["items_kept_after_error"] += 1
            if exists:
                return  # keep the previous body, deadline and attachments
        unique = {attachment["stable_id"]: attachment for attachment in draft.attachments}
        downloadable = [a for a in unique.values() if not _is_media(a["url"])]
        skipped = [a["name"] for a in unique.values() if _is_media(a["url"])]
        counts["media_skipped"] += len(skipped)
        metadata = dict(draft.metadata)
        if skipped:
            metadata["media_not_downloaded"] = skipped
        metadata["attachment_manifest"] = [
            {"stable_id": a["stable_id"], "name": a["name"]} for a in unique.values()
        ]
        result = self.repository.upsert_item(
            ItemRecord(
                source=SOURCE,
                source_id=draft.source_id,
                category=draft.category,
                title=draft.title,
                body=draft.body,
                url=draft.url,
                course_name=course_name,
                course_id=course_id,
                published_at=draft.published_at,
                deadline=draft.deadline,
                metadata=metadata,
            )
        )
        if result.created:
            counts["items_created"] += 1
        elif result.changed_fields:
            counts["items_updated"] += 1
        if draft.category in METADATA_ONLY or not draft.complete:
            return
        attachment_counts = self.attachments.sync_item_attachments(
            item_id=result.id,
            course_id=course_id,
            idnumber=idnumber,
            attachments=downloadable,
        )
        counts["attachments_downloaded"] += attachment_counts["downloaded"]
        counts["attachments_reused"] += attachment_counts["reused"]
        counts["attachments_failed"] += attachment_counts["failed"]


__all__ = ["MoodleSynchronizer", "SOURCE", "attachment_key"]
