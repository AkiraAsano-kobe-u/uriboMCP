"""Serialisable results of a document extraction attempt."""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any


@dataclass(frozen=True)
class DocumentPage:
    number: int
    text: str
    source: str = "native"

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class DocumentChunk:
    index: int
    page_start: int
    page_end: int
    text: str

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class DocumentResult:
    status: str
    sha256: str
    media_type: str | None
    pages: tuple[DocumentPage, ...] = field(default_factory=tuple)
    chunks: tuple[DocumentChunk, ...] = field(default_factory=tuple)
    error: str | None = None
    truncated: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": self.status, "sha256": self.sha256, "media_type": self.media_type,
            "pages": [page.to_dict() for page in self.pages],
            "chunks": [chunk.to_dict() for chunk in self.chunks], "error": self.error,
            "truncated": self.truncated,
        }
