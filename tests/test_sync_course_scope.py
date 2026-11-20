"""A sync limited by ``course_ids`` must leave every other course untouched."""

from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from uribo_mcp import sync as sync_module
from uribo_mcp.config import load_settings
from uribo_mcp.db import database
from uribo_mcp.legacy import inference
from uribo_mcp.repository import Repository

BASE = "https://beefplus.center.kobe-u.ac.jp"
GLOBAL_KEY = sync_module.BEEF_GLOBAL_FINGERPRINT_KEY
FULL_KEY = sync_module.BEEF_FULL_DETAIL_KEY


def _course_html(idnumber: str, status: str) -> str:
    return f"""
<h1 class="course-title">科目{idnumber}</h1>
<div class="course-result-list contents-display-flex sortReportBlock clearfix">
  <a class="course-view-report-name reportTitle" href="/lms/course/report/submission?idnumber={idnumber}&amp;reportId=7">自習シート</a>
  <div class="course-view-report-time-start timeStart">2026/11/17 11:40</div>
  <div class="course-view-report-time-end timeEnd">2026/11/24 10:40</div>
  <div class="course-view-report-status submitStatus"><label>{status}</label></div>
</div>
<article class="contents-item" data-module="test" data-content-id="2">
  <a href="/lms/course/examination/takeresult?idnumber={idnumber}&examinationId=2">小テスト</a>
  <span>期限：2026/08/01 10:00 受験済</span>
</article>
"""


class FakeBeefClient:
    base_url = BASE

    def __init__(self) -> None:
        self.status = {"C1": "未提出", "C2": "未提出"}
        self.courses_fetched: list[str] = []
        self.details_fetched: list[str] = []

    def configure_announcement_form(self, html: str) -> None:
        pass

    def fetch_course(self, idnumber: str) -> str:
        self.courses_fetched.append(idnumber)
        return _course_html(idnumber, self.status[idnumber])

    def fetch_assignment_detail(self, url: str) -> str:
        self.details_fetched.append(url)
        return "<div id='contents'><h2 class='contents-title'>自習シート</h2><div class='body'>問題1</div></div>"

    def fetch_announcement_detail(self, *, information_id: str, idnumber: str) -> str:
        self.details_fetched.append(f"information/{idnumber}/{information_id}")
        return "<div id='contents'><div class='body'>お知らせ本文</div></div>"

    def download_attachment(self, attachment, *, destination_dir: Path, max_bytes: int):
        raise AssertionError("no attachment is expected in these tests")


def _task(category: str, idnumber: str, key: str, path: str, title: str) -> dict:
    return {
        "source": "beef",
        "source_id": f"{category}/{idnumber}/{key}",
        "category": category,
        "title": title,
        "body": None,
        "url": f"{BASE}{path}?idnumber={idnumber}&id={key}",
        "course_name": f"科目{idnumber}",
        "published_at": None,
        "deadline": None,
    }


def _announcement(info_id: str, idnumber: str | None) -> dict:
    return {
        "source": "beef",
        "source_id": f"announcement/info/{info_id}",
        "category": "announcement",
        "title": f"お知らせ{info_id}",
        "body": None,
        "url": f"{BASE}/lms/course?idnumber={idnumber}",
        "course_name": f"科目{idnumber}",
        "published_at": "2026-11-18T09:00",
        "deadline": None,
    }


def _timetable_row(idnumber: str, period: int) -> dict:
    return {
        "day_of_week": 1,
        "period": period,
        "course_name": f"科目{idnumber}",
        "room": None,
        "quarter": "Q3",
        "idnumber": idnumber,
    }


class Beef:
    """What the BEEF+ global pages show; the parsers are replaced by this."""

    def __init__(self) -> None:
        self.client = FakeBeefClient()
        self.timetable = [_timetable_row("C1", 1), _timetable_row("C2", 2)]
        # The global-list copies are poorer than the course pages: no deadline,
        # another URL, a title with trailing whitespace.
        self.tasks = [
            _task("assignment", "C1", "7", "/lms/course/report/submission", "自習シート "),
            _task("test", "C1", "2", "/lms/course/examination/taketop", "小テスト"),
            _task("assignment", "C2", "7", "/lms/course/report/submission", "自習シート "),
            _task("test", "C2", "2", "/lms/course/examination/taketop", "小テスト"),
            # A course that is not on the timetable (an earlier term).
            _task("assignment", "OLD", "1", "/lms/course/report/submission", "過去の課題"),
        ]
        self.info_map = {"41": "C1", "42": "C2"}
        self.information = [
            _announcement("41", "C1"),
            _announcement("42", "C2"),
            _announcement("99", None),
        ]


