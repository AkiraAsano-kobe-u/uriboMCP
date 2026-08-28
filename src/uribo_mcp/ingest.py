"""Pure-ish ingestion services connecting parsers, storage, and read-only clients."""

from __future__ import annotations

import logging
import time
from collections.abc import Callable, Iterable
from pathlib import Path
from typing import Any, Literal
from urllib.parse import parse_qs, urlparse

from uribo_mcp.config import Settings
from uribo_mcp.course.client import BeefReadOnlyClient
from uribo_mcp.course.parser import (
    parse_announcement_detail,
    parse_assignment_detail,
    parse_course_page,
)
from uribo_mcp.documents.extractor import DocumentLimits, extract_document
from uribo_mcp.documents.ocr import TesseractCliBackend
from uribo_mcp.models import AttachmentRecord, CourseRecord, ItemRecord
from uribo_mcp.repository import Repository

LOGGER = logging.getLogger(__name__)
DETAIL_CATEGORIES = {"announcement", "assignment"}
METADATA_ONLY_CATEGORIES = {"test", "survey", "attendance"}
AttachmentSyncOutcome = Literal["downloaded", "reused", "failed"]


def ingest_items(
    repository: Repository,
    items: Iterable[dict[str, Any]],
    *,
    course_lookup: dict[str, int] | None = None,
) -> dict[str, Any]:
    """UPSERT canonical legacy scraper rows and return deterministic counters."""
    course_lookup = course_lookup or {}
    created = updated = unchanged = 0
    seen: list[str] = []
    for item in items:
        data = dict(item)
        idnumber = _idnumber_from_item(data)
        if idnumber and not data.get("course_id"):
            data["course_id"] = course_lookup.get(idnumber)
        result = repository.upsert_item(ItemRecord(**_item_fields(data)))
        seen.append(data["source_id"])
        if result.created:
            created += 1
        elif result.changed_fields:
            updated += 1
        else:
            unchanged += 1
    return {
        "created": created,
        "updated": updated,
        "unchanged": unchanged,
        "seen_source_ids": seen,
    }


def _item_fields(value: dict[str, Any]) -> dict[str, Any]:
    names = {
        "source",
        "source_id",
        "category",
        "title",
        "body",
        "url",
        "course_name",
        "course_id",
        "published_at",
        "deadline",
        "remote_updated_at",
        "metadata",
    }
    return {name: value.get(name) for name in names if name in value}


def _idnumber_from_item(item: dict[str, Any]) -> str | None:
    metadata = item.get("metadata") or {}
    if metadata.get("idnumber"):
        return str(metadata["idnumber"])
    parts = str(item.get("source_id") or "").split("/")
    if len(parts) >= 3 and parts[1] not in {"info", "unknown"}:
        return parts[1]
    query = parse_qs(urlparse(str(item.get("url") or "")).query)
    return (query.get("idnumber") or [None])[0]


