import asyncio
import os
import sys
from contextlib import contextmanager
from pathlib import Path

import pytest
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

from uribo_mcp.config import load_settings
from uribo_mcp.db import database
from uribo_mcp.models import AttachmentRecord, CourseRecord, ItemRecord
from uribo_mcp.repository import Repository
from uribo_mcp.server import (
    build_server,
    tool_get_deadlines,
    tool_get_today,
    tool_search_items,
)

EXPECTED_TOOLS = {
    "get_today",
    "get_deadlines",
    "search_items",
    "mark_done",
    "get_unread",
    "get_notes",
    "get_news",
    "get_item",
    "get_week",
    "get_course",
    "get_source_health",
    "list_courses",
    "list_course_content",
    "get_course_content",
    "search_course_content",
    "read_document",
    "get_changes",
    "get_notifications",
    "acknowledge_notifications",
    "build_study_context",
    "export_materials",
    "start_refresh",
    "get_refresh_status",
}


def test_server_registers_legacy_and_new_tools(tmp_path: Path) -> None:
    settings = load_settings(root=tmp_path)
    server = build_server(settings)
    tools = asyncio.run(server.list_tools())
    assert {tool.name for tool in tools} == EXPECTED_TOOLS
    annotations = {tool.name: tool.annotations for tool in tools}
    assert annotations["read_document"].readOnlyHint
    assert annotations["mark_done"].destructiveHint is False
    assert annotations["start_refresh"].openWorldHint


def test_legacy_tool_logic_uses_unified_database(tmp_path: Path) -> None:
    settings = load_settings(root=tmp_path)
    with database(settings.paths.db_path) as connection:
        connection.execute(
            """INSERT INTO items
               (source,source_id,category,title,deadline,importance,
                first_seen_at,last_seen_at,updated_at)
               VALUES ('beef','assignment/C1/1','assignment','課題1',
                       '2026-08-01T10:00:00','action_required',
                       datetime('now'),datetime('now'),datetime('now'))"""
        )
        connection.execute(
            """INSERT INTO timetable(day_of_week,period,course_name,room,idnumber)
               VALUES (1,1,'解析学','A101','C1')"""
        )
        connection.commit()
    assert tool_search_items(settings, query="課題")["count"] == 1
    assert tool_get_deadlines(
        settings, date_from="2026-08-01", date_to="2026-08-01"
    )["count"] == 1
    today = tool_get_today(settings)
    assert "timetable" in today and "actions" in today


READ_ONLY_CALLS: dict[str, dict[str, object]] = {
    "get_today": {},
    "get_deadlines": {"days_ahead": 30},
    "search_items": {"query": "課題"},
    "get_unread": {},
    "get_notes": {},
    "get_news": {},
    "get_item": {"item_id": 1},
    "get_week": {},
    "get_course": {"course_name": "解析学"},
    "get_source_health": {},
    "list_courses": {},
    "list_course_content": {"course": "解析学"},
    "get_course_content": {"item_id": 1},
    "search_course_content": {"query": "課題"},
    "read_document": {"attachment_id": 1},
    "get_changes": {},
    "get_notifications": {},
    "build_study_context": {"course": "解析学", "query": "課題"},
    "get_refresh_status": {},
}


