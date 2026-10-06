"""Persistence and query layer shared by sync jobs and MCP tools."""

from __future__ import annotations

import hashlib
import json
import re
import shutil
import sqlite3
import unicodedata
from collections.abc import Iterable, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from uribo_mcp.errors import ContentNotFound, ExportPathError
from uribo_mcp.models import AttachmentRecord, CourseRecord, ItemRecord, UpsertResult

TRACKED_ITEM_FIELDS = (
    "category",
    "title",
    "body",
    "url",
    "course_name",
    "course_id",
    "published_at",
    "deadline",
    "remote_updated_at",
    "metadata_json",
    "available",
)

NOTIFICATION_CATEGORIES = {"material", "announcement", "assignment", "test", "survey"}


def utc_now() -> str:
    return datetime.now(UTC).replace(microsecond=0).isoformat()


def canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def content_hash(value: Any) -> str:
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


def normalize_search(text: str | None) -> str:
    if not text:
        return ""
    value = unicodedata.normalize("NFKC", text).casefold()
    return re.sub(r"\s+", " ", value).strip()


def _row_dict(row: sqlite3.Row | None) -> dict[str, Any] | None:
    return dict(row) if row is not None else None


def _notification_kind(category: str, created: bool, changed: Sequence[str]) -> str | None:
    if category not in NOTIFICATION_CATEGORIES:
        return None
    if created:
        return f"new_{category}"
    if "deadline" in changed:
        return "deadline_changed"
    if "available" in changed:
        return "availability_changed"
    return f"updated_{category}"


