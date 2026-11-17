from pathlib import Path

from uribo_mcp.config import load_settings
from uribo_mcp.course.client import DownloadedFile
from uribo_mcp.db import database
from uribo_mcp.ingest import CourseIngestor
from uribo_mcp.repository import Repository

COURSE_HTML = """
<h1 class="course-title">解析学</h1>
<article class="contents-material" data-module="material" data-content-id="1">
  <a class="material-title" href="/lms/course/material?idnumber=C1&materialId=1">
    講義資料
  </a>
  <a href="/lms/course/download?id=1" download>notes.txt</a>
</article>
<article class="contents-item" data-module="test" data-content-id="2">
  <a href="/lms/course/test/start?idnumber=C1&testId=2">小テスト</a>
  <span>期限：2026/08/01 10:00 未受験</span>
</article>
"""


class FakeClient:
    base_url = "https://beefplus.center.kobe-u.ac.jp"

    def __init__(self) -> None:
        self.detail_calls = 0
        self.download_calls = 0

    def fetch_course(self, idnumber: str) -> str:
        return COURSE_HTML

    def fetch_assignment_detail(self, url: str) -> str:
        self.detail_calls += 1
        raise AssertionError("test metadata URL must never be opened")

    def fetch_announcement_detail(self, **kwargs) -> str:
        self.detail_calls += 1
        raise AssertionError("test metadata URL must never be opened")

    def download_attachment(
        self, attachment, *, destination_dir: Path, max_bytes: int
    ):
        self.download_calls += 1
        destination_dir.mkdir(parents=True, exist_ok=True)
        path = destination_dir / "notes.txt"
        path.write_text("解析学の教材本文", encoding="utf-8")
        return DownloadedFile(
            path=path,
            file_name="notes.txt",
            media_type="text/plain",
            size_bytes=path.stat().st_size,
            response_url="https://beefplus.center.kobe-u.ac.jp/lms/course/download?id=1",
        )


def test_course_ingestor_extracts_documents_but_never_opens_tests(
    tmp_path: Path,
) -> None:
    settings = load_settings(root=tmp_path)
    client = FakeClient()
    with database(settings.paths.db_path) as connection:
        result = CourseIngestor(
            settings=settings,
            repository=Repository(connection),
            client=client,
            pause=lambda: None,
        ).sync_course(idnumber="C1")
        assert result["items_created"] == 2
        assert result["attachments_downloaded"] == 1
        assert client.detail_calls == 0
        test = connection.execute(
            "SELECT body,metadata_json FROM items WHERE category='test'"
        ).fetchone()
        assert test["body"] is None
        assert "metadata_only" in test["metadata_json"]
        assert connection.execute("SELECT COUNT(*) FROM document_chunks").fetchone()[0] == 1


def test_course_ingestor_reuses_unchanged_cached_attachment(
    tmp_path: Path,
) -> None:
    settings = load_settings(root=tmp_path)
    client = FakeClient()
    pauses: list[None] = []
    with database(settings.paths.db_path) as connection:
        ingestor = CourseIngestor(
            settings=settings,
            repository=Repository(connection),
            client=client,
            pause=lambda: pauses.append(None),
        )
        first = ingestor.sync_course(idnumber="C1")
        local_path = Path(
            connection.execute(
                "SELECT local_path FROM attachments WHERE available=1"
            ).fetchone()["local_path"]
        )
        first_mtime = local_path.stat().st_mtime_ns

        second = ingestor.sync_course(idnumber="C1")

        assert first["attachments_downloaded"] == 1
        assert first["attachments_reused"] == 0
        assert second["attachments_downloaded"] == 0
        assert second["attachments_reused"] == 1
        assert client.download_calls == 1
        assert len(pauses) == 1
        assert local_path.stat().st_mtime_ns == first_mtime

        attachment = connection.execute(
            "SELECT stable_key,item_id,course_id FROM attachments WHERE available=1"
        ).fetchone()
        changed = ingestor.sync_item_attachments(
            item_id=attachment["item_id"],
            course_id=attachment["course_id"],
            idnumber="C1",
            attachments=[
                {
                    "stable_id": attachment["stable_key"],
                    "name": "notes.txt",
                    "object_name": "new/immutable/object-id",
                }
            ],
        )
        assert changed["downloaded"] == 1
        assert changed["reused"] == 0
        assert client.download_calls == 2