def test_read_only_tools_do_not_write(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    settings = load_settings(root=tmp_path)
    with database(settings.paths.db_path) as connection:
        repository = Repository(connection)
        course = repository.upsert_course(
            CourseRecord(source="beef", idnumber="C1", course_name="解析学")
        )
        item = repository.upsert_item(
            ItemRecord(
                source="beef",
                source_id="assignment/C1/1",
                category="assignment",
                title="課題1",
                body="課題の説明",
                course_name="解析学",
                course_id=course.id,
                deadline="2026-08-01T10:00:00",
            )
        )
        attachment = repository.upsert_attachment(
            AttachmentRecord(
                stable_key="file/1", file_name="notes.txt", item_id=item.id, course_id=course.id
            )
        )
        repository.replace_document_text(
            attachment.id,
            [{"page_number": 1, "text": "課題の本文", "extraction_method": "native"}],
            [{"chunk_index": 0, "page_start": 1, "page_end": 1, "text": "課題の本文"}],
            status="ok",
            method="native",
        )
        connection.execute(
            "INSERT INTO timetable(day_of_week,period,course_name,room,idnumber) "
            "VALUES (1,1,'解析学','A101','C1')"
        )
        connection.commit()
        connection.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    assert set(READ_ONLY_CALLS) <= EXPECTED_TOOLS
    server = build_server(settings)
    wal = settings.paths.db_path.with_name(settings.paths.db_path.name + "-wal")
    wal_before = wal.stat().st_size if wal.exists() else 0

    opened: list[tuple[str, bool]] = []
    current: list[str] = []

    @contextmanager
    def spy(path: Path, *, readonly: bool = False):
        opened.append((current[-1], readonly))
        with database(path, readonly=readonly) as conn:
            assert conn.execute("PRAGMA query_only").fetchone()[0] == int(readonly)
            yield conn

    monkeypatch.setattr("uribo_mcp.server.database", spy)
    monkeypatch.setattr("uribo_mcp.sync.database", spy)

    async def run() -> None:
        for name, arguments in READ_ONLY_CALLS.items():
            current.append(name)
            await server.call_tool(name, arguments)

    asyncio.run(run())
    assert {name for name, _ in opened} == set(READ_ONLY_CALLS)
    assert all(readonly for _, readonly in opened), opened
    assert (wal.stat().st_size if wal.exists() else 0) == wal_before


def _seed_long_document(settings) -> None:
    with database(settings.paths.db_path) as connection:
        repository = Repository(connection)
        course = repository.upsert_course(
            CourseRecord(source="beef", idnumber="C1", course_name="数値解析")
        )
        item = repository.upsert_item(
            ItemRecord(
                source="beef",
                source_id="material/C1/1",
                category="material",
                title="第3回 誤差",
                course_name="数値解析",
                course_id=course.id,
                metadata={"attachment_manifest": [{"file_name": "a.pdf"}]},
            )
        )
        attachment = repository.upsert_attachment(
            AttachmentRecord(
                stable_key="file/1", file_name="a.pdf", item_id=item.id, course_id=course.id
            )
        )
        text = ("前" * 400) + "丸め誤差と打ち切り誤差" + ("後" * 400)
        repository.replace_document_text(
            attachment.id,
            [{"page_number": 1, "text": text, "extraction_method": "native"}],
            [{"chunk_index": 0, "page_start": 1, "page_end": 1, "text": text}],
            status="ok",
            method="native",
        )


def test_real_stdio_handshake_lists_tools(tmp_path: Path) -> None:
    settings = load_settings(root=tmp_path)
    _seed_long_document(settings)

    async def run() -> None:
        parameters = StdioServerParameters(
            command=sys.executable,
            args=["-m", "uribo_mcp", "--root", str(tmp_path), "serve"],
            env=os.environ.copy(),
        )
        async with stdio_client(parameters) as (reader, writer):
            async with ClientSession(reader, writer) as session:
                await session.initialize()
                tools = await session.list_tools()
                assert {tool.name for tool in tools.tools} == EXPECTED_TOOLS
                schemas = {tool.name: tool.inputSchema["properties"] for tool in tools.tools}
                assert schemas["search_course_content"]["include_text"]["default"] is False
                assert schemas["list_course_content"]["fields"]["default"] == "summary"
                assert schemas["list_courses"]["fields"]["default"] == "summary"

                slim = await session.call_tool(
                    "search_course_content", {"query": "誤差 打ち切り", "limit": 5}
                )
                assert slim.structuredContent is not None
                payload = slim.structuredContent
                assert payload["match_mode"] == "all"
                assert payload["tokens"] == ["誤差", "打ち切り"]
                assert payload["tokens_unmatched"] == []
                document = payload["results"][0]
                assert document["result_type"] == "document"
                assert "text" not in document
                assert "誤差" in document["snippet"]
                assert document["text_chars"] == 811
                assert document["tokens_matched"] == ["誤差", "打ち切り"]

                full = await session.call_tool(
                    "search_course_content",
                    {"query": "誤差", "limit": 5, "include_text": True},
                )
                assert len(full.structuredContent["results"][0]["text"]) == 811

                any_mode = await session.call_tool(
                    "search_course_content", {"query": "誤差 隤週", "limit": 5}
                )
                assert any_mode.structuredContent["match_mode"] == "any"
                assert any_mode.structuredContent["tokens_unmatched"] == ["隤週"]

                summary = await session.call_tool(
                    "list_course_content", {"course": "数値解析", "limit": 10}
                )
                item = summary.structuredContent["items"][0]
                assert "metadata" not in item and "content_hash" not in item
                assert item["attachment_count"] == 1
                full_items = await session.call_tool(
                    "list_course_content",
                    {"course": "数値解析", "limit": 10, "fields": "full"},
                )
                assert "metadata" in full_items.structuredContent["items"][0]

                courses = await session.call_tool("list_courses", {})
                course = courses.structuredContent["courses"][0]
                assert set(course) == {
                    "id",
                    "source",
                    "idnumber",
                    "course_code",
                    "course_name",
                    "academic_year",
                    "term",
                    "schedule_text",
                    "available",
                }
                full_courses = await session.call_tool("list_courses", {"fields": "full"})
                assert "instructors" in full_courses.structuredContent["courses"][0]

                context = await session.call_tool(
                    "build_study_context", {"course": "数値解析", "query": "誤差"}
                )
                evidence = context.structuredContent["evidence"][0]
                assert len(evidence["text"]) == 811
                assert context.structuredContent["match_mode"] == "all"

    asyncio.run(run())