class Repository:
    def __init__(self, conn: sqlite3.Connection):
        self.conn = conn

    # ------------------------------------------------------------------ writes

    def upsert_course(self, course: CourseRecord | dict[str, Any]) -> UpsertResult:
        data = course.to_dict() if isinstance(course, CourseRecord) else dict(course)
        now = utc_now()
        metadata = data.get("metadata") or {}
        payload = {
            "source": data["source"],
            "idnumber": data["idnumber"],
            "course_code": data.get("course_code"),
            "course_name": data["course_name"],
            "academic_year": data.get("academic_year"),
            "term": data.get("term"),
            "instructors_json": canonical_json(data.get("instructors") or []),
            "schedule_text": data.get("schedule_text"),
            "summary": data.get("summary"),
            "source_url": data.get("source_url"),
            "metadata_json": canonical_json(metadata),
            "available": 1,
        }
        payload["content_hash"] = content_hash(payload)
        existing = self.conn.execute(
            "SELECT * FROM courses WHERE source=? AND idnumber=?",
            (payload["source"], payload["idnumber"]),
        ).fetchone()
        if existing is None:
            columns = (*payload.keys(), "first_seen_at", "last_seen_at", "created_at", "updated_at")
            values = (*payload.values(), now, now, now, now)
            sql = (
                f"INSERT INTO courses ({', '.join(columns)}) "
                f"VALUES ({', '.join('?' for _ in columns)})"
            )
            cur = self.conn.execute(sql, values)
            self.conn.commit()
            return UpsertResult(id=int(cur.lastrowid), created=True)

        changed = tuple(
            key for key, value in payload.items() if key not in {"source", "idnumber"}
            and existing[key] != value
        )
        if not changed:
            return UpsertResult(id=int(existing["id"]), created=False)
        assignments = ", ".join(f"{key}=?" for key in payload if key not in {"source", "idnumber"})
        update_values = [
            payload[key] for key in payload if key not in {"source", "idnumber"}
        ]
        self.conn.execute(
            f"UPDATE courses SET {assignments}, last_seen_at=?, updated_at=? WHERE id=?",
            (*update_values, now, now if changed else existing["updated_at"], existing["id"]),
        )
        self.conn.commit()
        return UpsertResult(id=int(existing["id"]), created=False, changed_fields=changed)

    def upsert_item(self, item: ItemRecord | dict[str, Any]) -> UpsertResult:
        data = item.to_dict() if isinstance(item, ItemRecord) else dict(item)
        now = utc_now()
        payload = {
            "source": data["source"],
            "source_id": data["source_id"],
            "category": data["category"],
            "title": data["title"],
            "body": data.get("body"),
            "url": data.get("url"),
            "course_name": data.get("course_name"),
            "course_id": data.get("course_id"),
            "published_at": data.get("published_at"),
            "deadline": data.get("deadline"),
            "remote_updated_at": data.get("remote_updated_at"),
            "metadata_json": canonical_json(data.get("metadata") or {}),
            "available": 1,
        }
        payload["content_hash"] = content_hash(
            {key: payload[key] for key in TRACKED_ITEM_FIELDS}
        )
        existing = self.conn.execute(
            "SELECT * FROM items WHERE source=? AND source_id=?",
            (payload["source"], payload["source_id"]),
        ).fetchone()
        if existing is None:
            columns = (*payload.keys(), "first_seen_at", "last_seen_at", "created_at", "updated_at")
            values = (*payload.values(), now, now, now, now)
            sql = (
                f"INSERT INTO items ({', '.join(columns)}) "
                f"VALUES ({', '.join('?' for _ in columns)})"
            )
            cur = self.conn.execute(sql, values)
            item_id = int(cur.lastrowid)
            revision_id = self._record_revision(
                item_id,
                payload["source"],
                payload["source_id"],
                "created",
                tuple(TRACKED_ITEM_FIELDS),
                None,
                payload,
                category=payload["category"],
            )
            self.conn.commit()
            return UpsertResult(id=item_id, created=True, revision_id=revision_id)

        changed = tuple(
            key for key in TRACKED_ITEM_FIELDS if existing[key] != payload[key]
        )
        if not changed:
            return UpsertResult(id=int(existing["id"]), created=False)
        assignments = ", ".join(
            f"{key}=?" for key in payload if key not in {"source", "source_id"}
        )
        update_values = [
            payload[key] for key in payload if key not in {"source", "source_id"}
        ]
        self.conn.execute(
            f"UPDATE items SET {assignments}, last_seen_at=?, updated_at=? WHERE id=?",
            (*update_values, now, now if changed else existing["updated_at"], existing["id"]),
        )
        revision_id = None
        if changed:
            revision_id = self._record_revision(
                int(existing["id"]),
                payload["source"],
                payload["source_id"],
                "updated",
                changed,
                {key: existing[key] for key in changed},
                {key: payload[key] for key in changed},
                category=payload["category"],
            )
        self.conn.commit()
        return UpsertResult(
            id=int(existing["id"]),
            created=False,
            changed_fields=changed,
            revision_id=revision_id,
        )

    def mark_courses_missing(
        self, *, source: str, seen_idnumbers: Iterable[str]
    ) -> list[int]:
        seen = set(seen_idnumbers)
        rows = self.conn.execute(
            "SELECT id,idnumber FROM courses WHERE source=? AND available=1",
            (source,),
        ).fetchall()
        missing = [int(row["id"]) for row in rows if row["idnumber"] not in seen]
        if missing:
            now = utc_now()
            self.conn.execute(
                f"UPDATE courses SET available=0,updated_at=? "
                f"WHERE id IN ({','.join('?' for _ in missing)})",
                (now, *missing),
            )
            self.conn.commit()
        return missing

    def mark_items_missing(
        self,
        *,
        source: str,
        seen_source_ids: Iterable[str],
        course_id: int | None = None,
        categories: Sequence[str] | None = None,
        source_id_prefixes: Sequence[str] | None = None,
    ) -> int:
        """Mark unseen rows unavailable only after a successful complete snapshot.

        ``source_id_prefixes`` narrows the snapshot to rows whose ``source_id``
        starts with one of the prefixes (plain string match, no LIKE wildcards),
        so one remote container cannot hide rows owned by another container that
        shares the same local ``course_id``.
        """
        seen = set(seen_source_ids)
        prefixes = tuple(source_id_prefixes or ())
        where = ["source=?", "available=1"]
        params: list[Any] = [source]
        if course_id is not None:
            where.append("course_id=?")
            params.append(course_id)
        if categories:
            where.append(f"category IN ({','.join('?' for _ in categories)})")
            params.extend(categories)
        rows = self.conn.execute(
            f"SELECT * FROM items WHERE {' AND '.join(where)}", params
        ).fetchall()
        changed = 0
        now = utc_now()
        for row in rows:
            if row["source_id"] in seen:
                continue
            if prefixes and not str(row["source_id"]).startswith(prefixes):
                continue
            self.conn.execute(
                "UPDATE items SET available=0, updated_at=? WHERE id=?",
                (now, row["id"]),
            )
            self._record_revision(
                int(row["id"]),
                row["source"],
                row["source_id"],
                "unavailable",
                ("available",),
                {"available": 1},
                {"available": 0},
                category=row["category"],
            )
            changed += 1
        self.conn.commit()
        return changed

    def upsert_attachment(
        self, attachment: AttachmentRecord | dict[str, Any]
    ) -> UpsertResult:
        data = (
            attachment.to_dict()
            if isinstance(attachment, AttachmentRecord)
            else dict(attachment)
        )
        if data.get("course_id") is None:
            raise ValueError("attachment.course_id is required for a stable identity")
        now = utc_now()
        payload = {
            "item_id": data.get("item_id"),
            "course_id": data["course_id"],
            "stable_key": data["stable_key"],
            "content_id": data.get("content_id"),
            "resource_id": data.get("resource_id"),
            "display_name": data.get("display_name"),
            "file_name": data["file_name"],
            "mime_type": data.get("mime_type"),
            "source_url": data.get("source_url"),
            "object_name": data.get("object_name"),
            "scan_status": data.get("scan_status"),
            "open_end_at": data.get("open_end_at"),
            "metadata_json": canonical_json(data.get("metadata") or {}),
            "available": 1,
        }
        existing = self.conn.execute(
            "SELECT * FROM attachments WHERE course_id=? AND stable_key=?",
            (payload["course_id"], payload["stable_key"]),
        ).fetchone()
        if existing is None:
            columns = (*payload.keys(), "last_seen_at", "created_at", "updated_at")
            values = (*payload.values(), now, now, now)
            cur = self.conn.execute(
                f"INSERT INTO attachments ({', '.join(columns)}) "
                f"VALUES ({', '.join('?' for _ in columns)})",
                values,
            )
            self.conn.commit()
            return UpsertResult(id=int(cur.lastrowid), created=True)

        changed = tuple(key for key, value in payload.items() if existing[key] != value)
        if not changed:
            return UpsertResult(id=int(existing["id"]), created=False)
        assignments = ", ".join(
            f"{key}=?" for key in payload if key not in {"course_id", "stable_key"}
        )
        values = [
            payload[key] for key in payload if key not in {"course_id", "stable_key"}
        ]
        self.conn.execute(
            f"UPDATE attachments SET {assignments}, last_seen_at=?, updated_at=? WHERE id=?",
            (*values, now, now if changed else existing["updated_at"], existing["id"]),
        )
        self.conn.commit()
        return UpsertResult(
            id=int(existing["id"]), created=False, changed_fields=changed
        )

    def mark_attachments_missing(
        self,
        *,
        item_id: int,
        seen_stable_keys: Iterable[str],
        course_id: int | None = None,
    ) -> int:
        """Hide unseen attachments of an item.

        With ``course_id`` given, rows of the item stored under another course
        (left behind when the item moved between courses) are hidden as well,
        so the same file is not searchable twice.
        """
        seen = set(seen_stable_keys)
        rows = self.conn.execute(
            "SELECT id,stable_key,course_id FROM attachments WHERE item_id=? AND available=1",
            (item_id,),
        ).fetchall()
        changed = 0
        now = utc_now()
        for row in rows:
            moved = course_id is not None and row["course_id"] != course_id
            if row["stable_key"] in seen and not moved:
                continue
            self.conn.execute(
                "UPDATE attachments SET available=0,updated_at=? WHERE id=?",
                (now, row["id"]),
            )
            changed += 1
        self.conn.commit()
        return changed

    def replace_document_text(
        self,
        attachment_id: int,
        pages: Sequence[dict[str, Any]],
        chunks: Sequence[dict[str, Any]],
        *,
        status: str,
        method: str | None,
        sha256: str | None = None,
        size_bytes: int | None = None,
        local_path: str | None = None,
        error_message: str | None = None,
    ) -> None:
        now = utc_now()
        with self.conn:
            self.conn.execute(
                "DELETE FROM document_pages WHERE attachment_id=?", (attachment_id,)
            )
            self.conn.execute(
                "DELETE FROM document_chunks WHERE attachment_id=?", (attachment_id,)
            )
            self.conn.executemany(
                """INSERT INTO document_pages
                   (attachment_id, page_number, text, extraction_method, char_count)
                   VALUES (?, ?, ?, ?, ?)""",
                [
                    (
                        attachment_id,
                        int(page["page_number"]),
                        str(page.get("text") or ""),
                        str(page.get("extraction_method") or method or "unknown"),
                        len(str(page.get("text") or "")),
                    )
                    for page in pages
                ],
            )
            self.conn.executemany(
                """INSERT INTO document_chunks
                   (attachment_id, chunk_index, page_start, page_end, text, search_text)
                   VALUES (?, ?, ?, ?, ?, ?)""",
                [
                    (
                        attachment_id,
                        int(chunk["chunk_index"]),
                        int(chunk["page_start"]),
                        int(chunk["page_end"]),
                        str(chunk.get("text") or ""),
                        normalize_search(str(chunk.get("text") or "")),
                    )
                    for chunk in chunks
                ],
            )
            self.conn.execute(
                """UPDATE attachments
                   SET extract_status=?, extract_method=?, extracted_at=?,
                       sha256=COALESCE(?, sha256), size_bytes=COALESCE(?, size_bytes),
                       local_path=COALESCE(?, local_path), error_message=?, updated_at=?
                   WHERE id=?""",
                (
                    status,
                    method,
                    now,
                    sha256,
                    size_bytes,
                    local_path,
                    error_message,
                    now,
                    attachment_id,
                ),
            )

    def _record_revision(
        self,
        item_id: int,
        source: str,
        source_id: str,
        change_type: str,
        changed_fields: Sequence[str],
        before: dict[str, Any] | None,
        after: dict[str, Any] | None,
        *,
        category: str,
    ) -> int:
        cur = self.conn.execute(
            """INSERT INTO item_revisions
               (item_id, source, source_id, change_type, changed_fields_json,
                before_json, after_json)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (
                item_id,
                source,
                source_id,
                change_type,
                canonical_json(list(changed_fields)),
                canonical_json(before) if before is not None else None,
                canonical_json(after) if after is not None else None,
            ),
        )
        revision_id = int(cur.lastrowid)
        kind = _notification_kind(category, change_type == "created", changed_fields)
        if kind:
            title = (after or before or {}).get("title") or source_id
            course_name = (after or before or {}).get("course_name")
            message = f"{course_name}: {title}" if course_name else str(title)
            importance = "action_required" if category in {"assignment", "test"} else "relevant"
            self.conn.execute(
                """INSERT INTO notifications
                   (revision_id, item_id, kind, title, message, importance)
                   VALUES (?, ?, ?, ?, ?, ?)""",
                (revision_id, item_id, kind, str(title), message, importance),
            )
        return revision_id

    # ------------------------------------------------------------------- reads

    def list_courses(self, *, available_only: bool = True) -> list[dict[str, Any]]:
        sql = "SELECT * FROM courses"
        if available_only:
            sql += " WHERE available=1"
        sql += " ORDER BY academic_year DESC, term, course_name"
        return [self._public_course(row) for row in self.conn.execute(sql)]

    def find_course(self, query: str | int) -> dict[str, Any] | None:
        if isinstance(query, int) or str(query).isdigit():
            row = self.conn.execute("SELECT * FROM courses WHERE id=?", (int(query),)).fetchone()
            return self._public_course(row) if row else None
        exact = self.conn.execute(
            "SELECT * FROM courses WHERE idnumber=? OR course_name=? "
            "ORDER BY available DESC, (source='moodle') ASC, id LIMIT 1",
            (str(query), str(query)),
        ).fetchone()
        if exact:
            return self._linked_course(exact)
        needle = normalize_search(str(query))
        for row in self.conn.execute(
            "SELECT * FROM courses ORDER BY available DESC, (source='moodle') ASC, "
            "academic_year DESC, id"
        ):
            if needle in normalize_search(f"{row['course_code'] or ''} {row['course_name']}"):
                return self._linked_course(row)
        return None

    def _linked_course(self, row: sqlite3.Row) -> dict[str, Any]:
        """A Moodle anchor row linked to BEEF+ resolves to the row holding its items."""
        course = self._public_course(row)
        linked = (course.get("metadata") or {}).get("linked_beef_course_id")
        if row["source"] == "moodle" and linked:
            target = self.conn.execute(
                "SELECT * FROM courses WHERE id=? AND available=1", (int(linked),)
            ).fetchone()
            if target:
                return self._public_course(target)
        return course

    def list_course_content(
        self,
        course_id: int,
        *,
        kinds: Sequence[str] | None = None,
        available_only: bool = True,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        where = ["i.course_id=?"]
        params: list[Any] = [course_id]
        if available_only:
            where.append("i.available=1")
        if kinds:
            where.append(f"i.category IN ({','.join('?' for _ in kinds)})")
            params.extend(kinds)
        params.append(limit)
        rows = self.conn.execute(
            f"""SELECT i.*, COUNT(a.id) AS attachment_count
                FROM items i
                LEFT JOIN attachments a ON a.item_id=i.id AND a.available=1
                WHERE {' AND '.join(where)}
                GROUP BY i.id
                ORDER BY COALESCE(i.deadline, i.published_at, i.updated_at) DESC
                LIMIT ?""",
            params,
        ).fetchall()
        return [self._public_item(row, include_body=False) for row in rows]

    def get_item(self, item_id: int, *, include_body: bool = True) -> dict[str, Any] | None:
        row = self.conn.execute("SELECT * FROM items WHERE id=?", (item_id,)).fetchone()
        if not row:
            return None
        result = self._public_item(row, include_body=include_body)
        result["attachments"] = [
            self._public_attachment(a)
            for a in self.conn.execute(
                "SELECT * FROM attachments WHERE item_id=? ORDER BY id", (item_id,)
            )
        ]
        return result

    def search_course_content(
        self,
        query: str,
        *,
        course_id: int | None = None,
        kinds: Sequence[str] | None = None,
        limit: int = 20,
    ) -> list[dict[str, Any]]:
        needle = normalize_search(query)
        if not needle:
            return []
        tokens = [token for token in needle.split(" ") if token]
        where = ["i.available=1"]
        params: list[Any] = []
        if course_id is not None:
            where.append("i.course_id=?")
            params.append(course_id)
        if kinds:
            where.append(f"i.category IN ({','.join('?' for _ in kinds)})")
            params.extend(kinds)
        results: list[dict[str, Any]] = []
        for row in self.conn.execute(
            f"SELECT i.* FROM items i WHERE {' AND '.join(where)} "
            "ORDER BY i.updated_at DESC",
            params,
        ):
            haystack = normalize_search(
                " ".join(
                    filter(None, (row["title"], row["body"], row["course_name"]))
                )
            )
            if all(token in haystack for token in tokens):
                results.append(
                    {
                        "result_type": "item",
                        "item": self._public_item(row, include_body=True),
                        "score": sum(haystack.count(token) for token in tokens),
                    }
                )

        # search_text is normalize_search()'d at write time and the query at read
        # time, so SQL instr() substring matching is exactly the Python `in` test.
        # Candidates are pre-filtered with OR in SQL; the AND decision and the
        # score stay in Python on the (small) candidate set.
        doc_where = [
            "a.available=1",
            "(" + " OR ".join("instr(dc.search_text, ?)>0" for _ in tokens) + ")",
        ]
        doc_params: list[Any] = [*tokens]
        if course_id is not None:
            doc_where.append("a.course_id=?")
            doc_params.append(course_id)
        if kinds:
            doc_where.append(f"i.category IN ({','.join('?' for _ in kinds)})")
            doc_params.extend(kinds)
        rows = self.conn.execute(
            f"""SELECT dc.*, a.file_name, a.item_id, a.course_id,
                       c.course_name, i.category
                FROM document_chunks dc
                JOIN attachments a ON a.id=dc.attachment_id
                JOIN items i ON i.id=a.item_id
                LEFT JOIN courses c ON c.id=a.course_id
                WHERE {' AND '.join(doc_where)}
                ORDER BY dc.id DESC""",
            doc_params,
        )
        for row in rows:
            search_text = row["search_text"]
            if all(token in search_text for token in tokens):
                results.append(
                    {
                        "result_type": "document",
                        "attachment_id": row["attachment_id"],
                        "file_name": row["file_name"],
                        "course_id": row["course_id"],
                        "course_name": row["course_name"],
                        "category": row["category"],
                        "page_start": row["page_start"],
                        "page_end": row["page_end"],
                        "text": row["text"],
                        "score": sum(search_text.count(token) for token in tokens),
                    }
                )
        results.sort(key=lambda item: item["score"], reverse=True)
        return results[:limit]

    def read_document(
        self,
        attachment_id: int,
        *,
        page_from: int | None = None,
        page_to: int | None = None,
        max_chars: int = 16000,
    ) -> dict[str, Any]:
        attachment = self.conn.execute(
            """SELECT a.*, c.course_name
               FROM attachments a LEFT JOIN courses c ON c.id=a.course_id
               WHERE a.id=?""",
            (attachment_id,),
        ).fetchone()
        if not attachment:
            raise ContentNotFound(f"Attachment {attachment_id} was not found")
        where = ["attachment_id=?"]
        params: list[Any] = [attachment_id]
        if page_from is not None:
            where.append("page_number>=?")
            params.append(page_from)
        if page_to is not None:
            where.append("page_number<=?")
            params.append(page_to)
        pages = []
        used = 0
        truncated = False
        for row in self.conn.execute(
            f"SELECT * FROM document_pages WHERE {' AND '.join(where)} ORDER BY page_number",
            params,
        ):
            remaining = max_chars - used
            if remaining <= 0:
                truncated = True
                break
            text = row["text"]
            if len(text) > remaining:
                text = text[:remaining]
                truncated = True
            pages.append(
                {
                    "page_number": row["page_number"],
                    "text": text,
                    "extraction_method": row["extraction_method"],
                }
            )
            used += len(text)
            if truncated:
                break
        return {
            "attachment": self._public_attachment(attachment),
            "course_name": attachment["course_name"],
            "pages": pages,
            "truncated": truncated,
            "returned_chars": used,
        }

    def get_changes(
        self,
        *,
        since: str | None = None,
        course_id: int | None = None,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        where: list[str] = []
        params: list[Any] = []
        if since:
            where.append("r.detected_at>=?")
            params.append(since)
        if course_id is not None:
            where.append("i.course_id=?")
            params.append(course_id)
        sql = (
            "SELECT r.*, i.title, i.category, i.course_name, i.url "
            "FROM item_revisions r LEFT JOIN items i ON i.id=r.item_id"
        )
        if where:
            sql += " WHERE " + " AND ".join(where)
        sql += " ORDER BY r.detected_at DESC, r.id DESC LIMIT ?"
        params.append(limit)
        return [
            {
                **dict(row),
                "changed_fields": json.loads(row["changed_fields_json"] or "[]"),
                "before": json.loads(row["before_json"]) if row["before_json"] else None,
                "after": json.loads(row["after_json"]) if row["after_json"] else None,
            }
            for row in self.conn.execute(sql, params)
        ]

    def get_notifications(
        self, *, acknowledged: bool | None = False, limit: int = 100
    ) -> list[dict[str, Any]]:
        where = ""
        params: list[Any] = []
        if acknowledged is not None:
            where = "WHERE n.is_acknowledged=?"
            params.append(int(acknowledged))
        params.append(limit)
        return [
            dict(row)
            for row in self.conn.execute(
                f"""SELECT n.*, i.category, i.course_name, i.deadline, i.url
                    FROM notifications n LEFT JOIN items i ON i.id=n.item_id
                    {where}
                    ORDER BY n.created_at DESC, n.id DESC LIMIT ?""",
                params,
            )
        ]

    def acknowledge_notifications(self, notification_ids: Sequence[int]) -> int:
        ids = sorted({int(value) for value in notification_ids})
        if not ids:
            return 0
        cur = self.conn.execute(
            f"UPDATE notifications SET is_acknowledged=1 "
            f"WHERE id IN ({','.join('?' for _ in ids)}) AND is_acknowledged=0",
            ids,
        )
        self.conn.commit()
        return cur.rowcount

    def build_study_context(
        self,
        query: str,
        *,
        course_id: int,
        limit: int = 12,
        max_chars: int = 20000,
    ) -> dict[str, Any]:
        results = self.search_course_content(
            query, course_id=course_id, limit=max(limit * 2, limit)
        )
        evidence = []
        used = 0
        for result in results:
            if len(evidence) >= limit or used >= max_chars:
                break
            if result["result_type"] == "document":
                text = result["text"]
                citation = {
                    "attachment_id": result["attachment_id"],
                    "file_name": result["file_name"],
                    "page_start": result["page_start"],
                    "page_end": result["page_end"],
                }
                kind = "document"
            else:
                item = result["item"]
                text = item.get("body") or item["title"]
                citation = {
                    "item_id": item["id"],
                    "title": item["title"],
                    "url": item.get("url"),
                }
                kind = item["category"]
            remaining = max_chars - used
            if len(text) > remaining:
                text = text[:remaining]
            evidence.append({"kind": kind, "text": text, "citation": citation})
            used += len(text)
        course = self.conn.execute("SELECT * FROM courses WHERE id=?", (course_id,)).fetchone()
        return {
            "query": query,
            "course": self._public_course(course) if course else None,
            "evidence": evidence,
            "returned_chars": used,
            "truncated": len(results) > len(evidence),
            "note": "Evidence pack only; the MCP client should summarize while retaining citations.",
        }

    def export_materials(
        self,
        *,
        course_id: int,
        export_root: Path,
        item_ids: Sequence[int] | None = None,
    ) -> dict[str, Any]:
        export_root = export_root.resolve()
        export_root.mkdir(parents=True, exist_ok=True)
        course = self.conn.execute("SELECT * FROM courses WHERE id=?", (course_id,)).fetchone()
        if not course:
            raise ContentNotFound(f"Course {course_id} was not found")
        where = [
            "a.course_id=?",
            "a.available=1",
            "a.local_path IS NOT NULL",
            "i.category='material'",
        ]
        params: list[Any] = [course_id]
        if item_ids:
            clean_ids = sorted({int(value) for value in item_ids})
            where.append(f"a.item_id IN ({','.join('?' for _ in clean_ids)})")
            params.extend(clean_ids)
        rows = self.conn.execute(
            f"""SELECT a.*, i.title AS item_title
                FROM attachments a JOIN items i ON i.id=a.item_id
                WHERE {' AND '.join(where)} ORDER BY i.published_at, a.id""",
            params,
        ).fetchall()
        course_dir = export_root / self._safe_name(course["course_name"])
        course_dir.mkdir(parents=True, exist_ok=True)
        exported = []
        for row in rows:
            source = Path(row["local_path"]).resolve()
            if not source.is_file():
                continue
            group = course_dir / self._safe_name(row["item_title"])
            group.mkdir(parents=True, exist_ok=True)
            target = (group / self._safe_name(row["file_name"])).resolve()
            if export_root not in target.parents:
                raise ExportPathError(f"Export target escaped root: {target}")
            if target.exists() and self._sha256_file(target) != row["sha256"]:
                target = target.with_name(
                    f"{target.stem}-{(row['sha256'] or 'different')[:8]}{target.suffix}"
                )
            if not target.exists():
                shutil.copy2(source, target)
            self.conn.execute(
                "INSERT INTO exports(attachment_id, destination, sha256) VALUES (?, ?, ?)",
                (row["id"], str(target), row["sha256"]),
            )
            exported.append({"attachment_id": row["id"], "path": str(target)})
        self.conn.commit()
        return {"course_id": course_id, "export_root": str(course_dir), "files": exported}

    # ---------------------------------------------------------------- helpers

    @staticmethod
    def _safe_name(value: str) -> str:
        cleaned = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", value).strip(" .")
        return cleaned[:120] or "untitled"

    @staticmethod
    def _sha256_file(path: Path) -> str:
        digest = hashlib.sha256()
        with path.open("rb") as handle:
            for block in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(block)
        return digest.hexdigest()

    @staticmethod
    def _public_course(row: sqlite3.Row) -> dict[str, Any]:
        out = dict(row)
        out["instructors"] = json.loads(out.pop("instructors_json") or "[]")
        out["metadata"] = json.loads(out.pop("metadata_json") or "{}")
        out["available"] = bool(out["available"])
        return out

    @staticmethod
    def _public_item(row: sqlite3.Row, *, include_body: bool) -> dict[str, Any]:
        out = dict(row)
        out["metadata"] = json.loads(out.pop("metadata_json") or "{}")
        out["is_read"] = bool(out["is_read"])
        out["is_done"] = bool(out["is_done"])
        out["available"] = bool(out["available"])
        if not include_body:
            out.pop("body", None)
        return out

    @staticmethod
    def _public_attachment(row: sqlite3.Row) -> dict[str, Any]:
        out = dict(row)
        out["metadata"] = json.loads(out.pop("metadata_json") or "{}")
        out["available"] = bool(out["available"])
        # Download recipes are intentionally internal.
        out.pop("object_name", None)
        return out
