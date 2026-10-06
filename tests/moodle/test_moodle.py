import json
from datetime import date
from pathlib import Path

import pytest

from uribo_mcp.config import load_settings
from uribo_mcp.course.client import DownloadedFile
from uribo_mcp.db import database
from uribo_mcp.moodle.client import MoodleReadOnlyClient, MoodleSessionExpired, academic_year
from uribo_mcp.moodle.parser import (
    calendar_deadlines,
    parse_activity_page,
    parse_course_page,
    parse_enrolled_course,
    parse_forum_discussions,
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

    def __init__(self, course_html: str = COURSE_HTML, enrolled=None) -> None:
        self.course_html = course_html
        self.enrolled = ENROLLED if enrolled is None else enrolled
        self.fetched: list[str] = []
        self.downloads: list[str] = []
        self.revision = 1
        self.fail: set[str] = set()  # substrings of URLs/cmids that raise ConnectionError

    def _maybe_fail(self, key: str) -> None:
        if any(marker in key for marker in self.fail):
            raise ConnectionError(f"simulated failure: {key}")

    def enrolled_courses(self):
        return self.enrolled

    def calendar_month(self, year: int, month: int):
        return EVENTS if (year, month) == (2026, 10) else []

    def fetch_course(self, course_id):
        self._maybe_fail(f"course:{course_id}")
        return self.course_html

    def resolve_resource_file(self, cmid):
        self._maybe_fail(f"resource:{cmid}")
        if str(cmid) == "105":
            return f"{BASE}/pluginfile.php/5/mod_resource/content/1/robot.mov"
        return f"{BASE}/pluginfile.php/1/mod_resource/content/{self.revision}/lecture01.txt"

    def fetch_page(self, url, *, params=None):
        self.fetched.append(url)
        self._maybe_fail(url)
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
    def __init__(self, url, status=200, headers=None, text="", chunks=None):
        self.url, self.status_code, self.headers, self.text = url, status, headers or {}, text
        self.chunks = chunks or []
        self.closed = False

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")

    def iter_content(self, chunk_size=0):
        yield from self.chunks

    def close(self):
        self.closed = True


class _Session:
    """Routes GETs through ``routes`` (url-prefix -> response factory)."""

    def __init__(self, routes=None, post_text='[{"error":false,"data":{"courses":[]}}]', post_status=200):
        self.calls = []
        self.routes = routes or {}
        self.post_text, self.post_status = post_text, post_status
        self.responses = []

    def get(self, url, **kwargs):
        assert kwargs.get("allow_redirects") is False, "client must follow redirects itself"
        self.calls.append(("GET", url, kwargs))
        for prefix, factory in self.routes.items():
            if url.startswith(prefix):
                response = factory(url)
                break
        else:
            response = _Response(url, text='{"sesskey":"abc"}')
        self.responses.append(response)
        return response

    def post(self, url, **kwargs):
        self.calls.append(("POST", url, kwargs))
        return _Response(url, status=self.post_status, text=self.post_text)


def _redirect(location):
    return lambda url: _Response(url, status=303, headers={"Location": location})


def test_client_refuses_actions_and_foreign_hosts() -> None:
    client = MoodleReadOnlyClient(_Session(), base_url=BASE)
    for path in (
        "mod/quiz/view.php?id=1",
        "mod/quiz/attempt.php?attempt=1",
        "mod/assign/view.php?id=1&action=editsubmission",
        "course/view.php?id=1&edit=on&sesskey=x",
        "mod/forum/post.php?forum=1",
        "login/logout.php?sesskey=x",
        "pluginfile.php/../mod/quiz/attempt.php?cmid=1",
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


def test_redirect_hops_are_checked_against_the_allowlist() -> None:
    session = _Session({f"{BASE}/mod/page/view.php": _redirect(f"{BASE}/mod/quiz/attempt.php?attempt=9")})
    client = MoodleReadOnlyClient(session, base_url=BASE)
    with pytest.raises(PermissionError):
        client.fetch_page("mod/page/view.php?id=1")
    assert not any("/mod/quiz/" in call[1] for call in session.calls)
    session = _Session({f"{BASE}/mod/page/view.php": _redirect("https://beef.center.kobe-u.ac.jp/2025/course/view.php?id=1")})
    with pytest.raises(PermissionError):
        MoodleReadOnlyClient(session, base_url=BASE).fetch_page("mod/page/view.php?id=1")


def test_client_ajax_uses_sesskey_and_allowlist() -> None:
    session = _Session()
    client = MoodleReadOnlyClient(session, base_url=BASE)
    assert client.enrolled_courses() == []
    method, url, kwargs = session.calls[-1]
    assert method == "POST" and url == f"{BASE}/lib/ajax/service.php"
    assert kwargs["params"]["sesskey"] == "abc"
    assert kwargs["allow_redirects"] is False


@pytest.mark.parametrize(
    "session",
    [
        _Session({f"{BASE}/my/courses.php": _redirect(f"{BASE}/login/index.php")}),
        _Session({f"{BASE}/my/courses.php": _redirect("https://knossos.center.kobe-u.ac.jp/auth/realms/x")}),
        _Session({f"{BASE}/my/courses.php": lambda url: _Response(f"{BASE}/auth/shibboleth/index.php")}),
    ],
)
def test_expired_page_session_is_detected(session) -> None:
    with pytest.raises(MoodleSessionExpired):
        MoodleReadOnlyClient(session, base_url=BASE).fetch_my_courses()


@pytest.mark.parametrize(
    "post_status,post_text",
    [
        (303, ""),
        (200, '[{"error":true,"exception":{"errorcode":"servicerequireslogin"}}]'),
        (200, '[{"error":true,"exception":{"errorcode":"invalidsesskey"}}]'),
    ],
)
def test_expired_ajax_session_is_detected(post_status, post_text) -> None:
    client = MoodleReadOnlyClient(_Session(post_status=post_status, post_text=post_text), base_url=BASE)
    with pytest.raises(MoodleSessionExpired):
        client.enrolled_courses()


def test_ajax_non_json_is_a_runtime_error() -> None:
    client = MoodleReadOnlyClient(_Session(post_text="<html>maintenance</html>"), base_url=BASE)
    with pytest.raises(RuntimeError):
        client.enrolled_courses()


def test_resolve_resource_file_follows_only_to_pluginfile() -> None:
    file_url = f"{BASE}/pluginfile.php/1/mod_resource/content/3/a.pdf"
    session = _Session({f"{BASE}/mod/resource/view.php": _redirect(file_url + "?forcedownload=1")})
    assert MoodleReadOnlyClient(session, base_url=BASE).resolve_resource_file(1) == file_url
    assert not any("pluginfile" in call[1] for call in session.calls)  # not downloaded
    session = _Session({f"{BASE}/mod/resource/view.php": _redirect("https://knossos.center.kobe-u.ac.jp/auth")})
    with pytest.raises(MoodleSessionExpired):
        MoodleReadOnlyClient(session, base_url=BASE).resolve_resource_file(1)


def test_download_streams_to_disk_and_enforces_size_limit(tmp_path: Path) -> None:
    url = f"{BASE}/pluginfile.php/1/mod_resource/content/1/%E8%B3%87%E6%96%99.pdf"
    session = _Session({f"{BASE}/pluginfile.php/": lambda u: _Response(u, chunks=[b"ab", b"cd"], headers={"Content-Type": "application/pdf"})})
    client = MoodleReadOnlyClient(session, base_url=BASE)
    result = client.download_attachment({"url": url}, destination_dir=tmp_path)
    assert result.file_name == "資料.pdf" and result.path.read_bytes() == b"abcd" and result.size_bytes == 4
    assert session.responses[-1].closed
    with pytest.raises(ValueError):
        client.download_attachment({"url": url}, destination_dir=tmp_path / "small", max_bytes=3)
    assert not list((tmp_path / "small").iterdir())  # no partial file left behind
    session = _Session({f"{BASE}/pluginfile.php/": lambda u: _Response(u, headers={"Content-Length": "999"})})
    with pytest.raises(ValueError):
        MoodleReadOnlyClient(session, base_url=BASE).download_attachment({"url": url}, destination_dir=tmp_path, max_bytes=10)
    assert session.responses[-1].closed


def test_stream_is_closed_when_download_redirects_to_login(tmp_path: Path) -> None:
    session = _Session({f"{BASE}/pluginfile.php/": lambda u: _Response(f"{BASE}/login/index.php")})
    with pytest.raises(MoodleSessionExpired):
        MoodleReadOnlyClient(session, base_url=BASE).download_attachment(
            {"url": f"{BASE}/pluginfile.php/1/x.pdf"}, destination_dir=tmp_path
        )
    assert session.responses[-1].closed


def test_file_name_comes_from_utf8_pluginfile_path() -> None:
    url = f"{BASE}/pluginfile.php/52615/mod_resource/content/1/20261005_%E7%AC%AC1%E5%9B%9E.pdf"
    assert MoodleReadOnlyClient.file_name_from_url(url) == "20261005_第1回.pdf"


def test_academic_year_switches_in_april() -> None:
    from datetime import datetime

    assert academic_year(datetime(2027, 3, 31)) == 2026
    assert academic_year(datetime(2027, 4, 1)) == 2027


# ---------------------------------------------------------------- open_moodle
class _FakeDriver:
    def __init__(self, signed_in_after_login=True):
        self.current_url = "https://knossos.center.kobe-u.ac.jp/auth"
        self.signed_in_after_login = signed_in_after_login
        self.quit_called = False
        self.logged_in = False

    def get(self, url):
        if self.logged_in and self.signed_in_after_login:
            self.current_url = url
        elif "favicon" in url:
            self.current_url = url
        else:
            self.current_url = "https://knossos.center.kobe-u.ac.jp/auth"

    def add_cookie(self, cookie):
        pass

    def get_cookies(self):
        return [{"name": "MoodleSession", "value": "new", "domain": "beef.center.kobe-u.ac.jp", "path": "/"}]

    def quit(self):
        self.quit_called = True


def test_open_moodle_reuses_cookies_without_browser(tmp_path: Path, monkeypatch) -> None:
    from uribo_mcp import live
    from uribo_mcp.auth import CookieVault

    settings = load_settings(root=tmp_path)
    CookieVault(settings.paths.credentials_dir).save("moodle", [{"name": "MoodleSession", "value": "v"}])
    monkeypatch.setattr(live, "_requests_from_cookies", lambda cookies: _Session())
    client = live.open_moodle(
        settings, base_url=BASE, driver_factory=lambda: pytest.fail("browser must not start")
    )
    assert client.has_session_key


def test_open_moodle_logs_in_once_when_cookies_expired(tmp_path: Path, monkeypatch) -> None:
    from uribo_mcp import live
    from uribo_mcp.auth import CookieVault
    from uribo_mcp.errors import AuthenticationRequired

    settings = load_settings(root=tmp_path)
    vault = CookieVault(settings.paths.credentials_dir)
    vault.save("moodle", [{"name": "MoodleSession", "value": "old"}])
    expired = _Session({f"{BASE}/my/courses.php": _redirect(f"{BASE}/login/index.php")})
    monkeypatch.setattr(live, "_requests_from_cookies", lambda cookies: expired)
    monkeypatch.setattr(live, "_requests_from_driver", lambda driver: _Session())
    logins = []

    def login(driver, url, host):
        logins.append(url)
        driver.logged_in = True

    driver = _FakeDriver()
    client = live.open_moodle(settings, base_url=BASE, driver_factory=lambda: driver, login=login)
    assert len(logins) == 1 and driver.quit_called and client.has_session_key
    assert vault.load("moodle")[0]["value"] == "new"

    logins.clear()
    failing = _FakeDriver(signed_in_after_login=False)
    with pytest.raises(AuthenticationRequired):
        live.open_moodle(settings, base_url=BASE, driver_factory=lambda: failing, login=login)
    assert len(logins) == 1 and failing.quit_called  # one attempt, no retry


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


def test_parse_course_page_on_live_sample_if_present() -> None:
    sample = Path("/Users/asanoakira/.hermes/cache/scratch/beefbeef/course_6328.html")
    if not sample.exists():
        pytest.skip("live sample not available on this machine")
    page = parse_course_page(sample.read_text(encoding="utf-8"), base_url=BASE)
    modules = [a["module"] for a in page["activities"]]
    assert modules.count("resource") == 10 and len(set(a["cmid"] for a in page["activities"])) == len(modules)


def test_subsection_activities_are_not_duplicated() -> None:
    html = f"""
    <li class="section course-section" data-number="1" data-sectionname="S1"><ul>
      <li class="activity modtype_subsection" data-id="50"><span class="instancename">Sub</span>
        <li class="section course-section" data-number="9" data-sectionname="Sub"><ul>
          <li class="activity modtype_resource" data-id="51">
            <div class="activityname"><a href="{BASE}/mod/resource/view.php?id=51">
              <span class="instancename">nested</span></a></div></li>
        </ul></li>
      </li>
    </ul></li>"""
    activities = parse_course_page(html, base_url=BASE)["activities"]
    assert [(a["cmid"], a["section_name"]) for a in activities] == [("51", "Sub")]


def test_calendar_deadline_priority() -> None:
    url = f"{BASE}/mod/assign/view.php?id=7"
    events = [
        {"url": url, "eventtype": "due", "timestart": 1792076340},
        {"url": url, "eventtype": "expectcompletionon", "timestart": 1793000000},
        {"url": url, "eventtype": "gradingdue", "timestart": 1794000000},
        {"url": url, "eventtype": "open", "timestart": None},
        {"url": f"{BASE}/mod/attendance/view.php?id=8", "eventtype": "session", "timestart": 1792076340},
    ]
    mapping = calendar_deadlines(events)
    assert mapping["7"] == {"deadline": "2026-10-15T23:59", "expected_completion": "2026-10-26T16:33"}
    assert "deadline" not in mapping.get("8", {})
    later = calendar_deadlines([events[1], events[0]])
    assert later["7"]["deadline"] == "2026-10-15T23:59"
    only_expected = calendar_deadlines([events[1]])
    assert "deadline" not in only_expected["7"]
    assert calendar_deadlines(EVENTS)["102"] == {
        "deadline": "2026-10-15T23:59",
        "available_from": "2026-10-05T14:40",
    }


def test_japanese_dates() -> None:
    assert parse_japanese_datetime("2026年 10月 19日(月曜日) 23:59") == "2026-10-19T23:59"
    assert parse_japanese_datetime("2026年 10月 19日(月曜日)") == "2026-10-19"


def test_activity_page_reads_deadline_only_from_dates_and_status_table() -> None:
    html = ASSIGN_HTML.replace(
        "</div></div>\n",
        '<table class="generaltable"><tr><th>評定終了日時</th><td>2026年 11月 30日 12:00</td></tr></table></div></div>\n',
        1,
    )
    detail = parse_activity_page(html, base_url=BASE)
    assert detail["deadline"] == "2026-10-19T23:59"
    assert detail["submission_status"] == "未提出"
    assert detail["files"][0]["url"].endswith("template.docx")


def test_forum_listing_reports_pagination() -> None:
    listing = parse_forum_discussions(FORUM_HTML, base_url=BASE)
    assert [d["title"] for d in listing["discussions"]] == ["休講のお知らせ"] and not listing["paged"]
    assert listing["recognized"]
    empty = '<div id="region-main"><div class="forumnodiscuss alert">(まだ投稿はありません)</div></div>'
    assert parse_forum_discussions(empty, base_url=BASE) == {"discussions": [], "paged": False, "recognized": True}
    assert not parse_forum_discussions('<div id="region-main"><p>?</p></div>', base_url=BASE)["recognized"]
    paged = FORUM_HTML.replace("</div>", '<nav class="pagination"><a href="?id=1&page=1">2</a></nav></div>')
    assert parse_forum_discussions(paged, base_url=BASE)["paged"]


def test_attachment_key_ignores_revision() -> None:
    a = f"{BASE}/pluginfile.php/1/mod_resource/content/1/x.pdf"
    b = f"{BASE}/pluginfile.php/1/mod_resource/content/7/x.pdf"
    assert attachment_key("101", a) == attachment_key("101", b)


# ----------------------------------------------------------------------- sync
def _beef(settings, idnumber="20263X0042001"):
    with database(settings.paths.db_path) as conn:
        return Repository(conn).upsert_course(
            {"source": "beef", "idnumber": idnumber, "course_name": "システムモデル"}
        )


def _rows(settings, where="1=1"):
    with database(settings.paths.db_path) as conn:
        return {
            row["source_id"]: dict(row)
            for row in conn.execute(f"SELECT * FROM items WHERE source='moodle' AND {where}")
        }


def test_sync_stores_items_files_and_links_beefplus_course(tmp_path: Path) -> None:
    settings = load_settings(root=tmp_path)
    beef = _beef(settings)
    client = FakeMoodleClient()
    result = _sync(settings, client)
    course = result["courses"]["2026_3X004"]
    assert course["linked_to_beefplus"] and course["course_id"] == beef.id
    assert course["media_skipped"] == 1
    assert not any("/mod/quiz/" in url for url in client.fetched)
    assert not any(url.endswith(".mov") for url in client.downloads)
    rows = _rows(settings)
    assert rows["material/2026_3X004/101"]["course_id"] == beef.id
    assert rows["test/2026_3X004/102"]["deadline"] == "2026-10-15T23:59"
    assert json.loads(rows["test/2026_3X004/102"]["metadata_json"])["read_policy"] == "metadata_only"
    assignment = rows["assignment/2026_3X004/104"]
    assert assignment["deadline"] == "2026-10-19T23:59"
    assert "レポートを提出" in assignment["body"]
    assert json.loads(assignment["metadata_json"])["submission_status"] == "未提出"
    assert rows["announcement/2026_3X004/d55"]["body"] == "10月12日は休講です。"
    assert rows["announcement/2026_3X004/d55"]["published_at"] == "2026-10-06T09:00"
    with database(settings.paths.db_path) as conn:
        anchor = conn.execute(
            "SELECT * FROM courses WHERE source='moodle' AND idnumber='2026_3X004'"
        ).fetchone()
        assert anchor["course_code"] == "3X004"
        assert {"lecture01.txt", "handout.pdf", "template.docx"} <= {
            row["file_name"] for row in conn.execute("SELECT file_name FROM attachments")
        }
        chunks = conn.execute(
            """SELECT a.file_name FROM document_chunks c
               JOIN attachments a ON a.id=c.attachment_id WHERE a.course_id=?""",
            (beef.id,),
        ).fetchall()
        assert "lecture01.txt" in {row["file_name"] for row in chunks}


def test_sync_never_changes_is_done(tmp_path: Path) -> None:
    settings = load_settings(root=tmp_path)
    client = FakeMoodleClient()
    global ASSIGN_HTML
    original = ASSIGN_HTML
    try:
        ASSIGN_HTML = original.replace("未提出", "提出済み")
        _sync(settings, client)
    finally:
        ASSIGN_HTML = original
    row = _rows(settings)["assignment/2026_3X004/104"]
    assert row["is_done"] == 0
    assert json.loads(row["metadata_json"])["submission_status"] == "提出済み"


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


def _without(html: str, *cmids: str) -> str:
    soup_html = html
    for cmid in cmids:
        start = soup_html.index(f'data-id="{cmid}"')
        start = soup_html.rindex("<li ", 0, start)
        end = soup_html.index("</li>", start) + len("</li>")
        soup_html = soup_html[:start] + soup_html[end:]
    return soup_html


def test_removed_activity_becomes_unavailable(tmp_path: Path) -> None:
    settings = load_settings(root=tmp_path)
    _sync(settings, FakeMoodleClient())
    result = _sync(settings, FakeMoodleClient(_without(COURSE_HTML, "104", "105")))
    assert result["courses"]["2026_3X004"]["items_unavailable"] == 2
    rows = _rows(settings)
    assert rows["assignment/2026_3X004/104"]["available"] == 0
    assert rows["material/2026_3X004/105"]["available"] == 0
    assert rows["material/2026_3X004/101"]["available"] == 1


def test_transient_failures_keep_existing_rows_and_files(tmp_path: Path) -> None:
    settings = load_settings(root=tmp_path)
    _sync(settings, FakeMoodleClient())
    before = _rows(settings)
    client = FakeMoodleClient()
    client.fail = {"/mod/forum/view.php", "/mod/assign/view.php", "resource:101"}
    result = _sync(settings, client)["courses"]["2026_3X004"]
    assert result["items_unavailable"] == 0
    assert result["items_kept_after_error"] == 2
    after = _rows(settings)
    for key in ("announcement/2026_3X004/d55", "assignment/2026_3X004/104", "material/2026_3X004/101"):
        assert after[key]["available"] == 1
        assert after[key]["body"] == before[key]["body"]
        assert after[key]["deadline"] == before[key]["deadline"]
    with database(settings.paths.db_path) as conn:
        hidden = conn.execute(
            "SELECT file_name FROM attachments WHERE available=0"
        ).fetchall()
        assert hidden == []
    # discussion detail failure also keeps the stored announcement
    client = FakeMoodleClient()
    client.fail = {"/mod/forum/discuss.php"}
    _sync(settings, client)
    assert _rows(settings)["announcement/2026_3X004/d55"]["body"] == "10月12日は休講です。"


def test_course_page_failure_hides_nothing(tmp_path: Path) -> None:
    settings = load_settings(root=tmp_path)
    _sync(settings, FakeMoodleClient())
    client = FakeMoodleClient()
    client.fail = {"course:12634"}
    result = _sync(settings, client)
    assert result["courses"]["2026_3X004"]["status"] == "error"
    assert all(row["available"] == 1 for row in _rows(settings).values())


def test_session_expiry_aborts_without_hiding(tmp_path: Path) -> None:
    settings = load_settings(root=tmp_path)
    _sync(settings, FakeMoodleClient())

    class Expiring(FakeMoodleClient):
        def resolve_resource_file(self, cmid):
            raise MoodleSessionExpired("expired")

    with pytest.raises(MoodleSessionExpired):
        _sync(settings, Expiring())
    assert all(row["available"] == 1 for row in _rows(settings).values())


def test_two_moodle_courses_with_one_code_do_not_hide_each_other(tmp_path: Path) -> None:
    settings = load_settings(root=tmp_path)
    _beef(settings)
    twin = {**ENROLLED[0], "id": 99, "idnumber": "2026_3X004_b", "viewurl": f"{BASE}/course/view.php?id=99"}
    client = FakeMoodleClient(enrolled=[ENROLLED[0], twin])
    for _ in range(2):
        result = _sync(settings, client)
        assert {v["items_unavailable"] for v in result["courses"].values()} == {0}
    rows = _rows(settings)
    assert all(row["available"] == 1 for row in rows.values())
    assert any(key.startswith("material/2026_3X004/") for key in rows)
    assert any(key.startswith("material/2026_3X004_b/") for key in rows)


def test_unenrolled_course_hides_its_items_even_when_linked(tmp_path: Path) -> None:
    settings = load_settings(root=tmp_path)
    _beef(settings)
    with database(settings.paths.db_path) as conn:
        Repository(conn).upsert_item(
            {"source": "beef", "source_id": "material/20263X0042001/1", "category": "material",
             "title": "BEEF+ 資料", "course_id": 1}
        )
    _sync(settings, FakeMoodleClient())
    result = _sync(settings, FakeMoodleClient(enrolled=[]))
    assert result["courses_unavailable"] == 1
    assert all(row["available"] == 0 for row in _rows(settings).values())
    with database(settings.paths.db_path) as conn:
        beef_row = conn.execute("SELECT available FROM items WHERE source='beef'").fetchone()
        assert beef_row["available"] == 1


def test_link_changes_move_attachments_without_duplicates(tmp_path: Path) -> None:
    settings = load_settings(root=tmp_path)
    client = FakeMoodleClient()
    _sync(settings, client)
    beef = _beef(settings)
    _sync(settings, client)
    with database(settings.paths.db_path) as conn:
        visible = conn.execute(
            "SELECT course_id, file_name FROM attachments WHERE available=1 AND file_name='lecture01.txt'"
        ).fetchall()
        assert [(row["course_id"], row["file_name"]) for row in visible] == [(beef.id, "lecture01.txt")]


def test_ambiguous_or_prefix_only_beefplus_match_is_not_linked(tmp_path: Path) -> None:
    settings = load_settings(root=tmp_path)
    _beef(settings, "20263X00410001")  # longer code, prefix-only match
    assert not _sync(settings, FakeMoodleClient())["courses"]["2026_3X004"]["linked_to_beefplus"]
    _beef(settings, "20263X0042001")
    _beef(settings, "20263X0042002")
    assert not _sync(settings, FakeMoodleClient())["courses"]["2026_3X004"]["linked_to_beefplus"]


def test_course_ids_filter_skips_other_courses_and_unenrolment(tmp_path: Path) -> None:
    settings = load_settings(root=tmp_path)
    _sync(settings, FakeMoodleClient())
    result = _sync(settings, FakeMoodleClient(enrolled=[]), course_ids=["2026_3X004"])
    assert result["courses"] == {} and result["courses_unavailable"] == 0
    assert all(row["available"] == 1 for row in _rows(settings).values())


# ------------------------------------------------- second review regressions
def test_live_empty_forum_is_recognised_and_discussion_chrome_is_not_a_body() -> None:
    from uribo_mcp.moodle.parser import parse_forum_discussion

    sample = Path("/Users/asanoakira/.hermes/cache/scratch/beefbeef/forum_16327.html")
    if sample.exists():
        html = sample.read_text(encoding="utf-8")
        assert parse_forum_discussions(html, base_url=BASE)["recognized"]
        assert parse_forum_discussion(html, base_url=BASE)["body"] is None
    chrome = '<nav><div class="content">Home ダッシュボード マイコース</div></nav>'
    assert parse_forum_discussion(chrome, base_url=BASE) == {
        "body": None, "published_at": None, "files": [], "recognized": False
    }


def test_calendar_failure_or_window_keeps_known_deadlines(tmp_path: Path) -> None:
    settings = load_settings(root=tmp_path)
    _sync(settings, FakeMoodleClient())

    class NoCalendar(FakeMoodleClient):
        def calendar_month(self, year, month):
            raise ConnectionError("calendar down")

    result = _sync(settings, NoCalendar())
    assert result["calendar_complete"] is False
    assert _rows(settings)["test/2026_3X004/102"]["deadline"] == "2026-10-15T23:59"
    with database(settings.paths.db_path) as conn:  # calendar moved out of the window
        later = MoodleSynchronizer(
            settings=settings, repository=Repository(conn), client=FakeMoodleClient(), today=date(2027, 1, 5)
        ).run()
    assert later["calendar_events"] == 0
    assert _rows(settings)["test/2026_3X004/102"]["deadline"] == "2026-10-15T23:59"


def test_expected_completion_never_overrides_page_deadline(tmp_path: Path, monkeypatch) -> None:
    settings = load_settings(root=tmp_path)
    expected_only = [{"url": f"{BASE}/mod/assign/view.php?id=104", "eventtype": "expectcompletionon", "timestart": 1791900000}]
    monkeypatch.setattr(FakeMoodleClient, "calendar_month", lambda self, y, m: expected_only if (y, m) == (2026, 10) else [])
    _sync(settings, FakeMoodleClient())
    row = _rows(settings)["assignment/2026_3X004/104"]
    assert row["deadline"] == "2026-10-19T23:59"
    assert json.loads(row["metadata_json"])["expected_completion"] == "2026-10-13T23:00"


def test_http200_error_page_hides_nothing(tmp_path: Path) -> None:
    settings = load_settings(root=tmp_path)
    _sync(settings, FakeMoodleClient())
    error_page = '<body id="page-error"><div class="errorbox">メンテナンス中</div></body>'
    result = _sync(settings, FakeMoodleClient(error_page))
    assert result["courses"]["2026_3X004"]["status"] == "error"
    assert all(row["available"] == 1 for row in _rows(settings).values())


def test_unrecognised_forum_or_discussion_keeps_announcements(tmp_path: Path, monkeypatch) -> None:
    settings = load_settings(root=tmp_path)
    _sync(settings, FakeMoodleClient())
    original = FakeMoodleClient.fetch_page

    def odd_forum(self, url, *, params=None):
        if "/mod/forum/view.php" in url:
            return '<div id="region-main"><p>new layout</p></div>'
        return original(self, url, params=params)

    monkeypatch.setattr(FakeMoodleClient, "fetch_page", odd_forum)
    _sync(settings, FakeMoodleClient())
    row = _rows(settings)["announcement/2026_3X004/d55"]
    assert row["available"] == 1 and row["body"] == "10月12日は休講です。"

    def odd_discussion(self, url, *, params=None):
        if "/mod/forum/discuss.php" in url:
            return '<nav><div class="content">Home ダッシュボード</div></nav>'
        return original(self, url, params=params)

    monkeypatch.setattr(FakeMoodleClient, "fetch_page", odd_discussion)
    _sync(settings, FakeMoodleClient())
    assert _rows(settings)["announcement/2026_3X004/d55"]["body"] == "10月12日は休講です。"


@pytest.mark.parametrize("variant", ["paged", "truncated"])
def test_partial_forum_listing_keeps_unseen_announcements(tmp_path: Path, monkeypatch, variant) -> None:
    settings = load_settings(root=tmp_path)
    _sync(settings, FakeMoodleClient())
    original = FakeMoodleClient.fetch_page
    if variant == "paged":
        listing = FORUM_HTML.replace("d=55", "d=56").replace(
            "</div>", '<nav class="pagination"><a href="?id=900&page=1">2</a></nav></div>'
        )
    else:
        rows = "".join(
            f'<tr><th><a href="{BASE}/mod/forum/discuss.php?d={n}">告知{n}</a></th></tr>'
            for n in range(100, 131)
        )
        listing = f'<div id="region-main"><table>{rows}</table></div>'

    def forum(self, url, *, params=None):
        if "/mod/forum/view.php" in url:
            return listing
        return original(self, url, params=params)

    monkeypatch.setattr(FakeMoodleClient, "fetch_page", forum)
    _sync(settings, FakeMoodleClient())
    assert _rows(settings)["announcement/2026_3X004/d55"]["available"] == 1


def test_allowlist_refusal_fails_one_activity_not_the_source(tmp_path: Path) -> None:
    settings = load_settings(root=tmp_path)
    _sync(settings, FakeMoodleClient())

    class Refusing(FakeMoodleClient):
        def resolve_resource_file(self, cmid):
            if str(cmid) == "101":
                raise PermissionError("Endpoint is outside the read-only allowlist: /enrol/index.php")
            return super().resolve_resource_file(cmid)

    result = _sync(settings, Refusing())["courses"]["2026_3X004"]
    assert result["items_kept_after_error"] == 1 and result["items_unavailable"] == 0
    assert all(row["available"] == 1 for row in _rows(settings).values())


def test_failed_sso_skips_later_sso_sources(tmp_path: Path, monkeypatch) -> None:
    from uribo_mcp import sync as sync_module
    from uribo_mcp.errors import AuthenticationRequired

    settings = load_settings(root=tmp_path)
    calls = []

    def beef(settings, course_ids=None):
        calls.append("beef")
        raise AuthenticationRequired("KNOSSOS login failed")

    def moodle(settings, course_ids=None):
        calls.append("moodle")
        return {}

    monkeypatch.setattr(sync_module, "_sync_beef", beef)
    monkeypatch.setattr(sync_module, "_sync_moodle", moodle)
    monkeypatch.setattr(sync_module, "_sync_csi", lambda settings: calls.append("csi") or {})
    monkeypatch.setattr(sync_module.time, "sleep", lambda seconds: None)
    from dataclasses import replace

    settings = replace(settings, sync=replace(settings.sync, enabled_sources=("beef", "csi", "moodle")))
    results = sync_module.synchronize(settings, scope="all")
    assert calls == ["beef", "csi"]
    assert results["moodle"]["status"] == "skipped"
    assert results["csi"]["status"] == "ok"


def test_open_moodle_waits_for_shibboleth_chain_before_login(tmp_path: Path, monkeypatch) -> None:
    """Live failure 2026-10-06: get() returned mid-chain on the Moodle host,
    the old login helper saw the Moodle host and skipped the form."""
    from uribo_mcp import live
    from uribo_mcp.errors import AuthenticationRequired

    settings = load_settings(root=tmp_path)
    monkeypatch.setattr(live, "_requests_from_driver", lambda driver: _Session())
    monkeypatch.setattr("time.sleep", lambda seconds: None)

    class ChainDriver(_FakeDriver):
        def __init__(self):
            super().__init__()
            self.steps = []

        def get(self, url):
            if self.logged_in:
                self.current_url = url
            elif "favicon" in url:
                self.current_url = url
            else:  # mid-chain first, KNOSSOS on the next poll
                self.steps = ["https://beef.center.kobe-u.ac.jp/Shibboleth.sso/SAML2/POST",
                              "https://knossos.center.kobe-u.ac.jp/auth"]
                self.current_url = self.steps.pop(0)

        def __getattribute__(self, name):
            if name == "current_url":
                steps = object.__getattribute__(self, "__dict__").get("steps")
                value = object.__getattribute__(self, "__dict__")["current_url"]
                if steps:
                    object.__getattribute__(self, "__dict__")["current_url"] = steps.pop(0)
                return value
            return object.__getattribute__(self, name)

    logins = []

    def login(driver, url, host):
        assert "knossos" in driver.current_url, "form must be submitted on the KNOSSOS page"
        logins.append(url)
        driver.logged_in = True
        driver.current_url = url

    client = live.open_moodle(settings, base_url=BASE, driver_factory=ChainDriver, login=login)
    assert len(logins) == 1 and client.has_session_key

    class StuckDriver(_FakeDriver):
        def get(self, url):
            self.current_url = "https://idp.center.kobe-u.ac.jp/idp/error"

    monkeypatch.setattr("time.monotonic", iter(range(0, 1000, 30)).__next__)
    with pytest.raises(AuthenticationRequired):
        live.open_moodle(settings, base_url=BASE, driver_factory=StuckDriver, login=login)
    assert len(logins) == 1  # never submitted when KNOSSOS was not reached


def test_beef_schedule_also_runs_moodle_when_enabled(tmp_path: Path, monkeypatch) -> None:
    from dataclasses import replace

    from uribo_mcp import sync as sync_module

    calls = []
    monkeypatch.setattr(sync_module, "_sync_beef", lambda s, course_ids=None: calls.append("beef") or {})
    monkeypatch.setattr(sync_module, "_sync_moodle", lambda s, course_ids=None: calls.append("moodle") or {})
    monkeypatch.setattr(sync_module.time, "sleep", lambda seconds: None)
    settings = load_settings(root=tmp_path)
    off = replace(settings, sync=replace(settings.sync, enabled_sources=("beef",)))
    sync_module.synchronize(off, scope="beef")
    assert calls == ["beef"]
    calls.clear()
    on = replace(settings, sync=replace(settings.sync, enabled_sources=("beef", "moodle")))
    results = sync_module.synchronize(on, scope="beef")
    assert calls == ["beef", "moodle"] and results["moodle"]["status"] == "ok"


def test_mcp_course_lookup_finds_moodle_content(tmp_path: Path) -> None:
    from uribo_mcp.server import tool_get_deadlines

    settings = load_settings(root=tmp_path)
    beef = _beef(settings)
    _sync(settings, FakeMoodleClient())
    with database(settings.paths.db_path) as conn:
        repo = Repository(conn)
        course = repo.find_course("システムモデル")
        assert course["id"] == beef.id  # the row holding the linked Moodle items
        titles = {i["title"] for i in repo.list_course_content(course["id"])}
        assert {"第1回講義資料", "第1回 小テスト", "レポート1"} <= titles
        from uribo_mcp.legacy import webdata

        links = webdata.get_course_related(conn, ["システムモデル"])["course_links"]
        assert any(link.get("lms") == "BEEF (Moodle)" for link in links)
    from datetime import datetime

    from uribo_mcp.server import JST

    due = tool_get_deadlines(settings, date_from="2026-10-01", date_to="2026-10-31", now=datetime(2026, 10, 5, tzinfo=JST))
    assert {(d["source"], d["title"]) for d in due["deadlines"]} >= {("moodle", "第1回 小テスト"), ("moodle", "レポート1")}
