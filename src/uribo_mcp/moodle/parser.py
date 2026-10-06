"""Parsers for Moodle 4.x pages served by LMS BEEF.

Only stable Moodle markers are used: ``li.activity[data-id]`` with a
``modtype_<module>`` class, section ``li`` elements carrying
``data-number``/``data-sectionname`` and URLs such as
``/mod/<module>/view.php?id=<cmid>`` and ``/pluginfile.php/``.
"""

from __future__ import annotations

import re
from datetime import UTC, datetime, timedelta, timezone
from typing import Any
from urllib.parse import parse_qs, urljoin, urlparse

from bs4 import BeautifulSoup, Tag

JST = timezone(timedelta(hours=9))

# Moodle module -> uribo item category.  Modules missing here are skipped
# (e.g. ``subsection`` containers, whose activities are parsed on their own).
CATEGORY_BY_MODULE = {
    "resource": "material",
    "folder": "material",
    "page": "material",
    "url": "material",
    "label": "material",
    "assign": "assignment",
    "quiz": "test",
    "feedback": "survey",
    "questionnaire": "survey",
    "choice": "survey",
    "attendance": "attendance",
    "forum": "forum",
}
# Pages that are safe to open for their body text (no attempt/submit side effects).
DETAIL_MODULES = frozenset({"page", "folder", "assign"})

_SECTION_SELECTOR = "li.section[data-number], li.course-section[data-number]"
_HIDDEN_SELECTOR = ".accesshide, .sr-only, .visually-hidden, script, style"
# Calendar event types.  ``expectcompletionon`` is the teacher's planned
# completion date, not a deadline, so it is reported separately.
# ``gradingdue`` (the teacher's grading deadline) and unknown types are ignored.
_DEADLINE_EVENTS = frozenset({"due", "close"})
_EXPECTED_EVENTS = frozenset({"expectcompletionon"})
_OPEN_EVENTS = frozenset({"open", "allowsubmissionsfromdate"})
_DEADLINE_LABEL = re.compile(r"^(?:期限|終了日時|終了|Due|Closes?)$", re.IGNORECASE)
_STATUS_LABEL = re.compile(r"提出ステータス|Submission status", re.IGNORECASE)


def _clean(value: str | None) -> str:
    return re.sub(r"[\s\u3000]+", " ", value or "").strip()


def _visible_copy(element: Tag) -> BeautifulSoup:
    copy = BeautifulSoup(str(element), "html.parser")
    for hidden in copy.select(_HIDDEN_SELECTOR):
        hidden.decompose()
    return copy


def _text(element: Tag | None) -> str:
    return _clean(_visible_copy(element).get_text(" ", strip=True)) if element else ""


def _multiline(element: Tag | None) -> str | None:
    if element is None:
        return None
    raw = _visible_copy(element).get_text("\n", strip=True)
    text = "\n".join(line for line in (_clean(part) for part in raw.splitlines()) if line)
    return text or None


def epoch_to_iso(value: int | str | None) -> str | None:
    """Unix seconds -> naive JST ``YYYY-MM-DDTHH:MM`` (the DB's deadline format)."""
    if value in (None, "", 0, "0"):
        return None
    try:
        seconds = int(value)
    except (TypeError, ValueError):
        return None
    return (
        datetime.fromtimestamp(seconds, UTC)
        .astimezone(JST)
        .replace(tzinfo=None)
        .isoformat(timespec="minutes")
    )


def course_code(fullname: str, idnumber: str | None = None) -> str | None:
    """Kobe timetable codes are five characters, e.g. ``3X004``."""
    match = re.search(r"\[([0-9][A-Z][0-9A-Z]{3})\]", fullname or "")
    if match:
        return match.group(1)
    match = re.search(r"_([0-9][A-Z][0-9A-Z]{3})$", idnumber or "")
    return match.group(1) if match else None


