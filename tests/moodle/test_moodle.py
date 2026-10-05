import json
from datetime import date
from pathlib import Path

import pytest

from uribo_mcp.config import load_settings
from uribo_mcp.course.client import DownloadedFile
from uribo_mcp.db import database
from uribo_mcp.moodle.client import MoodleReadOnlyClient, academic_year
from uribo_mcp.moodle.parser import (
    calendar_deadlines,
    parse_course_page,
    parse_enrolled_course,
    parse_japanese_datetime,
)
from uribo_mcp.moodle.sync import MoodleSynchronizer, attachment_key
from uribo_mcp.repository import Repository

BASE = "https://beef.center.kobe-u.ac.jp/2026"

# Shape copied from a live Moodle 4.5 course page (classic theme), text replaced.
COURSE_HTML = f"""
<ul class="topics">
<li id="section-0" class="section course-section main clearfix" data-sectionid="0"
    data-for="section" data-id="1" data-number="0" data-sectionname="授業概要">
  <div class="summarytext"><p>授業のテーマ モデル</p></div>
  <ul class="section">
  <li class="activity activity-wrapper forum modtype_forum" data-for="cmitem" data-id="900" id="module-900">
    <div class="activity-item" data-activityname="アナウンスメント">
      <div class="activityname"><a class="aalink stretched-link" href="{BASE}/mod/forum/view.php?id=900">
        <span class="instancename">アナウンスメント <span class="accesshide">フォーラム</span></span></a></div>
    </div>
  </li>
  </ul>
</li>
<li id="section-1" class="section course-section main clearfix" data-sectionid="1"
    data-for="section" data-id="2" data-number="1" data-sectionname="第1回講義（10月5日）">
  <ul class="section">
  <li class="activity activity-wrapper resource modtype_resource" data-for="cmitem" data-id="101" id="module-101">
    <div class="activity-item" data-activityname="第1回講義資料">
      <div class="activityname"><a class="aalink stretched-link" href="{BASE}/mod/resource/view.php?id=101">
        <span class="instancename">第1回講義資料 <span class="accesshide">ファイル</span></span></a>
        <span class="activitybadge badge">PDF</span></div>
    </div>
  </li>
  <li class="activity activity-wrapper quiz modtype_quiz" data-for="cmitem" data-id="102" id="module-102">
    <div class="activity-item" data-activityname="第1回 小テスト">
      <div class="activityname"><a class="aalink stretched-link" href="{BASE}/mod/quiz/view.php?id=102">
        <span class="instancename">第1回 小テスト</span></a></div>
    </div>
  </li>
  <li class="activity activity-wrapper label modtype_label" data-for="cmitem" data-id="103" id="module-103">
    <div class="activity-item activityinline" data-activityname="授業内容の概要">
      <div class="activity-altcontent text-break"><div class="no-overflow">
        <ol><li>授業内容の概要</li><li>OSと計算機</li></ol>
        <a href="{BASE}/pluginfile.php/7/mod_label/intro/handout.pdf">配布資料</a>
      </div></div>
    </div>
  </li>
  <li class="activity activity-wrapper assign modtype_assign" data-for="cmitem" data-id="104" id="module-104">
    <div class="activity-item" data-activityname="レポート1">
      <div class="activityname"><a class="aalink stretched-link" href="{BASE}/mod/assign/view.php?id=104">
        <span class="instancename">レポート1 <span class="accesshide">課題</span></span></a></div>
    </div>
  </li>
  <li class="activity activity-wrapper resource modtype_resource" data-for="cmitem" data-id="105" id="module-105">
    <div class="activity-item" data-activityname="実験動画">
      <div class="activityname"><a class="aalink stretched-link" href="{BASE}/mod/resource/view.php?id=105">
        <span class="instancename">実験動画</span></a></div>
    </div>
  </li>
  </ul>
</li>
</ul>
"""

