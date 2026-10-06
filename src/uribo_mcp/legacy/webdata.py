"""Database-neutral compatibility queries from the original dashboard.

No Flask/FastAPI state is used: every operation receives an explicit DB
connection.  The host schema only needs the legacy ``items``, ``timetable``,
``course_notes``, ``preferences`` and ``scrape_log`` tables.
"""
from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

from .common import CATEGORY_LABELS, clean, is_actionable_open, parse_datetime

SOURCE_META = {
    "beef": {"label": "BEEF+", "full": "LMS (BEEF+)", "mark": "oklch(0.72 0.115 212)"},
    "moodle": {"label": "BEEF", "full": "LMS BEEF (Moodle)", "mark": "oklch(0.66 0.100 160)"},
    "venture": {"label": "venture", "full": "venture", "mark": "oklch(0.60 0.015 250)"},
    "csi": {"label": "学部", "full": "システム情報学部サイト", "mark": "oklch(0.55 0.020 250)"},
    "uribo": {"label": "うりぼー", "full": "教務システム", "mark": "oklch(0.70 0.020 250)"},
    "mail": {"label": "メール", "full": "大学メール", "mark": "oklch(0.65 0.018 250)"},
    "newsnet": {"label": "News", "full": "ニュースネット", "mark": "oklch(0.50 0.018 250)"},
}
CATEGORY_META = {key: {"key": {"assignment": "kadai", "announcement": "info", "material": "info"}.get(key, key), "label": label} for key, label in CATEGORY_LABELS.items()}
PERIODS = [{"p": n, "start": start, "end": end} for n, start, end in [(1, "08:50", "10:20"), (2, "10:40", "12:10"), (3, "13:20", "14:50"), (4, "15:10", "16:40"), (5, "16:50", "18:20")]]
DOW_JP = ["月", "火", "水", "木", "金", "土", "日"]


def parse_dt(value: str | None) -> datetime | None: return parse_datetime(value)
def source_meta(source: str) -> dict[str, str]: return SOURCE_META.get(source, {"label": "?", "full": "不明なソース", "mark": "oklch(0.55 0.02 250)"})
def category_meta(category: str) -> dict[str, str]: return CATEGORY_META.get(category, {"key": "info", "label": "その他"})
def importance_key(value: str | None) -> str: return value if value in {"action_required", "relevant", "info"} else "uncat"


def map_item(row, *, include_body: bool = False) -> dict[str, Any]:
    category, source, imp = category_meta(row["category"]), source_meta(row["source"]), importance_key(row["importance"])
    body = row["body"] or ""; result = {"id": row["id"], "source": row["source"], "source_label": source["label"], "source_full": source["full"], "source_mark": source["mark"], "category": row["category"], "cat_key": category["key"], "cat_label": category["label"], "importance": imp, "title": row["title"], "summary": clean(body)[:64] + ("…" if len(clean(body)) > 64 else ""), "course_name": row["course_name"], "url": row["url"], "published_at": row["published_at"], "deadline": row["deadline"], "is_read": bool(row["is_read"]), "is_done": bool(row["is_done"]), "is_actionable": row["category"] in {"assignment", "test", "survey"}, "cluster_id": row["cluster_id"], "alts": []}
    if include_body: result["body"] = body
    return result


def get_actions(conn) -> list[dict]:
    rows = conn.execute("SELECT * FROM items WHERE category IN ('assignment','test','survey') AND is_done=0 AND deadline IS NOT NULL ORDER BY deadline").fetchall()
    return [map_item(row, include_body=True) for row in rows]


def get_feed(conn, now: datetime | None = None, *, limit: int = 100) -> list[dict]:
    rows = conn.execute("SELECT * FROM items ORDER BY COALESCE(published_at,created_at) DESC LIMIT ?", (limit,)).fetchall()
    return [map_item(row) for row in rows]