def course_title(fullname: str) -> str:
    """``[後期][3X004] システムモデル`` -> ``システムモデル``."""
    return _clean(re.sub(r"^(?:\s*\[[^\]]*\])+", "", fullname or "")) or _clean(fullname)


def course_term(fullname: str) -> str | None:
    match = re.match(r"\s*\[([^\]]*期[^\]]*|第\d\s*クォーター|[1-4]Q)\]", fullname or "")
    return match.group(1) if match else None


def parse_enrolled_course(raw: dict[str, Any], *, base_url: str) -> dict[str, Any]:
    fullname = str(raw.get("fullname") or raw.get("shortname") or raw.get("id"))
    year_match = re.search(r"/(\d{4})$", base_url.rstrip("/"))
    return {
        "moodle_id": int(raw["id"]),
        "idnumber": str(raw.get("idnumber") or f"moodle-{raw['id']}"),
        "fullname": fullname,
        "course_name": course_title(fullname),
        "course_code": course_code(fullname, raw.get("idnumber")),
        "term": course_term(fullname),
        "academic_year": int(year_match.group(1)) if year_match else None,
        "url": str(raw.get("viewurl") or f"{base_url}/course/view.php?id={raw['id']}"),
    }


def pluginfile_links(html: str | Tag, *, base_url: str) -> list[dict[str, str]]:
    soup = html if isinstance(html, Tag) else BeautifulSoup(html or "", "html.parser")
    out: list[dict[str, str]] = []
    seen: set[str] = set()
    for tag in soup.select("a[href*='/pluginfile.php/']"):
        url = urljoin(base_url + "/", str(tag["href"]).strip()).split("?", 1)[0]
        if url in seen:
            continue
        seen.add(url)
        out.append({"url": url, "name": _text(tag) or url.rsplit("/", 1)[-1]})
    return out


def _nearest_section(li: Tag) -> Tag | None:
    parent = li.parent
    while parent is not None:
        if isinstance(parent, Tag) and parent.name == "li" and parent.has_attr("data-number"):
            if set(parent.get("class") or []) & {"section", "course-section"}:
                return parent
        parent = parent.parent
    return None


def parse_course_page(html: str, *, base_url: str) -> dict[str, Any]:
    """Return ``{"summary", "activities", "is_course_page"}`` from ``course/view.php``.

    ``is_course_page`` is false for error/maintenance pages served with HTTP
    200; callers must not treat a missing activity list as deletions then.

    Each activity is reported once, under its innermost section, so Moodle 4.5
    subsections (sections nested inside a ``modtype_subsection`` activity) do
    not duplicate their activities into the parent section.
    """
    soup = BeautifulSoup(html or "", "html.parser")
    summary_parts = [
        text
        for section in soup.select(_SECTION_SELECTOR)
        if section.get("data-number") == "0"
        for text in [_multiline(section.select_one(".summarytext, .section-summary-text"))]
        if text
    ]
    activities: list[dict[str, Any]] = []
    seen: set[str] = set()
    for li in soup.select("li.activity[data-id]"):
        module_match = re.search(r"modtype_(\w+)", " ".join(li.get("class") or []))
        cmid = str(li["data-id"])
        if not module_match or cmid in seen:
            continue
        seen.add(cmid)
        module = module_match.group(1)
        if module == "subsection":
            continue
        section = _nearest_section(li)
        section_name = _clean(section.get("data-sectionname")) if section is not None else ""
        link = li.select_one(".activityname a[href], a.aalink[href]")
        href = urljoin(base_url + "/", str(link["href"]).strip()) if link else None
        named = li.select_one("[data-activityname]")
        title = _text(li.select_one(".instancename")) or _clean(
            str(named.get("data-activityname")) if named else ""
        )
        content_el = li.select_one(".activity-altcontent, .contentafterlink, .description")
        body = _multiline(content_el)
        if module == "label" and body:
            # data-activityname is truncated with "..."; the first line is not.
            title = body.split("\n", 1)[0][:80]
        activities.append(
            {
                "cmid": cmid,
                "module": module,
                "title": title or f"{module} {cmid}",
                "url": href,
                "body": body,
                "file_type": _text(li.select_one(".activitybadge")) or None,
                "section_name": section_name or None,
                "inline_files": pluginfile_links(content_el, base_url=base_url)
                if content_el
                else [],
            }
        )
    # A course page always renders its sections; Moodle's error page can keep
    # the course-view body id, so the id alone is not evidence.
    is_course_page = bool(
        soup.select_one(_SECTION_SELECTOR) or soup.select_one(".course-content li.section")
    ) and not soup.select_one(".errorbox, [data-rel='fatalerror'], #notice")
    return {
        "summary": "\n".join(summary_parts) or None,
        "activities": activities,
        "is_course_page": is_course_page,
    }


