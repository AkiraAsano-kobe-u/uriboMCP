"""One-way, non-destructive import from the original KOBEDAI_dash database."""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any

from uribo_mcp.db import database
from uribo_mcp.models import ItemRecord
from uribo_mcp.repository import Repository


def import_legacy_database(source_path: Path, destination_path: Path) -> dict[str, Any]:
    source_path = source_path.resolve()
    if not source_path.is_file():
        raise FileNotFoundError(source_path)
    imported = {"items": 0, "timetable": 0, "notes": 0, "preferences": 0}
    source = sqlite3.connect(str(source_path))
    source.row_factory = sqlite3.Row
    try:
        tables = {
            row["name"]
            for row in source.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
        }
        with database(destination_path) as target:
            repository = Repository(target)
            if "items" in tables:
                for row in source.execute("SELECT * FROM items"):
                    data = dict(row)
                    result = repository.upsert_item(
                        ItemRecord(
                            source=data["source"],
                            source_id=data["source_id"],
                            category=data["category"],
                            title=data["title"],
                            body=data.get("body"),
                            url=data.get("url"),
                            course_name=data.get("course_name"),
                            published_at=data.get("published_at"),
                            deadline=data.get("deadline"),
                            metadata={"legacy_import": True},
                        )
                    )
                    target.execute(
                        """UPDATE items SET is_read=?, is_done=?, importance=?, cluster_id=?
                           WHERE id=?""",
                        (
                            int(data.get("is_read") or 0),
                            int(data.get("is_done") or 0),
                            data.get("importance"),
                            data.get("cluster_id"),
                            result.id,
                        ),
                    )
                    imported["items"] += 1
            if "timetable" in tables:
                columns = {
                    row["name"]
                    for row in source.execute("PRAGMA table_info(timetable)")
                }
                for row in source.execute("SELECT * FROM timetable"):
                    data = dict(row)
                    target.execute(
                        """INSERT INTO timetable
                           (day_of_week,period,course_name,room,quarter,idnumber)
                           VALUES (?,?,?,?,?,?)""",
                        (
                            data["day_of_week"],
                            data["period"],
                            data["course_name"],
                            data.get("room"),
                            data.get("quarter"),
                            data.get("idnumber") if "idnumber" in columns else None,
                        ),
                    )
                    imported["timetable"] += 1
            if "course_notes" in tables:
                for row in source.execute("SELECT * FROM course_notes"):
                    data = dict(row)
                    target.execute(
                        """INSERT INTO course_notes
                           (course_name,title,content,created_at,updated_at)
                           VALUES (?,?,?,?,?)""",
                        (
                            data["course_name"],
                            data["title"],
                            data.get("content"),
                            data.get("created_at"),
                            data.get("updated_at"),
                        ),
                    )
                    imported["notes"] += 1
            preference_table = (
                "user_preferences"
                if "user_preferences" in tables
                else "preferences"
                if "preferences" in tables
                else None
            )
            if preference_table:
                for row in source.execute(f"SELECT * FROM {preference_table}"):
                    data = dict(row)
                    target.execute(
                        """INSERT INTO user_preferences(key,value,updated_at)
                           VALUES (?,?,datetime('now'))
                           ON CONFLICT(key) DO UPDATE SET value=excluded.value,
                             updated_at=excluded.updated_at""",
                        (data["key"], data["value"]),
                    )
                    imported["preferences"] += 1
            target.commit()
    finally:
        source.close()
    return imported


__all__ = ["import_legacy_database"]