def get_inbox(conn, now: datetime | None = None) -> list[dict]:
    """Return newest items with alternate members of a dedup cluster attached."""
    rows = conn.execute("SELECT * FROM items ORDER BY COALESCE(published_at,created_at) DESC, id DESC").fetchall()
    by_cluster: dict[int, list] = {}
    for row in rows:
        if row["cluster_id"] is not None: by_cluster.setdefault(row["cluster_id"], []).append(row)
    result = []
    seen: set[int] = set()
    for row in rows:
        cluster_id = row["cluster_id"]
        if cluster_id is not None and cluster_id in seen: continue
        if cluster_id is not None: seen.add(cluster_id)
        item = map_item(row)
        item["alts"] = [map_item(other) for other in by_cluster.get(cluster_id, []) if other["id"] != row["id"]]
        result.append(item)
    return result


def count_unread(conn, now: datetime | None = None) -> int:
    return conn.execute("SELECT COUNT(*) FROM items WHERE is_read=0").fetchone()[0]


def get_item(conn, item_id: int) -> dict[str, Any] | None:
    row = conn.execute("SELECT * FROM items WHERE id=?", (item_id,)).fetchone(); return map_item(row, include_body=True) if row else None


def set_read(conn, item_id: int, value: bool) -> bool:
    cur = conn.execute("UPDATE items SET is_read=? WHERE id=?", (int(value), item_id)); conn.commit(); return cur.rowcount > 0


def set_done(conn, item_id: int, value: bool) -> bool:
    cur = conn.execute("UPDATE items SET is_done=? WHERE id=?", (int(value), item_id)); conn.commit(); return cur.rowcount > 0


def filter_sources(conn) -> list[dict[str, str]]:
    return [source_meta(row[0]) | {"source": row[0]} for row in conn.execute("SELECT DISTINCT source FROM items ORDER BY source")]


def filter_categories(conn) -> list[dict[str, str]]:
    return [category_meta(row[0]) | {"category": row[0]} for row in conn.execute("SELECT DISTINCT category FROM items ORDER BY category")]


def get_timetable_today(conn, now: datetime) -> list[dict]:
    dow = now.weekday() + 1; rows = conn.execute("SELECT period, course_name, room FROM timetable WHERE day_of_week=? ORDER BY period", (dow,)).fetchall()
    return [{"period": row["period"], "start": next((p["start"] for p in PERIODS if p["p"] == row["period"]), None), "course_name": row["course_name"], "course_names": [row["course_name"]], "room": row["room"], "has_class": True} for row in rows]


def get_timetable_week(conn, now: datetime) -> dict[str, Any]:
    monday = now.date() - timedelta(days=now.weekday())
    days = [{"dow": DOW_JP[i], "date_label": f"{(monday + timedelta(days=i)).month}/{(monday + timedelta(days=i)).day}", "is_today": monday + timedelta(days=i) == now.date(), "dow_num": i + 1} for i in range(6)]
    rows = conn.execute("SELECT day_of_week,period,course_name,room,idnumber FROM timetable ORDER BY day_of_week,period").fetchall()
    lookup = {(r["day_of_week"], r["period"]): r for r in rows}
    grid = [{"period": p["p"], "start": p["start"], "cells": [{"has_class": (r := lookup.get((d["dow_num"], p["p"]))) is not None, "course_name": r["course_name"] if r else None, "course_names": [r["course_name"]] if r else [], "room": r["room"] if r else None, "is_today": d["is_today"], "state": "", "badge": None, "has_note": False, "note_preview": None} for d in days]} for p in PERIODS]
    return {"days": days, "grid": grid, "class_count": len(lookup)}


def get_preference(conn, key: str, default: str | None = None) -> str | None:
    try: row = conn.execute("SELECT value FROM user_preferences WHERE key=?", (key,)).fetchone()
    except Exception: row = conn.execute("SELECT value FROM preferences WHERE key=?", (key,)).fetchone()
    return row[0] if row else default


