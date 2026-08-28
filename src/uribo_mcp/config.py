"""Configuration loading without importing heavyweight scraper dependencies."""

from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from uribo_mcp.errors import ConfigurationError

ENV_CONFIG = "URIBO_MCP_CONFIG"


def _as_path(root: Path, raw: str | Path) -> Path:
    path = Path(raw).expanduser()
    return path.resolve() if path.is_absolute() else (root / path).resolve()


@dataclass(frozen=True)
class PathSettings:
    root: Path
    data_dir: Path
    cache_dir: Path
    export_root: Path
    credentials_dir: Path

    @property
    def db_path(self) -> Path:
        return self.data_dir / "uribo.db"

    @property
    def lock_path(self) -> Path:
        return self.data_dir / "sync.lock"

    @property
    def jobs_log_dir(self) -> Path:
        return self.data_dir / "logs"


@dataclass(frozen=True)
class SyncSettings:
    enabled_sources: tuple[str, ...] = (
        "csi",
        "pdf_calendar",
        "newsnet",
        "beef",
        "venture",
        "uribo",
        "mail",
    )
    request_interval_seconds: float = 3.0
    source_interval_seconds: float = 5.0
    job_stale_hours: int = 6


@dataclass(frozen=True)
class DocumentSettings:
    max_file_bytes: int = 100 * 1024 * 1024
    max_pdf_pages: int = 1000
    chunk_chars: int = 4000
    chunk_overlap_chars: int = 400
    ocr_min_chars_per_page: int = 40
    tesseract_command: str = "tesseract"
    ocr_languages: str = "jpn+jpn_vert+eng"


@dataclass(frozen=True)
class McpSettings:
    default_limit: int = 20
    max_limit: int = 200
    max_text_chars: int = 16000


@dataclass(frozen=True)
class FeatureSettings:
    google_calendar_sync: bool = False
    ocr: bool = True
    office_documents: bool = True


@dataclass(frozen=True)
class Settings:
    paths: PathSettings
    sync: SyncSettings = field(default_factory=SyncSettings)
    documents: DocumentSettings = field(default_factory=DocumentSettings)
    mcp: McpSettings = field(default_factory=McpSettings)
    features: FeatureSettings = field(default_factory=FeatureSettings)
    config_path: Path | None = None

    def ensure_directories(self) -> None:
        for path in (
            self.paths.data_dir,
            self.paths.cache_dir,
            self.paths.export_root,
            self.paths.credentials_dir,
            self.paths.jobs_log_dir,
        ):
            path.mkdir(parents=True, exist_ok=True)


def _section(data: dict[str, Any], name: str) -> dict[str, Any]:
    value = data.get(name, {})
    if not isinstance(value, dict):
        raise ConfigurationError(f"[{name}] must be a TOML table")
    return value


def load_settings(config_path: str | Path | None = None, *, root: Path | None = None) -> Settings:
    """Load TOML configuration with safe defaults rooted at the project directory."""
    root = (root or Path.cwd()).resolve()
    selected = config_path or os.environ.get(ENV_CONFIG)
    path = _as_path(root, selected) if selected else root / "config.local.toml"
    data: dict[str, Any] = {}
    if path.exists():
        try:
            data = tomllib.loads(path.read_text(encoding="utf-8"))
        except (OSError, tomllib.TOMLDecodeError) as exc:
            raise ConfigurationError(f"Failed to read config: {path}: {exc}") from exc

    paths = _section(data, "paths")
    data_dir = _as_path(root, paths.get("data_dir", "data"))
    path_settings = PathSettings(
        root=root,
        data_dir=data_dir,
        cache_dir=_as_path(root, paths.get("cache_dir", data_dir / "cache")),
        export_root=_as_path(root, paths.get("export_root", "exports")),
        credentials_dir=_as_path(root, paths.get("credentials_dir", "credentials")),
    )

    sync = _section(data, "sync")
    enabled = sync.get("enabled_sources", list(SyncSettings().enabled_sources))
    if not isinstance(enabled, list) or not all(isinstance(x, str) for x in enabled):
        raise ConfigurationError("sync.enabled_sources must be a list of strings")
    sync_settings = SyncSettings(
        enabled_sources=tuple(enabled),
        request_interval_seconds=float(sync.get("request_interval_seconds", 3.0)),
        source_interval_seconds=float(sync.get("source_interval_seconds", 5.0)),
        job_stale_hours=int(sync.get("job_stale_hours", 6)),
    )
    if (
        sync_settings.request_interval_seconds < 0
        or sync_settings.source_interval_seconds < 0
        or sync_settings.job_stale_hours < 1
    ):
        raise ConfigurationError(
            "sync intervals must be non-negative and job_stale_hours must be positive"
        )

    docs = _section(data, "documents")
    document_settings = DocumentSettings(
        max_file_bytes=int(docs.get("max_file_bytes", 100 * 1024 * 1024)),
        max_pdf_pages=int(docs.get("max_pdf_pages", 1000)),
        chunk_chars=int(docs.get("chunk_chars", 4000)),
        chunk_overlap_chars=int(docs.get("chunk_overlap_chars", 400)),
        ocr_min_chars_per_page=int(docs.get("ocr_min_chars_per_page", 40)),
        tesseract_command=str(docs.get("tesseract_command", "tesseract")),
        ocr_languages=str(docs.get("ocr_languages", "jpn+jpn_vert+eng")),
    )
    if document_settings.chunk_overlap_chars >= document_settings.chunk_chars:
        raise ConfigurationError("documents.chunk_overlap_chars must be smaller than chunk_chars")
    if (
        document_settings.max_file_bytes < 1
        or document_settings.max_pdf_pages < 1
        or document_settings.chunk_chars < 1
        or document_settings.chunk_overlap_chars < 0
    ):
        raise ConfigurationError("document limits must be positive")

    mcp = _section(data, "mcp")
    mcp_settings = McpSettings(
        default_limit=int(mcp.get("default_limit", 20)),
        max_limit=int(mcp.get("max_limit", 200)),
        max_text_chars=int(mcp.get("max_text_chars", 16000)),
    )
    if (
        mcp_settings.default_limit < 1
        or mcp_settings.max_limit < mcp_settings.default_limit
        or mcp_settings.max_text_chars < 1
    ):
        raise ConfigurationError("invalid MCP response limits")
    features = _section(data, "features")
    feature_settings = FeatureSettings(
        google_calendar_sync=bool(features.get("google_calendar_sync", False)),
        ocr=bool(features.get("ocr", True)),
        office_documents=bool(features.get("office_documents", True)),
    )

    result = Settings(
        paths=path_settings,
        sync=sync_settings,
        documents=document_settings,
        mcp=mcp_settings,
        features=feature_settings,
        config_path=path if path.exists() else None,
    )
    result.ensure_directories()
    return result
