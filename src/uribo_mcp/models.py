"""Shared typed records at scraper/repository boundaries."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any


@dataclass(slots=True)
class CourseRecord:
    source: str
    idnumber: str
    course_name: str
    course_code: str | None = None
    academic_year: int | None = None
    term: str | None = None
    instructors: list[str] = field(default_factory=list)
    schedule_text: str | None = None
    summary: str | None = None
    source_url: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(slots=True)
class ItemRecord:
    source: str
    source_id: str
    category: str
    title: str
    body: str | None = None
    url: str | None = None
    course_name: str | None = None
    course_id: int | None = None
    published_at: str | None = None
    deadline: str | None = None
    remote_updated_at: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(slots=True)
class AttachmentRecord:
    stable_key: str
    file_name: str
    item_id: int | None = None
    course_id: int | None = None
    content_id: str | None = None
    resource_id: str | None = None
    display_name: str | None = None
    mime_type: str | None = None
    source_url: str | None = None
    object_name: str | None = None
    scan_status: str | None = None
    open_end_at: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(slots=True)
class UpsertResult:
    id: int
    created: bool
    changed_fields: tuple[str, ...] = ()
    revision_id: int | None = None