class CourseIngestor:
    """Synchronize course pages without invoking test/survey/attendance actions."""

    def __init__(
        self,
        *,
        settings: Settings,
        repository: Repository,
        client: BeefReadOnlyClient,
        pause: Callable[[], None] | None = None,
    ) -> None:
        self.settings = settings
        self.repository = repository
        self.client = client
        self.pause = pause or (
            lambda: time.sleep(self.settings.sync.request_interval_seconds)
        )

    def sync_course(
        self,
        *,
        idnumber: str,
        known_name: str | None = None,
        source: str = "beef",
    ) -> dict[str, Any]:
        html = self.client.fetch_course(idnumber)
        page = parse_course_page(html, idnumber=idnumber, base_url=self.client.base_url)
        parsed_name = str(page.get("title") or "").strip()
        course_name = (
            known_name
            if not parsed_name or parsed_name == idnumber
            else parsed_name
        ) or idnumber
        course_result = self.repository.upsert_course(
            CourseRecord(
                source=source,
                idnumber=idnumber,
                course_name=course_name,
                summary=page.get("overview"),
                source_url=f"{self.client.base_url}/lms/course?idnumber={idnumber}",
            )
        )
        course_id = course_result.id
        counts = {
            "course_id": course_id,
            "items_created": 0,
            "items_updated": 0,
            "attachments_downloaded": 0,
            "attachments_reused": 0,
            "attachments_failed": 0,
        }
        seen: list[str] = []
        for content in page["contents"]:
            category = str(content["category"])
            # Safety invariant: metadata-only activities are never opened.
            if category in DETAIL_CATEGORIES:
                content = self._with_detail(content, idnumber=idnumber)
                self.pause()
            elif category in METADATA_ONLY_CATEGORIES:
                content = {
                    **content,
                    "body": None,
                    "metadata": {
                        **(content.get("metadata") or {}),
                        "read_policy": "metadata_only",
                    },
                }
            item = ItemRecord(
                source=source,
                source_id=str(content["stable_id"]),
                category=category,
                title=str(content["title"]),
                body=content.get("body"),
                url=content.get("url"),
                course_name=course_name,
                course_id=course_id,
                published_at=content.get("published_at"),
                deadline=content.get("deadline") or content.get("available_until"),
                metadata={
                    "idnumber": idnumber,
                    "available_from": content.get("available_from"),
                    "available_until": content.get("available_until"),
                    "submission_status": content.get("submission_status"),
                    "group_title": content.get("group_title"),
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
                        for attachment in content.get("attachments") or []
                    ],
                    **(content.get("metadata") or {}),
                },
            )
            item_result = self.repository.upsert_item(item)
            seen.append(item.source_id)
            if item_result.created:
                counts["items_created"] += 1
            elif item_result.changed_fields:
                counts["items_updated"] += 1
            if category not in METADATA_ONLY_CATEGORIES:
                attachment_counts = self.sync_item_attachments(
                    item_id=item_result.id,
                    course_id=course_id,
                    idnumber=idnumber,
                    attachments=list(content.get("attachments") or []),
                )
                counts["attachments_downloaded"] += attachment_counts["downloaded"]
                counts["attachments_reused"] += attachment_counts["reused"]
                counts["attachments_failed"] += attachment_counts["failed"]
        counts["items_unavailable"] = self.repository.mark_items_missing(
            source=source,
            seen_source_ids=seen,
            course_id=course_id,
            categories=[
                "material",
                "announcement",
                "assignment",
                "test",
                "survey",
                "attendance",
            ],
        )
        return counts

    def sync_item_attachments(
        self,
        *,
        item_id: int,
        course_id: int,
        idnumber: str,
        attachments: list[dict[str, Any]],
    ) -> dict[str, int]:
        counts = {"downloaded": 0, "reused": 0, "failed": 0, "unavailable": 0}
        seen = []
        for attachment in attachments:
            seen.append(str(attachment["stable_id"]))
            try:
                outcome = self._sync_attachment(
                    attachment,
                    item_id=item_id,
                    course_id=course_id,
                    idnumber=idnumber,
                )
                counts[outcome] += 1
                if outcome == "downloaded":
                    self.pause()
            except Exception as exc:  # Continue other course material.
                LOGGER.warning(
                    "attachment sync failed for %s: %s",
                    attachment.get("name"),
                    exc,
                )
                counts["failed"] += 1
                self.pause()
        counts["unavailable"] = self.repository.mark_attachments_missing(
            item_id=item_id,
            seen_stable_keys=seen,
        )
        return counts

    def _with_detail(self, content: dict[str, Any], *, idnumber: str) -> dict[str, Any]:
        url = str(content.get("url") or "")
        try:
            if content["category"] == "assignment" and url:
                query = parse_qs(urlparse(url).query)
                detail = parse_assignment_detail(
                    self.client.fetch_assignment_detail(url),
                    idnumber=idnumber,
                    report_id=(query.get("reportId") or [None])[0],
                    base_url=self.client.base_url,
                )
                return self._merge_content(content, detail)
            if content["category"] == "announcement":
                source_id = str(content["stable_id"]).rsplit("/", 1)[-1]
                detail = parse_announcement_detail(
                    self.client.fetch_announcement_detail(
                        information_id=source_id,
                        idnumber=idnumber,
                        rishunen=(content.get("metadata") or {}).get("rishunen"),
                    ),
                    idnumber=idnumber,
                    info_id=source_id,
                    base_url=self.client.base_url,
                )
                return self._merge_content(content, detail)
        except Exception as exc:
            LOGGER.warning("detail fetch failed for %s: %s", content.get("stable_id"), exc)
        return content

    @staticmethod
    def _merge_content(
        summary: dict[str, Any], detail: dict[str, Any]
    ) -> dict[str, Any]:
        result = dict(summary)
        for name in (
            "body",
            "published_at",
            "deadline",
            "available_from",
            "available_until",
            "submission_status",
        ):
            if detail.get(name) is not None:
                result[name] = detail[name]
        combined = {
            attachment["stable_id"]: attachment
            for attachment in (
                list(summary.get("attachments") or [])
                + list(detail.get("attachments") or [])
            )
        }
        result["attachments"] = list(combined.values())
        return result

    def _sync_attachment(
        self,
        attachment: dict[str, Any],
        *,
        item_id: int,
        course_id: int,
        idnumber: str,
    ) -> AttachmentSyncOutcome:
        scan_status = str(attachment.get("scan_status") or "").casefold()
        if scan_status in {"infected", "unsafe", "ng", "blocked", "error"}:
            LOGGER.warning("blocked unsafe attachment: %s", attachment.get("name"))
            return "failed"
        metadata = {**(attachment.get("metadata") or {}), "idnumber": idnumber}
        record = AttachmentRecord(
            stable_key=str(attachment["stable_id"]),
            file_name=str(attachment["name"]),
            display_name=str(attachment["name"]),
            item_id=item_id,
            course_id=course_id,
            content_id=attachment.get("content_id"),
            resource_id=attachment.get("resource_id"),
            mime_type=attachment.get("mime_type"),
            source_url=attachment.get("url"),
            object_name=attachment.get("object_name"),
            scan_status=attachment.get("scan_status"),
            open_end_at=attachment.get("open_end_at"),
            metadata=metadata,
        )
        saved = self.repository.upsert_attachment(record)
        row = self.repository.conn.execute(
            "SELECT * FROM attachments WHERE id=?", (saved.id,)
        ).fetchone()
        cached_path = self._cached_attachment_path(
            row,
            changed_fields=saved.changed_fields,
        )
        if cached_path is not None:
            return "reused"
        previous_sha = row["sha256"] if row else None
        target_dir = (
            self.settings.paths.cache_dir
            / "attachments"
            / str(course_id)
            / str(saved.id)
        )
        try:
            downloaded = self.client.download_attachment(
                {**attachment, "metadata": metadata},
                destination_dir=target_dir,
                max_bytes=self.settings.documents.max_file_bytes,
            )
            limits = DocumentLimits(
                max_bytes=self.settings.documents.max_file_bytes,
                max_pages=self.settings.documents.max_pdf_pages,
                chunk_chars=self.settings.documents.chunk_chars,
                chunk_overlap_chars=self.settings.documents.chunk_overlap_chars,
                ocr_min_native_chars=self.settings.documents.ocr_min_chars_per_page,
            )
            ocr = (
                TesseractCliBackend(
                    self.settings.documents.tesseract_command,
                    default_language=self.settings.documents.ocr_languages,
                )
                if self.settings.features.ocr
                else None
            )
            result = extract_document(downloaded.path, limits=limits, ocr_backend=ocr)
            if (
                previous_sha
                and previous_sha == result.sha256
                and row
                and row["extract_status"] == "ok"
            ):
                return "downloaded"
            pages = [
                {
                    "page_number": page.number,
                    "text": page.text,
                    "extraction_method": page.source,
                }
                for page in result.pages
            ]
            chunks = [
                {
                    "chunk_index": chunk.index,
                    "page_start": chunk.page_start,
                    "page_end": chunk.page_end,
                    "text": chunk.text,
                }
                for chunk in result.chunks
            ]
            methods = sorted({page.source for page in result.pages})
            self.repository.replace_document_text(
                saved.id,
                pages,
                chunks,
                status=result.status,
                method="+".join(methods) or None,
                sha256=result.sha256,
                size_bytes=downloaded.size_bytes,
                local_path=str(downloaded.path),
                error_message=result.error,
            )
            return (
                "downloaded"
                if result.status in {"ok", "unsupported", "password_required"}
                else "failed"
            )
        except Exception as exc:
            self.repository.replace_document_text(
                saved.id,
                [],
                [],
                status="error",
                method=None,
                error_message=f"{type(exc).__name__}: {exc}",
            )
            raise

    @staticmethod
    def _cached_attachment_path(
        row: Any,
        *,
        changed_fields: tuple[str, ...],
    ) -> Path | None:
        if row is None or not row["local_path"] or not row["sha256"]:
            return None
        changed = set(changed_fields)
        if "object_name" in changed:
            return None
        if not row["object_name"] and changed.intersection(
            {"resource_id", "content_id", "source_url", "file_name"}
        ):
            return None
        path = Path(str(row["local_path"]))
        try:
            if not path.is_file():
                return None
            expected_size = row["size_bytes"]
            if expected_size is not None and path.stat().st_size != int(expected_size):
                return None
        except OSError:
            return None
        return path


__all__ = ["CourseIngestor", "ingest_items"]
