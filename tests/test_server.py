import asyncio
import os
import sys
from pathlib import Path

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

from uribo_mcp.config import load_settings
from uribo_mcp.db import database
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


def test_real_stdio_handshake_lists_tools(tmp_path: Path) -> None:
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

    asyncio.run(run())