def set_preference(conn, key: str, value: str) -> None:
    try: conn.execute("INSERT INTO user_preferences(key,value,updated_at) VALUES (?,?,datetime('now')) ON CONFLICT(key) DO UPDATE SET value=excluded.value,updated_at=datetime('now')", (key, value))
    except Exception: conn.execute("INSERT INTO preferences(key,value) VALUES (?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, value))
    conn.commit()


def get_calendar_items(conn) -> list[dict]:
    rows = conn.execute("SELECT * FROM items WHERE deadline IS NOT NULL OR category='event'").fetchall(); return [map_item(row) for row in rows]


def _normalize_course(name: str | None) -> str:
    import re
    return re.sub(r"\s+", "", re.sub(r"^[0-9A-Za-z]+\s+", "", name or ""))


def _note(row) -> dict: return {"id": row["id"], "course_name": row["course_name"], "title": row["title"], "content": row["content"] or "", "created_at": row["created_at"], "updated_at": row["updated_at"]}
def get_course_notes(conn, course_names: list[str]) -> list[dict]:
    names = {_normalize_course(x) for x in course_names}; return [_note(row) for row in conn.execute("SELECT * FROM course_notes ORDER BY created_at DESC,id DESC") if _normalize_course(row["course_name"]) in names]
def get_all_notes(conn) -> list[dict]: return [_note(row) for row in conn.execute("SELECT * FROM course_notes ORDER BY created_at DESC,id DESC")]
def add_course_note(conn, course_name: str, title: str, content: str | None) -> dict:
    cursor = conn.execute("INSERT INTO course_notes(course_name,title,content) VALUES(?,?,?)", (course_name,title,content)); conn.commit(); return _note(conn.execute("SELECT * FROM course_notes WHERE id=?", (cursor.lastrowid,)).fetchone())
def update_course_note(conn, note_id: int, title: str, content: str | None) -> bool:
    cursor = conn.execute("UPDATE course_notes SET title=?,content=?,updated_at=datetime('now') WHERE id=?", (title,content,note_id)); conn.commit(); return cursor.rowcount > 0
def delete_course_note(conn, note_id: int) -> bool:
    cursor = conn.execute("DELETE FROM course_notes WHERE id=?", (note_id,)); conn.commit(); return cursor.rowcount > 0


def get_course_related(conn, course_names: list[str], *, announcement_limit: int = 10) -> dict[str, Any]:
    names = {_normalize_course(x) for x in course_names}; rows = [r for r in conn.execute("SELECT * FROM items WHERE course_name IS NOT NULL") if _normalize_course(r["course_name"]) in names]
    open_items = [map_item(r) for r in rows if is_actionable_open(r["category"], r["is_done"], r["deadline"])]
    open_items.sort(key=lambda x: x["deadline"] or "")
    announcements = [map_item(r) for r in rows if r["category"] in {"announcement", "event", "material"}][:announcement_limit]
    links = [{"course_name": r["course_name"], "url": f"https://beefplus.center.kobe-u.ac.jp/lms/course?idnumber={r['idnumber']}"} for r in conn.execute("SELECT DISTINCT course_name,idnumber FROM timetable WHERE idnumber IS NOT NULL") if _normalize_course(r["course_name"]) in names]
    links += [{"course_name": r["course_name"], "url": r["source_url"], "lms": "BEEF (Moodle)"} for r in conn.execute("SELECT course_name,source_url FROM courses WHERE source='moodle' AND available=1 AND source_url IS NOT NULL") if _normalize_course(r["course_name"]) in names]
    return {"open_actionable": open_items, "announcements": announcements, "notes": get_course_notes(conn, course_names), "course_links": links, "total": len(open_items) + len(announcements)}


def get_source_health(conn) -> list[dict]:
    rows = conn.execute("SELECT source,status,new_items,error_message,run_at FROM scrape_log ORDER BY run_at DESC").fetchall(); seen = set(); result = []
    for row in rows:
        if row["source"] in seen: continue
        seen.add(row["source"]); result.append(dict(row))
    return result


def health_summary(health: list[dict], now: datetime | None = None) -> dict[str, int]:
    return {"ok": sum(x.get("status") == "ok" for x in health), "error": sum(x.get("status") == "error" for x in health), "total": len(health)}
