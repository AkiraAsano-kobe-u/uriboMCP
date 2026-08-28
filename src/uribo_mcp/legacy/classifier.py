from __future__ import annotations

from collections.abc import Iterable, Sequence
from typing import Any

from .common import is_actionable_open, normalize_text

RELEVANT_CATEGORIES = ("announcement", "news", "material")


def matched_course(title: str | None, body: str | None, courses_norm: Iterable[str]) -> bool:
    title_norm, body_norm = normalize_text(title), normalize_text(body)
    return any(c and (c in title_norm or c in body_norm) for c in courses_norm)


def matched_keyword(title: str | None, body: str | None, keywords_norm: Iterable[str]) -> bool:
    title_norm, body_norm = normalize_text(title), normalize_text(body)
    return any(k and (k in title_norm or k in body_norm) for k in keywords_norm)


def classify(category: str | None, is_done: Any, deadline: str | None, title: str | None, body: str | None, courses_norm: Sequence[str], keywords_norm: Sequence[str]) -> str:
    if is_actionable_open(category, is_done, deadline): return "action_required"
    if category in RELEVANT_CATEGORIES and (matched_course(title, body, courses_norm) or matched_keyword(title, body, keywords_norm)): return "relevant"
    return "info"


def apply(conn, *, keywords: Sequence[str]) -> dict[str, int]:
    courses = [normalize_text(r[0]) for r in conn.execute("SELECT DISTINCT course_name FROM timetable WHERE course_name IS NOT NULL AND course_name <> ''")]
    words = [normalize_text(k) for k in keywords]; counts = {"action_required": 0, "relevant": 0, "info": 0}
    for row in conn.execute(
        "SELECT id, category, is_done, deadline, title, body, importance FROM items"
    ):
        value = classify(row["category"], row["is_done"], row["deadline"], row["title"], row["body"], courses, words)
        if row["importance"] != value:
            conn.execute(
                "UPDATE items SET importance=? WHERE id=?", (value, row["id"])
            )
        counts[value] += 1
    conn.commit(); return counts