ASSIGN_HTML = """
<div id="region-main"><div role="main">
  <div class="activity-header"><div data-region="activity-dates" class="activity-dates">
    <div><strong>開始日時:</strong> 2026年 10月 5日(月曜日) 00:00</div>
    <div><strong>期限:</strong> 2026年 10月 19日(月曜日) 23:59</div>
  </div>
  <div class="activity-description" id="intro"><p>第1回のレポートを提出すること。</p>
    <a href="https://beef.center.kobe-u.ac.jp/2026/pluginfile.php/9/mod_assign/introattachment/0/template.docx">template.docx</a>
  </div></div>
  <table class="generaltable submissionstatustable"><tbody>
    <tr><th>提出ステータス</th><td>未提出</td></tr>
  </tbody></table>
</div></div>
"""

FORUM_HTML = f"""
<div id="region-main"><table><tbody>
<tr class="discussion" data-region="discussion-list-item" data-discussionid="55">
  <th><a href="{BASE}/mod/forum/discuss.php?d=55">休講のお知らせ</a></th>
  <td><time data-timestamp="1791200000">x</time></td>
</tr></tbody></table></div>
"""

DISCUSSION_HTML = """
<article class="forum-post-container"><div data-region="post">
  <time datetime="2026-10-06T09:00:00+09:00">x</time>
  <div class="post-content-container"><p>10月12日は休講です。</p></div>
</div></article>
"""

ENROLLED = [
    {
        "id": 12634,
        "fullname": "[後期][3X004] システムモデル",
        "idnumber": "2026_3X004",
        "visible": True,
        "viewurl": f"{BASE}/course/view.php?id=12634",
    }
]

EVENTS = [
    {"url": f"{BASE}/mod/quiz/view.php?id=102", "eventtype": "open", "timestart": 1791178800, "course": {"id": 12634}},
    {"url": f"{BASE}/mod/quiz/view.php?id=102", "eventtype": "close", "timestart": 1792076340, "course": {"id": 12634}},
]


class FakeMoodleClient:
    base_url = BASE

    def __init__(self, course_html: str = COURSE_HTML) -> None:
        self.course_html = course_html
        self.fetched: list[str] = []
        self.downloads: list[str] = []
        self.revision = 1

    def fetch_my_courses(self) -> str:
        return "<html></html>"

    def enrolled_courses(self):
        return ENROLLED

    def calendar_month(self, year: int, month: int):
        return EVENTS if (year, month) == (2026, 10) else []

    def fetch_course(self, course_id):
        return self.course_html

    def resolve_resource_file(self, cmid):
        if str(cmid) == "105":
            return f"{BASE}/pluginfile.php/5/mod_resource/content/1/robot.mov"
        return f"{BASE}/pluginfile.php/1/mod_resource/content/{self.revision}/lecture01.txt"

    def fetch_page(self, url, *, params=None):
        self.fetched.append(url)
        if "/mod/quiz/" in url:
            raise AssertionError("quiz pages must never be opened")
        if "/mod/assign/" in url:
            return ASSIGN_HTML
        if "/mod/forum/view.php" in url:
            return FORUM_HTML
        if "/mod/forum/discuss.php" in url:
            return DISCUSSION_HTML
        raise AssertionError(f"unexpected page {url}")

    def download_attachment(self, attachment, *, destination_dir: Path, max_bytes: int):
        self.downloads.append(attachment["url"])
        destination_dir.mkdir(parents=True, exist_ok=True)
        name = MoodleReadOnlyClient.file_name_from_url(attachment["url"])
        path = destination_dir / name
        path.write_text(f"講義資料 {name} rev{self.revision}", encoding="utf-8")
        return DownloadedFile(path, name, "text/plain", path.stat().st_size, attachment["url"])


def _sync(settings, client, **kwargs):
    with database(settings.paths.db_path) as conn:
        return MoodleSynchronizer(
            settings=settings,
            repository=Repository(conn),
            client=client,
            today=date(2026, 10, 5),
        ).run(**kwargs)


# --------------------------------------------------------------------- client
class _Response:
    def __init__(self, url, status=200, headers=None, text=""):
        self.url, self.status_code, self.headers, self.text = url, status, headers or {}, text

    def raise_for_status(self):
        return None