def parse_activity_page(html: str, *, base_url: str) -> dict[str, Any]:
    """Body text, dates, submission status and files from a page/folder/assign view."""
    soup = BeautifulSoup(html or "", "html.parser")
    main = soup.select_one("#region-main [role='main'], #region-main") or soup
    for noisy in main.select(".completion-info, .activity-navigation, form, .submissionlinks"):
        noisy.decompose()
    intro = main.select_one(".activity-description, #intro")
    content = main.select_one(".box.generalbox.center, .page-content, #folder_tree0, .foldertree")
    body_parts = [part for part in (_multiline(intro), _multiline(content)) if part]
    dates: dict[str, str] = {}
    for row in main.select("[data-region='activity-dates'] > div, .activity-dates > div"):
        match = re.match(r"\s*([^:：\d]+?)\s*[:：]\s*(.+)", _clean(row.get_text(" ", strip=True)))
        if match:
            dates[match.group(1).strip()] = match.group(2).strip()
    status: dict[str, str] = {}
    for tr in main.select("table.submissionstatustable tr"):
        th, td = tr.select_one("th"), tr.select_one("td")
        if th and td:
            status[_text(th)] = _text(td)
    deadline = next(
        (
            parsed
            for label, value in dates.items()
            if _DEADLINE_LABEL.match(label)
            for parsed in [parse_japanese_datetime(value)]
            if parsed
        ),
        None,
    )
    return {
        "body": "\n\n".join(dict.fromkeys(body_parts)) or None,
        "deadline": deadline,
        "submission_status": next(
            (value for key, value in status.items() if _STATUS_LABEL.search(key)), None
        ),
        "dates": dates or None,
        "files": pluginfile_links(main, base_url=base_url),
    }


_JP_DT = re.compile(
    r"(20\d{2})年\s*(\d{1,2})月\s*(\d{1,2})日(?:[^\d]{0,16}?(\d{1,2}):(\d{2}))?"
)


def parse_japanese_datetime(text: str | None) -> str | None:
    """``2026年 10月 19日(月曜日) 23:59`` -> ``2026-10-19T23:59``; date-only -> ``2026-10-19``."""
    match = _JP_DT.search(text or "")
    if not match:
        return None
    year, month, day, hour, minute = match.groups()
    if hour is None:
        return datetime(int(year), int(month), int(day)).date().isoformat()
    return datetime(int(year), int(month), int(day), int(hour), int(minute)).isoformat(
        timespec="minutes"
    )


