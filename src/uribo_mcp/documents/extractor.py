"""Bounded native extraction for course attachments.

No network or persistence happens here. Optional third-party readers are used
when present; Office Open XML fallbacks use the standard library so common
course material remains readable in a minimal installation.
"""
from __future__ import annotations

import hashlib
import mimetypes
import re
import subprocess
import tempfile
import zipfile
from dataclasses import dataclass
from pathlib import Path
from xml.etree import ElementTree

from uribo_mcp.subprocesses import hidden_process_kwargs

from .models import DocumentChunk, DocumentPage, DocumentResult
from .ocr import OcrBackend


@dataclass(frozen=True)
class DocumentLimits:
    max_bytes: int = 50 * 1024 * 1024
    max_pages: int = 300
    chunk_chars: int = 3_000
    chunk_overlap_chars: int = 300
    ocr_min_native_chars: int = 40


_MEDIA_TYPES = {
    ".pdf": "application/pdf", ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    ".pptx": "application/vnd.openxmlformats-officedocument.presentationml.presentation",
    ".xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet", ".txt": "text/plain",
    ".csv": "text/csv", ".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".gif": "image/gif", ".webp": "image/webp",
}
_IMAGE_SUFFIXES = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".tif", ".tiff", ".bmp"}


def sha256_file(path: str | Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _chunks(
    pages: list[DocumentPage], chunk_chars: int, overlap_chars: int
) -> tuple[DocumentChunk, ...]:
    chunks: list[DocumentChunk] = []
    overlap_chars = max(0, min(overlap_chars, max(0, chunk_chars - 1)))
    for page in pages:
        text = page.text.strip()
        if not text:
            continue
        start = 0
        while start < len(text):
            end = min(len(text), start + max(1, chunk_chars))
            if end < len(text):
                boundary = max(
                    text.rfind("\n", start, end),
                    text.rfind(" ", start, end),
                )
                if boundary > start:
                    end = boundary
            piece = text[start:end].strip()
            if piece:
                chunks.append(
                    DocumentChunk(
                        len(chunks), page.number, page.number, piece
                    )
                )
            if end >= len(text):
                break
            start = max(start + 1, end - overlap_chars)
    return tuple(chunks)


def _xml_text(data: bytes) -> str:
    try:
        root = ElementTree.fromstring(data)
    except ElementTree.ParseError:
        return ""
    return " ".join(text.strip() for text in root.itertext() if text and text.strip())


def _read_docx(path: Path, max_pages: int) -> list[DocumentPage]:
    with zipfile.ZipFile(path) as archive:
        text = _xml_text(archive.read("word/document.xml"))
    return [DocumentPage(1, text)] if text else []


def _read_pptx(path: Path, max_pages: int) -> list[DocumentPage]:
    with zipfile.ZipFile(path) as archive:
        names = sorted(
            (name for name in archive.namelist() if re.fullmatch(r"ppt/slides/slide\d+\.xml", name)),
            key=lambda name: int(re.search(r"\d+", name).group()),
        )
        return [DocumentPage(index + 1, _xml_text(archive.read(name))) for index, name in enumerate(names[:max_pages])]


def _read_xlsx(path: Path, max_pages: int) -> list[DocumentPage]:
    with zipfile.ZipFile(path) as archive:
        shared = []
        if "xl/sharedStrings.xml" in archive.namelist():
            shared = [text for text in _xml_text(archive.read("xl/sharedStrings.xml")).split(" ") if text]
        names = sorted(
            (name for name in archive.namelist() if re.fullmatch(r"xl/worksheets/sheet\d+\.xml", name)),
            key=lambda name: int(re.search(r"\d+", name).group()),
        )
        pages: list[DocumentPage] = []
        for index, name in enumerate(names[:max_pages]):
            root = ElementTree.fromstring(archive.read(name))
            rows = []
            for row in root.findall("{*}sheetData/{*}row"):
                values = []
                for cell in row.findall("{*}c"):
                    value = cell.findtext("{*}v", default="")
                    if cell.get("t") == "s" and value.isdigit() and int(value) < len(shared):
                        value = shared[int(value)]
                    values.append(value)
                if values:
                    rows.append("\t".join(values))
            pages.append(DocumentPage(index + 1, "\n".join(rows)))
        return pages


def _read_text(path: Path) -> list[DocumentPage]:
    raw = path.read_bytes()
    for encoding in ("utf-8-sig", "utf-8", "cp932"):
        try:
            return [DocumentPage(1, raw.decode(encoding))]
        except UnicodeDecodeError:
            pass
    return [DocumentPage(1, raw.decode("utf-8", errors="replace"))]


def _ocr_pdf_page(path: Path, page_number: int, backend: OcrBackend) -> str:
    """Render exactly one PDF page before passing it to the configured OCR.

    ``pdftoppm`` is optional. Its absence is represented by retaining native
    text rather than making a whole document unreadable.
    """
    with tempfile.TemporaryDirectory(prefix="uribo-mcp-ocr-") as directory:
        prefix = Path(directory) / "page"
        try:
            completed = subprocess.run(
                ["pdftoppm", "-f", str(page_number), "-l", str(page_number), "-png", str(path), str(prefix)],
                capture_output=True, text=True, encoding="utf-8", timeout=120, check=False,
                **hidden_process_kwargs(),
            )
        except (FileNotFoundError, subprocess.TimeoutExpired) as exc:
            raise RuntimeError("PDF OCR requires the local pdftoppm executable") from exc
        if completed.returncode:
            raise RuntimeError(completed.stderr.strip() or "pdftoppm failed")
        image = next(Path(directory).glob("page-*.png"), None)
        if image is None:
            raise RuntimeError("pdftoppm produced no image")
        return backend.image_to_text(image)


def _read_pdf(path: Path, limits: DocumentLimits, ocr_backend: OcrBackend | None) -> tuple[list[DocumentPage], bool, str | None]:
    try:
        import pdfplumber  # type: ignore[import-not-found]
    except ImportError:
        return [], False, "PDF extraction requires the optional 'pdfplumber' dependency"
    try:
        with pdfplumber.open(path) as document:
            pages: list[DocumentPage] = []
            for index, page in enumerate(document.pages[:limits.max_pages]):
                text = (page.extract_text() or "").strip()
                source = "native"
                if ocr_backend and len(text) < limits.ocr_min_native_chars:
                    try:
                        recognised = _ocr_pdf_page(path, index + 1, ocr_backend).strip()
                        if recognised:
                            text, source = recognised, "ocr"
                    except RuntimeError:
                        # Native extraction is still useful; a sync worker can record
                        # OCR availability separately without turning this into failure.
                        pass
                pages.append(DocumentPage(index + 1, text, source))
            return pages, len(document.pages) > limits.max_pages, None
    except Exception as exc:  # Reader error is a result, never an uncaught MCP failure.
        if any(word in str(exc).lower() for word in ("password", "encrypt")):
            return [], False, "password_required"
        return [], False, f"{type(exc).__name__}: {exc}"


def extract_document(
    path: str | Path,
    *,
    limits: DocumentLimits | None = None,
    ocr_backend: OcrBackend | None = None,
) -> DocumentResult:
    """Extract a local attachment into bounded pages and search chunks.

    Status is one of ``ok``, ``password_required``, ``unsupported`` or
    ``error``. Files beyond ``max_bytes`` are rejected before parsing.
    """
    limits = limits or DocumentLimits()
    file_path = Path(path)
    digest = sha256_file(file_path)
    suffix = file_path.suffix.lower()
    media_type = _MEDIA_TYPES.get(suffix) or mimetypes.guess_type(file_path.name)[0]
    if file_path.stat().st_size > limits.max_bytes:
        return DocumentResult("error", digest, media_type, error=f"size_limit_exceeded: {limits.max_bytes} bytes")
    try:
        truncated = False
        if suffix == ".pdf":
            pages, truncated, error = _read_pdf(file_path, limits, ocr_backend)
            if error:
                status = "password_required" if error == "password_required" else "error"
                return DocumentResult(status, digest, media_type, error=error)
        elif suffix == ".docx":
            pages = _read_docx(file_path, limits.max_pages)
        elif suffix == ".pptx":
            pages = _read_pptx(file_path, limits.max_pages)
        elif suffix == ".xlsx":
            pages = _read_xlsx(file_path, limits.max_pages)
        elif suffix in {".txt", ".csv"}:
            pages = _read_text(file_path)
        elif suffix in _IMAGE_SUFFIXES:
            if not ocr_backend:
                return DocumentResult("unsupported", digest, media_type, error="OCR backend is not configured")
            pages = [DocumentPage(1, ocr_backend.image_to_text(file_path), "ocr")]
        else:
            return DocumentResult("unsupported", digest, media_type, error=f"unsupported extension: {suffix or '(none)'}")
        chunks = _chunks(
            pages, limits.chunk_chars, limits.chunk_overlap_chars
        )
        return DocumentResult("ok", digest, media_type, tuple(pages), chunks, truncated=truncated)
    except (OSError, zipfile.BadZipFile, ElementTree.ParseError, RuntimeError) as exc:
        return DocumentResult("error", digest, media_type, error=f"{type(exc).__name__}: {exc}")