@pytest.fixture
def beef(monkeypatch) -> Beef:
    import uribo_mcp.legacy.beef as legacy_beef
    import uribo_mcp.live as live

    state = Beef()

    @contextmanager
    def fake_lms(settings, **kwargs):
        yield object(), state.client

    monkeypatch.setattr(live, "authenticated_lms", fake_lms)
    monkeypatch.setattr(
        legacy_beef, "fetch_pages", lambda driver, urls, **kwargs: dict.fromkeys(urls, "")
    )
    monkeypatch.setattr(legacy_beef, "parse_timetable", lambda html: list(state.timetable))
    monkeypatch.setattr(legacy_beef, "parse_tasks", lambda html: list(state.tasks))
    monkeypatch.setattr(legacy_beef, "info_idnumbers", lambda *htmls: dict(state.info_map))
    monkeypatch.setattr(
        legacy_beef, "parse_information_list", lambda html, ids=None: list(state.information)
    )
    monkeypatch.setattr(legacy_beef, "parse_updateinfo", lambda html: [])
    monkeypatch.setattr(sync_module.time, "sleep", lambda seconds: None)
    return state


def _items(settings, where: str = "1=1") -> dict[str, dict]:
    with database(settings.paths.db_path) as conn:
        return {
            row["source_id"]: dict(row)
            for row in conn.execute(f"SELECT * FROM items WHERE source='beef' AND {where}")
        }


def _revisions(settings) -> dict[str, int]:
    with database(settings.paths.db_path) as conn:
        return {
            row["source_id"]: row["n"]
            for row in conn.execute(
                "SELECT source_id, COUNT(*) AS n FROM item_revisions GROUP BY source_id"
            )
        }


def _preferences(settings) -> dict[str, str]:
    with database(settings.paths.db_path) as conn:
        return {
            row["key"]: row["value"]
            for row in conn.execute(
                "SELECT key,value FROM user_preferences WHERE key LIKE 'detail_fingerprints:%' OR key=?",
                (FULL_KEY,),
            )
        }


def _make_full_detail_pass_due(settings) -> str:
    old = (datetime.now(UTC) - timedelta(hours=sync_module.BEEF_FULL_DETAIL_HOURS + 1)).isoformat()
    with database(settings.paths.db_path) as conn:
        sync_module._set_preference(conn, FULL_KEY, old)
    return old


def _add_course_missing_from_timetable(settings) -> None:
    with database(settings.paths.db_path) as conn:
        repository = Repository(conn)
        course = repository.upsert_course(
            {"source": "beef", "idnumber": "GONE", "course_name": "履修をやめた科目"}
        )
        repository.upsert_item(
            {
                "source": "beef",
                "source_id": "material/GONE/1",
                "category": "material",
                "title": "資料",
                "course_id": course.id,
            }
        )


