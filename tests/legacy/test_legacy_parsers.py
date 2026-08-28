from __future__ import annotations

import base64
from datetime import datetime

from uribo_mcp.legacy import beef, classifier, csi, dedup, gcal, inference, mail, newsnet, uribo


def test_beef_task_parser_and_venture_filter() -> None:
    html = '''<div id="taskList"><div class="result_list_content"><div class="result_list_line">
    <div class="tasklist-course">情報学</div><div class="tasklist-contents">課題</div>
    <div class="tasklist-title"><a href="/lms/course/report?idnumber=C1&reportId=9">第1回</a></div>
    <div class="tasklist-deadline">期限：2026/06/20 23:59:00</div></div></div></div>'''
    item = beef.parse_tasks(html)[0]
    assert item["source_id"] == "assignment/C1/9"
    assert item["deadline"] == "2026-06-20T23:59:00"


def test_misc_html_parsers() -> None:
    assert csi.parse_announcements('<ul class="newsList"><li><time class="news-date">2026/6/1</time><p class="news-txt">連絡</p></li></ul>')[0]["published_at"] == "2026-06-01"
    board = '<table><tr><td>2026/6/1 10:00</td><td><a href="?keijitype=4&genrecd=2&seqNo=3">掲示</a></td></tr></table>'
    assert uribo.parse_keiji_list(board)[0]["source_id"] == "keiji/4/2/3"


def test_mail_and_classifier_are_pure() -> None:
    payload = {"headers": [{"name": "From", "value": "事務 <a@office.kobe-u.ac.jp>"}, {"name": "To", "value": "x@stu.kobe-u.ac.jp"}, {"name": "Subject", "value": "FW: 休講"}], "mimeType": "text/plain", "body": {"data": base64.urlsafe_b64encode("本文".encode()).decode().rstrip("=")}}
    message = {"id": "m1", "internalDate": "1717200000000", "payload": payload}
    assert mail.is_target_mail(message)
    assert mail.parse_message(message)["title"] == "休講"
    assert classifier.classify("announcement", 0, None, "休講", None, [], ["休講"]) == "relevant"


def test_dedup_inference_and_gcal() -> None:
    now = datetime(2026, 6, 10)
    grouped = dedup.cluster_items([{"id": 1, "source": "a", "title": "休講", "date": now}, {"id": 2, "source": "b", "title": "休講", "date": now}], now=now)
    assert grouped == {1: 1, 2: 1}
    assert inference.diff_submitted(["a", "b"], ["a"]) == {"b"}
    desired = gcal.build_desired_events([{"source": "beef", "source_id": "1", "category": "assignment", "title": "課題", "url": None, "deadline": "2026-06-11T10:00:00", "published_at": None, "is_done": 0}], now)
    assert len(desired) == 1 and gcal.diff_events(desired, []) ["create"] == desired


def test_newsnet_requires_injected_feed_parser() -> None:
    class Entry(dict):
        __getattr__ = dict.__getitem__
    class Feed:
        entries = [Entry(title="ニュース", link="https://example.test/1", id="1")]
    assert newsnet.parse_feed(b"x", lambda _: Feed())[0]["source_id"] == "1"
