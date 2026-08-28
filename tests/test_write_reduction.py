from datetime import UTC, datetime

from uribo_mcp.db import database
from uribo_mcp.legacy import classifier, dedup, inference
from uribo_mcp.models import ItemRecord
from uribo_mcp.repository import Repository
from uribo_mcp.sync import _beef_item_course, _replace_timetable_if_changed


def test_unchanged_derived_data_does_not_rewrite_database(tmp_path) -> None:
    with database(tmp_path / "data.db") as connection:
        repository = Repository(connection)
        first = repository.upsert_item(
            ItemRecord(
                source="source-a",
                source_id="a",
                category="announcement",
                title="同じ休講案内",
                published_at="2026-07-30T00:00:00+00:00",
            )
        )
        second = repository.upsert_item(
            ItemRecord(
                source="source-b",
                source_id="b",
                category="announcement",
                title="同じ休講案内",
                published_at="2026-07-30T00:00:00+00:00",
            )
        )
        assert first.id != second.id
        now = datetime(2026, 7, 30, tzinfo=UTC)
        classifier.apply(connection, keywords=["休講"])
        dedup.apply(connection, now=now)
        inference.save_task_snapshot(connection, {"task/1", "task/2"})
        changes_before = connection.total_changes

        classifier.apply(connection, keywords=["休講"])
        dedup.apply(connection, now=now)
        inference.save_task_snapshot(connection, {"task/1", "task/2"})

        assert connection.total_changes == changes_before


def test_unchanged_timetable_is_not_rewritten(tmp_path) -> None:
    rows = [
        {
            "day_of_week": 1,
            "period": 2,
            "course_name": "線形代数",
            "room": "A101",
            "quarter": "Q1",
            "idnumber": "C1",
        }
    ]
    with database(tmp_path / "data.db") as connection:
        assert _replace_timetable_if_changed(connection, rows)
        changes_before = connection.total_changes

        assert not _replace_timetable_if_changed(connection, rows)
        assert connection.total_changes == changes_before


def test_beef_course_items_are_identified_for_canonical_course_sync() -> None:
    info_map = {"42": "C1"}

    assert (
        _beef_item_course(
            {
                "category": "assignment",
                "source_id": "assignment/C1/7",
            },
            info_map,
        )
        == "C1"
    )
    assert (
        _beef_item_course(
            {
                "category": "announcement",
                "source_id": "announcement/info/42",
            },
            info_map,
        )
        == "C1"
    )
    assert (
        _beef_item_course(
            {
                "category": "test",
                "source_id": "test/C1/8",
            },
            info_map,
        )
        == "C1"
    )
    assert (
        _beef_item_course(
            {
                "category": "survey",
                "source_id": "survey/C1/9",
            },
            info_map,
        )
        == "C1"
    )
    assert (
        _beef_item_course(
            {
                "category": "news",
                "source_id": "news/1",
            },
            info_map,
        )
        is None
    )
