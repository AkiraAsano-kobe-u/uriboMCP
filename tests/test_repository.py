from pathlib import Path

from uribo_mcp.db import database
from uribo_mcp.models import AttachmentRecord, CourseRecord, ItemRecord
from uribo_mcp.repository import Repository


def test_repository_tracks_changes_searches_documents_and_exports(tmp_path: Path) -> None:
    db_path = tmp_path / "data.db"
    source_file = tmp_path / "source.txt"
    source_file.write_text("線形代数の固有値について", encoding="utf-8")
    with database(db_path) as connection:
        repository = Repository(connection)
        course = repository.upsert_course(
            CourseRecord(source="beef", idnumber="C1", course_name="線形代数")
        )
        item = repository.upsert_item(
            ItemRecord(
                source="beef",
                source_id="material/C1/1",
                category="material",
                title="第1回資料",
                course_name="線形代数",
                course_id=course.id,
            )
        )
        assert item.created
        changed = repository.upsert_item(
            ItemRecord(
                source="beef",
                source_id="material/C1/1",
                category="material",
                title="第1回資料（改訂）",
                course_name="線形代数",
                course_id=course.id,
            )
        )
        assert "title" in changed.changed_fields
        attachment = repository.upsert_attachment(
            AttachmentRecord(
                stable_key="file/1",
                file_name="notes.txt",
                item_id=item.id,
                course_id=course.id,
            )
        )
        repository.replace_document_text(
            attachment.id,
            [
                {
                    "page_number": 1,
                    "text": "線形代数の固有値について",
                    "extraction_method": "native",
                }
            ],
            [
                {
                    "chunk_index": 0,
                    "page_start": 1,
                    "page_end": 1,
                    "text": "線形代数の固有値について",
                }
            ],
            status="ok",
            method="native",
            sha256=Repository._sha256_file(source_file),
            size_bytes=source_file.stat().st_size,
            local_path=str(source_file),
        )

        search = repository.search_course_content("固有値", course_id=course.id)
        assert search[0]["result_type"] == "document"
        assert (
            repository.search_course_content(
                "固有値", course_id=course.id, kinds=["assignment"]
            )
            == []
        )
        document = repository.read_document(attachment.id)
        assert document["pages"][0]["text"] == "線形代数の固有値について"
        context = repository.build_study_context("固有値", course_id=course.id)
        assert context["evidence"][0]["citation"]["page_start"] == 1
        changes = repository.get_changes(course_id=course.id)
        assert {change["change_type"] for change in changes} == {"created", "updated"}
        assert len(repository.get_notifications()) == 2
        exported = repository.export_materials(
            course_id=course.id, export_root=tmp_path / "exports"
        )
        assert Path(exported["files"][0]["path"]).read_text(encoding="utf-8")


def test_repository_only_marks_missing_after_explicit_snapshot(tmp_path: Path) -> None:
    with database(tmp_path / "data.db") as connection:
        repository = Repository(connection)
        item = repository.upsert_item(
            ItemRecord(
                source="csi",
                source_id="old",
                category="announcement",
                title="old",
            )
        )
        assert repository.mark_items_missing(source="csi", seen_source_ids=[]) == 1
        assert repository.get_item(item.id)["available"] is False


def test_unchanged_upserts_do_not_write_to_database(tmp_path: Path) -> None:
    with database(tmp_path / "data.db") as connection:
        repository = Repository(connection)
        course_record = CourseRecord(
            source="beef", idnumber="C1", course_name="線形代数"
        )
        course = repository.upsert_course(course_record)
        item_record = ItemRecord(
            source="beef",
            source_id="material/C1/1",
            category="material",
            title="第1回資料",
            course_name="線形代数",
            course_id=course.id,
        )
        item = repository.upsert_item(item_record)
        attachment_record = AttachmentRecord(
            stable_key="file/1",
            file_name="notes.txt",
            item_id=item.id,
            course_id=course.id,
            object_name="immutable/object-id",
        )
        repository.upsert_attachment(attachment_record)
        changes_before = connection.total_changes

        assert not repository.upsert_course(course_record).changed_fields
        assert not repository.upsert_item(item_record).changed_fields
        assert not repository.upsert_attachment(attachment_record).changed_fields

        assert connection.total_changes == changes_before


def test_public_item_strips_object_name_from_attachment_manifest(tmp_path: Path) -> None:
    with database(tmp_path / "data.db") as connection:
        repository = Repository(connection)
        course = repository.upsert_course(
            CourseRecord(source="beef", idnumber="C1", course_name="線形代数")
        )
        item = repository.upsert_item(
            ItemRecord(
                source="beef",
                source_id="material/C1/1",
                category="material",
                title="第1回資料",
                course_id=course.id,
                metadata={
                    "attachment_manifest": [
                        {
                            "file_name": "notes.pdf",
                            "object_name": "internal/download/recipe",
                            "size_bytes": 10,
                        },
                        "not-a-dict",
                    ],
                    "other": {"object_name": "kept: only manifest entries are filtered"},
                },
            )
        )
        public = repository.get_item(item.id)
        listed = repository.list_course_content(course.id, fields="full")
        searched = repository.search_course_content("第1回", course_id=course.id)
    assert public["metadata"]["attachment_manifest"] == [
        {"file_name": "notes.pdf", "size_bytes": 10},
        "not-a-dict",
    ]
    assert public["metadata"]["other"]["object_name"]
    assert "object_name" not in listed[0]["metadata"]["attachment_manifest"][0]
    assert "object_name" not in searched[0]["item"]["metadata"]["attachment_manifest"][0]
    # The stored row is untouched; only the public view is filtered.
    with database(tmp_path / "data.db", readonly=True) as connection:
        raw = connection.execute("SELECT metadata_json FROM items WHERE id=?", (item.id,)).fetchone()
    assert "internal/download/recipe" in raw[0]
