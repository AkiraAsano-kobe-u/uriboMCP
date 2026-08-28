import sqlite3
from pathlib import Path

from uribo_mcp.db import database
from uribo_mcp.migration import import_legacy_database


def test_legacy_import_copies_data_without_credentials(tmp_path: Path) -> None:
    source_path = tmp_path / "old.db"
    source = sqlite3.connect(source_path)
    source.executescript(
        """
        CREATE TABLE items (
          id INTEGER PRIMARY KEY, source TEXT, source_id TEXT, category TEXT,
          title TEXT, body TEXT, url TEXT, course_name TEXT, published_at TEXT,
          deadline TEXT, importance TEXT, is_read INTEGER, is_done INTEGER,
          cluster_id INTEGER
        );
        CREATE TABLE timetable (
          day_of_week INTEGER, period INTEGER, course_name TEXT, room TEXT,
          quarter TEXT, idnumber TEXT
        );
        CREATE TABLE preferences (key TEXT PRIMARY KEY, value TEXT);
        INSERT INTO items VALUES
          (1,'beef','assignment/C1/1','assignment','課題',NULL,NULL,'解析学',
           NULL,NULL,'action_required',1,0,NULL);
        INSERT INTO timetable VALUES (1,1,'解析学','A101','Q1','C1');
        INSERT INTO preferences VALUES ('classification_keywords','["重要"]');
        """
    )
    source.commit()
    source.close()
    destination = tmp_path / "new.db"
    result = import_legacy_database(source_path, destination)
    assert result["items"] == 1
    with database(destination) as connection:
        row = connection.execute("SELECT * FROM items").fetchone()
        assert row["title"] == "課題"
        assert row["is_read"] == 1
        assert connection.execute("SELECT COUNT(*) FROM timetable").fetchone()[0] == 1