class _Session:
    def __init__(self):
        self.calls = []

    def get(self, url, **kwargs):
        self.calls.append(("GET", url, kwargs))
        return _Response(url, text='{"sesskey":"abc"}')

    def post(self, url, **kwargs):
        self.calls.append(("POST", url, kwargs))
        return _Response(url, text='[{"error":false,"data":{"courses":[]}}]')


def test_client_refuses_actions_and_foreign_hosts() -> None:
    client = MoodleReadOnlyClient(_Session(), base_url=BASE)
    for path in (
        "mod/quiz/view.php?id=1",
        "mod/quiz/attempt.php?attempt=1",
        "mod/assign/view.php?id=1&action=editsubmission",
        "course/view.php?id=1&edit=on&sesskey=x",
        "mod/forum/post.php?forum=1",
        "login/logout.php?sesskey=x",
    ):
        with pytest.raises(PermissionError):
            client.fetch_page(path)
    with pytest.raises(ValueError):
        client.fetch_page("https://example.com/2026/course/view.php?id=1")
    with pytest.raises(PermissionError):
        client.fetch_page("https://beef.center.kobe-u.ac.jp/2025/course/view.php?id=1")
    with pytest.raises(PermissionError):
        client.ajax("core_course_get_contents", {"courseid": 1})
    with pytest.raises(PermissionError):
        client.download_attachment(
            {"url": f"{BASE}/mod/resource/view.php?id=1"}, destination_dir=Path(".")
        )


def test_client_ajax_uses_sesskey_and_allowlist() -> None:
    session = _Session()
    client = MoodleReadOnlyClient(session, base_url=BASE)
    assert client.enrolled_courses() == []
    method, url, kwargs = session.calls[-1]
    assert method == "POST" and url == f"{BASE}/lib/ajax/service.php"
    assert kwargs["params"]["sesskey"] == "abc"
    assert kwargs["allow_redirects"] is False


def test_client_detects_expired_session() -> None:
    class Expired(_Session):
        def get(self, url, **kwargs):
            return _Response("https://beef.center.kobe-u.ac.jp/2026/login/index.php")

    with pytest.raises(PermissionError):
        MoodleReadOnlyClient(Expired(), base_url=BASE).fetch_my_courses()


def test_file_name_comes_from_utf8_pluginfile_path() -> None:
    url = f"{BASE}/pluginfile.php/52615/mod_resource/content/1/20261005_%E7%AC%AC1%E5%9B%9E.pdf"
    assert MoodleReadOnlyClient.file_name_from_url(url) == "20261005_第1回.pdf"


def test_academic_year_switches_in_april() -> None:
    from datetime import datetime

    assert academic_year(datetime(2027, 3, 31)) == 2026
    assert academic_year(datetime(2027, 4, 1)) == 2027


# --------------------------------------------------------------------- parser
def test_parse_course_page_and_enrolled_course() -> None:
    page = parse_course_page(COURSE_HTML, base_url=BASE)
    assert page["summary"] == "授業のテーマ モデル"
    by_id = {a["cmid"]: a for a in page["activities"]}
    assert set(by_id) == {"900", "101", "102", "103", "104", "105"}
    assert by_id["101"]["title"] == "第1回講義資料"
    assert by_id["101"]["file_type"] == "PDF"
    assert by_id["101"]["section_name"] == "第1回講義（10月5日）"
    assert by_id["102"]["module"] == "quiz"
    assert by_id["103"]["title"] == "授業内容の概要"
    assert by_id["103"]["inline_files"][0]["url"].endswith("/handout.pdf")
    course = parse_enrolled_course(ENROLLED[0], base_url=BASE)
    assert course["course_code"] == "3X004"
    assert course["course_name"] == "システムモデル"
    assert course["term"] == "後期"
    assert course["academic_year"] == 2026


def test_calendar_and_dates() -> None:
    mapping = calendar_deadlines(EVENTS)
    assert mapping["102"]["deadline"] == "2026-10-15T23:59"
    assert mapping["102"]["available_from"] == "2026-10-05T14:40"
    assert parse_japanese_datetime("2026年 10月 19日(月曜日) 23:59") == "2026-10-19T23:59"


