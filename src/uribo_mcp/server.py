"""FastMCP tool surface for the independent uriboMCP data store."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta, timezone
from typing import Any, Literal

from uribo_mcp.config import Settings, load_settings
from uribo_mcp.db import database
from uribo_mcp.legacy import webdata
from uribo_mcp.repository import Repository, normalize_search

JST = timezone(timedelta(hours=9))
INSTRUCTIONS = """\
Personal, local-only Kobe University information server. Use only the configured
user's own account. Prefer cached data and call start_refresh only when freshness
is required; poll get_refresh_status instead of waiting. Never start, answer, or
submit a test, survey, attendance action, or assignment. Test/survey/attendance
records are metadata-only. Document text may be incomplete (OCR/password/limits);
preserve item, file, and page citations when summarizing.
"""


def _limit(settings: Settings, value: int | None) -> int:
    return max(
        1,
        min(
            int(value or settings.mcp.default_limit),
            settings.mcp.max_limit,
        ),
    )


def _resolve_course(repository: Repository, query: str | int) -> dict[str, Any]:
    course = repository.find_course(query)
    if not course:
        return {"found": False, "query": query}
    return {"found": True, "course": course}


def _public_legacy(item: dict[str, Any], *, include_body: bool = False) -> dict[str, Any]:
    names = (
        "id",
        "source",
        "source_label",
        "category",
        "cat_label",
        "importance",
        "title",
        "summary",
        "course_name",
        "url",
        "published_at",
        "deadline",
        "is_read",
        "is_done",
    )
    result = {name: item.get(name) for name in names}
    if include_body:
        result["body"] = item.get("body")
    if item.get("alts"):
        result["also_in"] = [
            alternate.get("source_label") for alternate in item["alts"]
        ]
    return result


def tool_get_today(settings: Settings, now: datetime | None = None) -> dict[str, Any]:
    now = now or datetime.now(JST)
    with database(settings.paths.db_path, readonly=True) as conn:
        timetable = webdata.get_timetable_today(conn, now)
        actions = [_public_legacy(item) for item in webdata.get_actions(conn)]
    due_today = []
    for item in actions:
        try:
            if item["deadline"] and webdata.parse_dt(item["deadline"]).date() == now.date():
                due_today.append(item)
        except (AttributeError, ValueError):
            continue
    return {
        "date": now.date().isoformat(),
        "weekday": webdata.DOW_JP[now.weekday()],
        "timetable": timetable,
        "actions": actions,
        "due_today": due_today,
    }


def tool_get_deadlines(
    settings: Settings,
    *,
    days_ahead: int | None = None,
    date_from: str | None = None,
    date_to: str | None = None,
    include_done: bool = False,
    now: datetime | None = None,
) -> dict[str, Any]:
    now = now or datetime.now(JST)
    if days_ahead is None and date_from is None and date_to is None:
        days_ahead = 14
    start = (
        datetime.fromisoformat(date_from)
        if date_from
        else datetime.combine(now.date(), datetime.min.time())
        if days_ahead is not None
        else None
    )
    end = (
        datetime.combine(datetime.fromisoformat(date_to).date(), datetime.max.time())
        if date_to
        else datetime.combine(
            now.date() + timedelta(days=max(0, int(days_ahead))),
            datetime.max.time(),
        )
        if days_ahead is not None
        else None
    )
    with database(settings.paths.db_path, readonly=True) as conn:
        rows = conn.execute(
            "SELECT * FROM items WHERE deadline IS NOT NULL AND deadline<>''"
        ).fetchall()
        items = []
        for row in rows:
            if not include_done and row["is_done"]:
                continue
            due = webdata.parse_dt(row["deadline"])
            if not due or (start and due < start) or (end and due > end):
                continue
            items.append(_public_legacy(webdata.map_item(row)))
    items.sort(key=lambda value: value.get("deadline") or "")
    return {
        "from": start.isoformat() if start else None,
        "to": end.isoformat() if end else None,
        "count": len(items),
        "deadlines": items,
    }


def tool_search_items(
    settings: Settings,
    *,
    query: str | None = None,
    source: str | None = None,
    category: str | None = None,
    unread_only: bool = False,
    limit: int | None = None,
) -> dict[str, Any]:
    result_limit = _limit(settings, limit)
    where = ["available=1"]
    parameters: list[Any] = []
    if source:
        where.append("source=?")
        parameters.append(source)
    if category:
        where.append("category=?")
        parameters.append(category)
    if unread_only:
        where.append("is_read=0")
    needle = normalize_search(query)
    output = []
    with database(settings.paths.db_path, readonly=True) as conn:
        rows = conn.execute(
            f"SELECT * FROM items WHERE {' AND '.join(where)} "
            "ORDER BY COALESCE(published_at, updated_at) DESC, id DESC",
            parameters,
        )
        for row in rows:
            haystack = normalize_search(
                " ".join(
                    filter(None, (row["title"], row["body"], row["course_name"]))
                )
            )
            if needle and needle not in haystack:
                continue
            output.append(_public_legacy(webdata.map_item(row)))
            if len(output) >= result_limit:
                break
    return {"count": len(output), "items": output}


def build_server(settings: Settings | None = None):
    """Build the server without opening browsers or importing scraper runtimes."""
    from mcp.server.fastmcp import FastMCP
    from mcp.types import ToolAnnotations

    settings = settings or load_settings()
    with database(settings.paths.db_path):
        pass
    server = FastMCP("uribo-mcp", instructions=INSTRUCTIONS)
    read_only = ToolAnnotations(
        readOnlyHint=True,
        destructiveHint=False,
        idempotentHint=True,
        openWorldHint=False,
    )
    local_write = ToolAnnotations(
        readOnlyHint=False,
        destructiveHint=False,
        idempotentHint=True,
        openWorldHint=False,
    )
    refresh_write = ToolAnnotations(
        readOnlyHint=False,
        destructiveHint=False,
        idempotentHint=False,
        openWorldHint=True,
    )

    @server.tool(annotations=read_only)
    def get_today() -> dict[str, Any]:
        """今日の時間割、未完了の要対応項目、今日締切を返す。"""
        return tool_get_today(settings)

    @server.tool(annotations=read_only)
    def get_deadlines(
        days_ahead: int | None = None,
        date_from: str | None = None,
        date_to: str | None = None,
        include_done: bool = False,
    ) -> dict[str, Any]:
        """期間内の締切を返す。既定は今日から14日。"""
        return tool_get_deadlines(
            settings,
            days_ahead=days_ahead,
            date_from=date_from,
            date_to=date_to,
            include_done=include_done,
        )

    @server.tool(annotations=read_only)
    def search_items(
        query: str | None = None,
        source: str | None = None,
        category: str | None = None,
        unread_only: bool = False,
        limit: int = 20,
    ) -> dict[str, Any]:
        """全ソースの項目をタイトル・本文・授業名から検索する。"""
        return tool_search_items(
            settings,
            query=query,
            source=source,
            category=category,
            unread_only=unread_only,
            limit=limit,
        )

    @server.tool(annotations=local_write)
    def mark_done(item_id: int, done: bool = True) -> dict[str, Any]:
        """ローカルDB上で項目を完了／未完了にする。LMSへの提出操作は行わない。"""
        with database(settings.paths.db_path) as conn:
            updated = webdata.set_done(conn, item_id, done)
            item = webdata.get_item(conn, item_id) if updated else None
        return {
            "found": updated,
            "id": item_id,
            "done": done,
            "item": _public_legacy(item) if item else None,
        }

    @server.tool(annotations=read_only)
    def get_unread(limit: int = 20) -> dict[str, Any]:
        """未読項目を重要度優先で返す。"""
        rank = {"action_required": 0, "relevant": 1, "info": 2, "uncat": 3}
        with database(settings.paths.db_path, readonly=True) as conn:
            items = [item for item in webdata.get_inbox(conn) if not item["is_read"]]
        items.sort(key=lambda item: str(item.get("published_at") or ""), reverse=True)
        items.sort(key=lambda item: rank.get(item["importance"], 9))
        output = [_public_legacy(item) for item in items[: _limit(settings, limit)]]
        return {"count": len(output), "items": output}

    @server.tool(annotations=read_only)
    def get_notes(
        course_name: str | None = None, limit: int = 50
    ) -> dict[str, Any]:
        """保存済みの授業メモを返す。"""
        with database(settings.paths.db_path, readonly=True) as conn:
            notes = (
                webdata.get_course_notes(conn, [course_name])
                if course_name
                else webdata.get_all_notes(conn)
            )
        output = notes[: _limit(settings, limit)]
        return {"course_name": course_name, "count": len(output), "notes": output}

    @server.tool(annotations=read_only)
    def get_news(limit: int = 20) -> dict[str, Any]:
        """神戸大学ニュースネットの記事を返す。"""
        with database(settings.paths.db_path, readonly=True) as conn:
            rows = conn.execute(
                """SELECT * FROM items WHERE source='newsnet' AND available=1
                   ORDER BY published_at DESC, id DESC LIMIT ?""",
                (_limit(settings, limit),),
            ).fetchall()
            items = [_public_legacy(webdata.map_item(row)) for row in rows]
        return {"count": len(items), "items": items}

    @server.tool(annotations=read_only)
    def get_item(item_id: int) -> dict[str, Any]:
        """項目本文と添付一覧を返す。"""
        with database(settings.paths.db_path, readonly=True) as conn:
            item = Repository(conn).get_item(item_id)
        return {"found": bool(item), "item": item}

    @server.tool(annotations=read_only)
    def get_week() -> dict[str, Any]:
        """今週（月〜土）の時間割を返す。"""
        with database(settings.paths.db_path, readonly=True) as conn:
            return webdata.get_timetable_week(conn, datetime.now(JST))

    @server.tool(annotations=read_only)
    def get_course(
        course_name: str, announcement_limit: int = 10
    ) -> dict[str, Any]:
        """指定授業の要対応、お知らせ、メモ、LMSリンクを返す。"""
        with database(settings.paths.db_path, readonly=True) as conn:
            result = webdata.get_course_related(
                conn,
                [course_name],
                announcement_limit=min(max(1, announcement_limit), 30),
            )
        result["open_actionable"] = [
            _public_legacy(item) for item in result["open_actionable"]
        ]
        result["announcements"] = [
            _public_legacy(item) for item in result["announcements"]
        ]
        return {"course_name": course_name, **result}

    @server.tool(annotations=read_only)
    def get_source_health() -> dict[str, Any]:
        """ソースごとの直近同期結果を返す。"""
        with database(settings.paths.db_path, readonly=True) as conn:
            sources = webdata.get_source_health(conn)
        return {
            "as_of_utc": datetime.now(UTC).isoformat(),
            "sources": sources,
            "summary": webdata.health_summary(sources),
        }

    @server.tool(annotations=read_only)
    def list_courses(
        available_only: bool = True, fields: Literal["summary", "full"] = "summary"
    ) -> dict[str, Any]:
        """同期済みの授業一覧を返す。

        fields="summary"（既定）は id/source/idnumber/course_code/course_name/
        academic_year/term/schedule_text/available のみ。instructors・summary・
        metadata 等が必要なら fields="full"。
        """
        with database(settings.paths.db_path, readonly=True) as conn:
            courses = Repository(conn).list_courses(
                available_only=available_only, fields=fields
            )
        return {"count": len(courses), "courses": courses}

    @server.tool(annotations=read_only)
    def list_course_content(
        course: str,
        kinds: list[str] | None = None,
        available_only: bool = True,
        limit: int = 100,
        fields: Literal["summary", "full"] = "summary",
    ) -> dict[str, Any]:
        """授業内の教材・お知らせ・課題・テスト等のメタデータ一覧を返す。

        fields="summary"（既定）は id/source/category/title/url/course_name/
        course_id/published_at/deadline/is_done/available/updated_at/
        attachment_count のみ。metadata・content_hash・各種タイムスタンプが必要なら
        fields="full" か get_course_content(item_id)。
        """
        with database(settings.paths.db_path, readonly=True) as conn:
            repository = Repository(conn)
            resolved = _resolve_course(repository, course)
            if not resolved["found"]:
                return resolved
            items = repository.list_course_content(
                resolved["course"]["id"],
                kinds=kinds,
                available_only=available_only,
                limit=_limit(settings, limit),
                fields=fields,
            )
        return {**resolved, "count": len(items), "items": items}

    @server.tool(annotations=read_only)
    def get_course_content(item_id: int) -> dict[str, Any]:
        """授業コンテンツ1件の本文・期限・状態・添付を返す。"""
        with database(settings.paths.db_path, readonly=True) as conn:
            item = Repository(conn).get_item(item_id)
        return {"found": bool(item), "item": item}

    @server.tool(annotations=read_only)
    def search_course_content(
        query: str,
        course: str | None = None,
        kinds: list[str] | None = None,
        limit: int = 20,
        include_text: bool = False,
    ) -> dict[str, Any]:
        """授業本文と抽出済みPDF／Office文書を横断検索する。

        複数語のクエリは全語を含む結果を優先する（match_mode="all"）。全語一致が
        0件のときは語ごとの一致に自動で切り替え（match_mode="any"）、一致語数→
        スコア順で返す。各結果の tokens_matched と tokens_unmatched（どの資料にも
        無い語）を見て、語を削って再検索する前に判断すること。

        文書結果は既定で snippet（一致語を中心に約300文字）と text_chars のみを
        返す。全文が必要なら include_text=true か、attachment_id と
        page_start/page_end を read_document に渡す。
        """
        with database(settings.paths.db_path, readonly=True) as conn:
            repository = Repository(conn)
            course_id = None
            selected = None
            if course:
                selected = repository.find_course(course)
                if not selected:
                    return {"found": False, "course": course, "results": []}
                course_id = selected["id"]
            search = repository.search_course_content_detail(
                query,
                course_id=course_id,
                kinds=kinds,
                limit=_limit(settings, limit),
                include_text=include_text,
            )
        results = search["results"]
        return {
            "found": True,
            "course": selected,
            "count": len(results),
            "match_mode": search["match_mode"],
            "tokens": search["tokens"],
            "tokens_unmatched": search["tokens_unmatched"],
            "results": results,
        }

    @server.tool(annotations=read_only)
    def read_document(
        attachment_id: int,
        page_from: int | None = None,
        page_to: int | None = None,
        max_chars: int | None = None,
    ) -> dict[str, Any]:
        """教材ファイルの抽出済み本文をページ範囲つきで返す。"""
        bounded = min(
            max(1, int(max_chars or settings.mcp.max_text_chars)),
            settings.mcp.max_text_chars,
        )
        with database(settings.paths.db_path, readonly=True) as conn:
            return Repository(conn).read_document(
                attachment_id,
                page_from=page_from,
                page_to=page_to,
                max_chars=bounded,
            )

    @server.tool(annotations=read_only)
    def get_changes(
        since: str | None = None,
        course: str | None = None,
        limit: int = 100,
    ) -> dict[str, Any]:
        """新規・更新・非公開化された授業コンテンツの履歴を返す。"""
        with database(settings.paths.db_path, readonly=True) as conn:
            repository = Repository(conn)
            course_id = None
            if course:
                selected = repository.find_course(course)
                if not selected:
                    return {"found": False, "course": course, "changes": []}
                course_id = selected["id"]
            changes = repository.get_changes(
                since=since,
                course_id=course_id,
                limit=_limit(settings, limit),
            )
        return {"found": True, "count": len(changes), "changes": changes}

    @server.tool(annotations=read_only)
    def get_notifications(
        acknowledged: bool | None = False, limit: int = 100
    ) -> dict[str, Any]:
        """教材・お知らせ・課題等の更新通知を返す。"""
        with database(settings.paths.db_path, readonly=True) as conn:
            notifications = Repository(conn).get_notifications(
                acknowledged=acknowledged,
                limit=_limit(settings, limit),
            )
        return {"count": len(notifications), "notifications": notifications}

    @server.tool(annotations=local_write)
    def acknowledge_notifications(notification_ids: list[int]) -> dict[str, Any]:
        """更新通知をローカルで確認済みにする。"""
        with database(settings.paths.db_path) as conn:
            count = Repository(conn).acknowledge_notifications(notification_ids)
        return {"acknowledged": count, "notification_ids": notification_ids}

    @server.tool(annotations=read_only)
    def build_study_context(
        course: str,
        query: str,
        limit: int = 12,
        max_chars: int = 20000,
    ) -> dict[str, Any]:
        """授業内検索から、ページ引用つきの学習用根拠パックを構築する。"""
        with database(settings.paths.db_path, readonly=True) as conn:
            repository = Repository(conn)
            selected = repository.find_course(course)
            if not selected:
                return {"found": False, "course": course, "evidence": []}
            result = repository.build_study_context(
                query,
                course_id=selected["id"],
                limit=_limit(settings, limit),
                max_chars=min(max(1, max_chars), settings.mcp.max_text_chars * 2),
            )
        return {"found": True, **result}

    @server.tool(annotations=local_write)
    def export_materials(
        course: str, item_ids: list[int] | None = None
    ) -> dict[str, Any]:
        """教材を設定済みexport_root配下へ授業／項目別に整理コピーする。"""
        with database(settings.paths.db_path) as conn:
            repository = Repository(conn)
            selected = repository.find_course(course)
            if not selected:
                return {"found": False, "course": course, "files": []}
            result = repository.export_materials(
                course_id=selected["id"],
                export_root=settings.paths.export_root,
                item_ids=item_ids,
            )
        return {"found": True, **result}

    @server.tool(annotations=refresh_write)
    def start_refresh(
        scope: str = "all", course_ids: list[str] | None = None
    ) -> dict[str, Any]:
        """同期をバックグラウンド開始してjob IDを返す。テスト等の操作は行わない。"""
        from uribo_mcp.sync import recover_stale_jobs
        from uribo_mcp.sync import start_refresh as start

        recover_stale_jobs(settings)
        return start(settings, scope=scope, course_ids=course_ids)

    @server.tool(annotations=read_only)
    def get_refresh_status(job_id: str | None = None) -> dict[str, Any]:
        """バックグラウンド同期の進捗／結果を返す。"""
        from uribo_mcp.sync import get_refresh_status as status

        return status(settings, job_id)

    return server


def run_server(settings: Settings | None = None) -> None:
    build_server(settings).run(transport="stdio")


__all__ = [
    "build_server",
    "run_server",
    "tool_get_deadlines",
    "tool_get_today",
    "tool_search_items",
]