def test_course_ingestor_keeps_known_name_when_page_title_is_idnumber(
    tmp_path: Path,
) -> None:
    settings = load_settings(root=tmp_path)
    client = FakeClient()
    client.fetch_course = lambda idnumber: COURSE_HTML.replace(
        '<h1 class="course-title">解析学</h1>',
        '<h1 class="course-title">C1</h1>',
    )
    with database(settings.paths.db_path) as connection:
        CourseIngestor(
            settings=settings,
            repository=Repository(connection),
            client=client,
            pause=lambda: None,
        ).sync_course(idnumber="C1", known_name="解析学")
        course = connection.execute(
            "SELECT course_name FROM courses WHERE idnumber='C1'"
        ).fetchone()
        assert course["course_name"] == "解析学"


class NoDeadlineTestClient(FakeClient):
    def fetch_course(self, idnumber: str) -> str:
        return """
<h1 class="course-title">解析学</h1>
<article class="contents-item" data-module="test" data-content-id="5">
  <a href="/lms/course/test/start?idnumber=C1&testId=5">小テスト2</a>
</article>
"""


def test_task_list_deadline_fills_course_content_without_one(tmp_path: Path) -> None:
    settings = load_settings(root=tmp_path)
    with database(settings.paths.db_path) as connection:
        CourseIngestor(
            settings=settings,
            repository=Repository(connection),
            client=NoDeadlineTestClient(),
            pause=lambda: None,
        ).sync_course(
            idnumber="C1",
            known_deadlines={"test/C1/5": "2026-12-18T23:59:00"},
        )
        row = connection.execute(
            "SELECT deadline FROM items WHERE source_id='test/C1/5'"
        ).fetchone()
        assert row["deadline"] == "2026-12-18T23:59:00"


class AssignmentClient(FakeClient):
    def __init__(self) -> None:
        super().__init__()
        self.end = "2026/11/24 10:40"
        self.status = "未提出"

    def fetch_course(self, idnumber: str) -> str:
        return f"""
<h1 class="course-title">解析学</h1>
<div class="course-result-list contents-display-flex sortReportBlock clearfix">
  <a class="course-view-report-name reportTitle" href="/lms/course/report/submission?idnumber=C1&amp;reportId=7">自習シート</a>
  <div class="course-view-report-time-start timeStart">2026/11/17 11:40</div>
  <div class="course-view-report-time-end timeEnd">{self.end}</div>
  <div class="course-view-report-status submitStatus"><label>{self.status}</label></div>
</div>
"""

    def fetch_assignment_detail(self, url: str) -> str:
        self.detail_calls += 1
        return "<div id='contents'><h2 class='contents-title'>自習シート</h2><div class='body'>問題1</div></div>"


def test_unchanged_course_rows_do_not_reopen_detail_pages(tmp_path: Path) -> None:
    settings = load_settings(root=tmp_path)
    client = AssignmentClient()
    with database(settings.paths.db_path) as connection:
        ingestor = CourseIngestor(
            settings=settings,
            repository=Repository(connection),
            client=client,
            pause=lambda: None,
        )
        first = ingestor.sync_course(idnumber="C1", skip_unchanged_details=True)
        assert (first["details_fetched"], client.detail_calls) == (1, 1)

        second = ingestor.sync_course(idnumber="C1", skip_unchanged_details=True)
        assert (second["details_skipped"], client.detail_calls) == (1, 1)
        row = connection.execute(
            "SELECT body,deadline,available FROM items WHERE source_id='assignment/C1/7'"
        ).fetchone()
        assert row["body"] == "問題1"
        assert row["deadline"] == "2026-11-24T10:40"
        assert row["available"] == 1

        client.status = "期限内提出"
        third = ingestor.sync_course(idnumber="C1", skip_unchanged_details=True)
        assert (third["details_fetched"], client.detail_calls) == (1, 2)

        full = ingestor.sync_course(idnumber="C1", skip_unchanged_details=False)
        assert (full["details_fetched"], client.detail_calls) == (1, 3)