def test_attachment_key_ignores_revision() -> None:
    a = f"{BASE}/pluginfile.php/1/mod_resource/content/1/x.pdf"
    b = f"{BASE}/pluginfile.php/1/mod_resource/content/7/x.pdf"
    assert attachment_key("101", a) == attachment_key("101", b)


# ----------------------------------------------------------------------- sync
def test_sync_stores_items_files_and_links_beefplus_course(tmp_path: Path) -> None:
    settings = load_settings(root=tmp_path)
    with database(settings.paths.db_path) as conn:
        beef = Repository(conn).upsert_course(
            {"source": "beef", "idnumber": "20263X0042001", "course_name": "システムモデル"}
        )
    client = FakeMoodleClient()
    result = _sync(settings, client)
    course = result["courses"]["2026_3X004"]
    assert course["linked_to_beefplus"] and course["course_id"] == beef.id
    assert course["media_skipped"] == 1
    assert not any("/mod/quiz/" in url for url in client.fetched)
    assert not any(url.endswith(".mov") for url in client.downloads)
    with database(settings.paths.db_path) as conn:
        rows = {
            row["source_id"]: dict(row)
            for row in conn.execute("SELECT * FROM items WHERE source='moodle'")
        }
        assert rows["material/2026_3X004/101"]["course_id"] == beef.id
        assert rows["test/2026_3X004/102"]["deadline"] == "2026-10-15T23:59"
        assert json.loads(rows["test/2026_3X004/102"]["metadata_json"])["read_policy"] == "metadata_only"
        assignment = rows["assignment/2026_3X004/104"]
        assert assignment["deadline"] == "2026-10-19T23:59"
        assert "レポートを提出" in assignment["body"]
        assert rows["announcement/2026_3X004/d55"]["body"] == "10月12日は休講です。"
        assert rows["announcement/2026_3X004/d55"]["published_at"] == "2026-10-06T09:00"
        anchor = conn.execute(
            "SELECT * FROM courses WHERE source='moodle' AND idnumber='2026_3X004'"
        ).fetchone()
        assert anchor["course_code"] == "3X004"
        chunks = conn.execute(
            """SELECT a.file_name, c.text FROM document_chunks c
               JOIN attachments a ON a.id=c.attachment_id WHERE a.course_id=?""",
            (beef.id,),
        ).fetchall()
        names = {row["file_name"] for row in chunks}
        assert {"lecture01.txt", "handout.pdf", "template.docx"} <= {
            row["file_name"] for row in conn.execute("SELECT file_name FROM attachments")
        }
        assert "lecture01.txt" in names


def test_resync_reuses_cache_and_redownloads_new_revision(tmp_path: Path) -> None:
    settings = load_settings(root=tmp_path)
    client = FakeMoodleClient()
    _sync(settings, client)
    first = len(client.downloads)
    second = _sync(settings, client)["courses"]["2026_3X004"]
    assert len(client.downloads) == first
    assert second["items_created"] == 0 and second["attachments_reused"] >= 1
    client.revision = 2
    _sync(settings, client)
    assert len(client.downloads) == first + 1
    with database(settings.paths.db_path) as conn:
        count = conn.execute(
            "SELECT COUNT(*) FROM attachments WHERE file_name='lecture01.txt'"
        ).fetchone()[0]
        assert count == 1


def test_removed_activity_becomes_unavailable(tmp_path: Path) -> None:
    settings = load_settings(root=tmp_path)
    _sync(settings, FakeMoodleClient())
    trimmed = COURSE_HTML.replace('data-id="104"', 'data-id="104" hidden').split(
        '<li class="activity activity-wrapper assign'
    )[0] + "</ul></li></ul>"
    result = _sync(settings, FakeMoodleClient(trimmed))
    assert result["courses"]["2026_3X004"]["items_unavailable"] == 2  # assign + video
    with database(settings.paths.db_path) as conn:
        row = conn.execute(
            "SELECT available FROM items WHERE source_id='assignment/2026_3X004/104'"
        ).fetchone()
        assert row["available"] == 0