def test_one_course_sync_leaves_other_courses_untouched(tmp_path: Path, beef: Beef) -> None:
    settings = load_settings(root=tmp_path)
    sync_module._sync_beef(settings)
    _add_course_missing_from_timetable(settings)
    old_full_pass = _make_full_detail_pass_due(settings)
    items_before = _items(settings)
    revisions_before = _revisions(settings)
    preferences_before = _preferences(settings)
    with database(settings.paths.db_path) as conn:
        courses_before = [dict(row) for row in conn.execute("SELECT * FROM courses WHERE idnumber != 'C1' ORDER BY id")]
        snapshot_before = inference.load_task_snapshot(conn)
    assert preferences_before[FULL_KEY] == old_full_pass
    assert "assignment/OLD/1" in preferences_before[GLOBAL_KEY]

    # C1 changes on its course page; every task of C2 and OLD leaves the task list.
    beef.client.status["C1"] = "期限内提出"
    beef.tasks = [task for task in beef.tasks if "/C1/" in task["source_id"]]
    beef.tasks.append(_task("assignment", "OLD", "2", "/lms/course/report/submission", "別の過去課題"))
    beef.information.append(_announcement("43", "C2"))
    beef.info_map["43"] = "C2"
    beef.client.courses_fetched.clear()
    beef.client.details_fetched.clear()

    result = sync_module._sync_beef(settings, course_ids=["C1"])

    assert list(result["courses"]) == ["C1"]
    assert result["courses"]["C1"]["items_updated"] == 1
    assert (result["created"], result["updated"], result["courses_unavailable"]) == (0, 0, 0)
    assert beef.client.courses_fetched == ["C1"]
    assert all("idnumber=C1&" in url for url in beef.client.details_fetched)

    items_after = _items(settings)
    assert set(items_after) == set(items_before)  # nothing created from the global lists
    changed = {key for key in items_before if items_after[key] != items_before[key]}
    assert changed == {"assignment/C1/7"}
    other = [key for key in items_before if "/C1/" not in key]
    assert {"assignment/C2/7", "test/C2/2", "assignment/OLD/1", "material/GONE/1"} <= set(other)
    assert all(items_after[key]["available"] == 1 for key in other)
    revisions_after = _revisions(settings)
    assert {key: n for key, n in revisions_after.items() if "/C1/" not in key} == {
        key: n for key, n in revisions_before.items() if "/C1/" not in key
    }
    assert revisions_after["assignment/C1/7"] == revisions_before["assignment/C1/7"] + 1

    preferences_after = _preferences(settings)
    assert {key for key in preferences_before if preferences_after[key] != preferences_before[key]} == {
        "detail_fingerprints:beef:C1"
    }
    with database(settings.paths.db_path) as conn:
        assert [dict(row) for row in conn.execute("SELECT * FROM courses WHERE idnumber != 'C1' ORDER BY id")] == courses_before
        assert inference.load_task_snapshot(conn) == snapshot_before

    # The next all-course sync catches up on everything the limited one skipped.
    after_full = sync_module._sync_beef(settings)
    assert after_full["full_detail_pass"] is True
    assert after_full["courses_unavailable"] == 1
    items_final = _items(settings)
    assert items_final["material/GONE/1"]["available"] == 0
    assert items_final["assignment/C2/7"]["is_done"] == 1  # left the task list
    assert items_final["assignment/C2/7"]["metadata_json"] == items_before["assignment/C2/7"]["metadata_json"]
    assert "assignment/OLD/2" in items_final
    assert _preferences(settings)[FULL_KEY] != old_full_pass


def test_task_inference_in_one_course_sync_covers_only_that_course(tmp_path: Path, beef: Beef) -> None:
    settings = load_settings(root=tmp_path)
    sync_module._sync_beef(settings)
    beef.tasks = []  # every task was submitted or closed

    sync_module._sync_beef(settings, course_ids=["C1"])

    done = {key: row["is_done"] for key, row in _items(settings).items()}
    assert done["assignment/C1/7"] == 1 and done["test/C1/2"] == 1
    assert done["assignment/C2/7"] == 0 and done["test/C2/2"] == 0
    assert done["assignment/OLD/1"] == 0
    with database(settings.paths.db_path) as conn:
        assert inference.load_task_snapshot(conn) == {
            "assignment/C2/7",
            "test/C2/2",
            "assignment/OLD/1",
        }


def test_all_course_sync_keeps_course_pages_canonical_and_stores_the_rest(
    tmp_path: Path, beef: Beef
) -> None:
    settings = load_settings(root=tmp_path)
    _add_course_missing_from_timetable(settings)

    result = sync_module._sync_beef(settings)

    assert sorted(result["courses"]) == ["C1", "C2"]
    assert result["full_detail_pass"] is True
    assert result["courses_unavailable"] == 1
    # Only the items outside the timetable come from the global lists.
    assert (result["created"], result["updated"]) == (2, 0)
    items = _items(settings)
    assert set(items) == {
        "assignment/C1/7",
        "test/C1/2",
        "assignment/C2/7",
        "test/C2/2",
        "assignment/OLD/1",
        "announcement/info/99",
        "material/GONE/1",
    }
    assert items["material/GONE/1"]["available"] == 0
    for idnumber in ("C1", "C2"):
        assignment = items[f"assignment/{idnumber}/7"]
        assert assignment["title"] == "自習シート"
        assert assignment["deadline"] == "2026-11-24T10:40"
        assert '"group_title"' in assignment["metadata_json"]
        assert "takeresult" in items[f"test/{idnumber}/2"]["url"]
    assert items["assignment/OLD/1"]["body"] == "問題1"
    preferences = _preferences(settings)
    assert set(sync_module.json.loads(preferences[GLOBAL_KEY])) == {"assignment/OLD/1"}
    assert FULL_KEY in preferences

    details = len(beef.client.details_fetched)
    again = sync_module._sync_beef(settings)
    assert (again["created"], again["updated"], again["full_detail_pass"]) == (0, 0, False)
    assert again["global_details_skipped"] == 1
    assert len(beef.client.details_fetched) == details
    assert _items(settings) == items
