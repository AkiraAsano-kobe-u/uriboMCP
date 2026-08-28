"""Small serialisable value objects shared by course parsers."""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any


@dataclass(frozen=True)
class Attachment:
    stable_id: str
    name: str
    url: str | None
    kind: str | None = None
    size_bytes: int | None = None
    content_id: str | None = None
    resource_id: str | None = None
    object_name: str | None = None
    scan_status: str | None = None
    open_end_at: str | None = None
    mime_type: str | None = None
    metadata: dict[str, str] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class CourseContent:
    stable_id: str
    category: str
    title: str
    url: str | None
    body: str | None = None
    published_at: str | None = None
    deadline: str | None = None
    available_from: str | None = None
    available_until: str | None = None
    submission_status: str | None = None
    group_title: str | None = None
    attachments: tuple[Attachment, ...] = field(default_factory=tuple)
    metadata: dict[str, str] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        result = asdict(self)
        result["attachments"] = [attachment.to_dict() for attachment in self.attachments]
        return result


@dataclass(frozen=True)
class CoursePage:
    idnumber: str
    title: str
    overview: str | None
    contents: tuple[CourseContent, ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "idnumber": self.idnumber,
            "title": self.title,
            "overview": self.overview,
            "contents": [content.to_dict() for content in self.contents],
        }