def parse_forum_discussions(html: str, *, base_url: str) -> dict[str, Any]:
    """Discussion rows from ``mod/forum/view.php`` (Moodle 4.x list view).

    NOTE: the live BEEF forums had no posts when this was written, so these
    selectors are not yet verified against real content.  ``paged`` is true
    when the list continues on another page and ``recognized`` is false for an
    unknown layout; in either case the caller must not treat unseen
    discussions as deleted.
    """
    soup = BeautifulSoup(html or "", "html.parser")
    main = soup.select_one("#region-main") or soup
    discussions: list[dict[str, Any]] = []
    seen: set[str] = set()
    for link in main.select("a[href*='/mod/forum/discuss.php']"):
        href = urljoin(base_url + "/", str(link["href"]).strip())
        discussion_id = (parse_qs(urlparse(href).query).get("d") or [None])[0]
        if not discussion_id or discussion_id in seen:
            continue
        title = _text(link)
        if not title:
            continue
        seen.add(discussion_id)
        row = link.find_parent("tr") or link.find_parent(
            attrs={"data-region": "discussion-list-item"}
        )
        stamp = row.select_one("time[data-timestamp], [data-timestamp]") if row else None
        discussions.append(
            {
                "discussion_id": discussion_id,
                "title": title,
                "url": f"{base_url}/mod/forum/discuss.php?d={discussion_id}",
                "published_at": epoch_to_iso(stamp.get("data-timestamp")) if stamp else None,
            }
        )
    # Only a list we positively recognise may be used to detect deletions:
    # at least one discussion, or Moodle's explicit "no discussions" notice.
    recognized = bool(discussions) or main.select_one(".forumnodiscuss") is not None
    paged = bool(
        main.select_one("nav.pagination, ul.pagination, [data-region='pagination']")
        or main.select_one("a[href*='/mod/forum/view.php'][href*='page=']")
    )
    return {"discussions": discussions, "paged": paged, "recognized": recognized}


def parse_forum_discussion(html: str, *, base_url: str) -> dict[str, Any]:
    """First post of ``mod/forum/discuss.php`` (selectors not yet live-verified)."""
    soup = BeautifulSoup(html or "", "html.parser")
    post = soup.select_one("[data-region='post'], .forumpost")
    if post is None:  # unknown layout: never fall back to page chrome
        return {"body": None, "published_at": None, "files": [], "recognized": False}
    body_el = post.select_one(
        "[data-region-content='forum-post-core'], .post-content-container, .posting"
    )
    stamp = post.select_one("time[datetime]")
    published = None
    if stamp is not None:
        try:
            published = (
                datetime.fromisoformat(str(stamp["datetime"]).replace("Z", "+00:00"))
                .astimezone(JST)
                .replace(tzinfo=None)
                .isoformat(timespec="minutes")
            )
        except ValueError:
            published = None
    return {
        "body": _multiline(body_el),
        "published_at": published,
        "files": pluginfile_links(post, base_url=base_url),
        "recognized": body_el is not None,
    }


def calendar_deadlines(events: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    """Map course-module id -> {deadline, available_from, expected_completion}.

    Only ``due``/``close`` set ``deadline``; ``expectcompletionon`` goes to
    ``expected_completion`` so it can never outrank a real deadline.
    """
    out: dict[str, dict[str, Any]] = {}
    for event in events:
        url = str(event.get("url") or "")
        if "/mod/" not in url:
            continue
        cmid = (parse_qs(urlparse(url).query).get("id") or [None])[0]
        when = epoch_to_iso(event.get("timestart"))
        if not cmid or not when:
            continue
        kind = str(event.get("eventtype") or "")
        if kind in _OPEN_EVENTS:
            out.setdefault(cmid, {})["available_from"] = when
            continue
        if kind in _DEADLINE_EVENTS:
            out.setdefault(cmid, {})["deadline"] = when
        elif kind in _EXPECTED_EVENTS:
            out.setdefault(cmid, {})["expected_completion"] = when
    return out


__all__ = [
    "CATEGORY_BY_MODULE",
    "DETAIL_MODULES",
    "calendar_deadlines",
    "course_code",
    "course_term",
    "course_title",
    "epoch_to_iso",
    "parse_activity_page",
    "parse_course_page",
    "parse_enrolled_course",
    "parse_forum_discussion",
    "parse_forum_discussions",
    "parse_japanese_datetime",
    "pluginfile_links",
]
