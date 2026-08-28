from __future__ import annotations

import re
import unicodedata
from datetime import datetime
from typing import Any


def clean(text: str | None) -> str:
    return re.sub(r"[\s　]+", " ", text or "").strip()


def normalize_text(text: str | None) -> str:
    """Normalize Japanese full-width and whitespace variations for matching."""
    return re.sub(r"\s+", "", unicodedata.normalize("NFKC", text or "")).lower()


def parse_datetime(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def is_actionable_open(category: str | None, is_done: Any, deadline: str | None) -> bool:
    return category in {"assignment", "test", "survey"} and not bool(is_done) and bool(deadline)


CATEGORY_LABELS = {
    "assignment": "課題", "test": "テスト", "survey": "アンケート", "event": "行事",
    "announcement": "お知らせ", "material": "教材", "news": "ニュース",
}
