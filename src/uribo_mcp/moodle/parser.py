"""Parsers for Moodle 4.x pages served by LMS BEEF.

Only stable Moodle markers are used: ``li.activity[data-id]`` with a
``modtype_<module>`` class, ``li.section[data-sectionname]`` and URLs such as
``/mod/<module>/view.php?id=<cmid>`` and ``/pluginfile.php/``.
"""

from __future__ import annotations

import re
from datetime import UTC, datetime, timedelta, timezone
from typing import Any
from urllib.parse import parse_qs, urljoin, urlparse

from bs4 import BeautifulSoup, Tag

JST = timezone(timedelta(hours=9))

# Moodle module -> uribo item category.  Modules missing here are skipped.
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


def _clean(value: str | None) -> str:
    return re.sub(r"[\s\u3000]+", " ", value or "").strip()


def _text(element: Tag | None, *, sep: str = " ") -> str:
    if element is None:
        return ""
    copy = BeautifulSoup(str(element), "html.parser")
    for hidden in copy.select(".accesshide, .sr-only, .visually-hidden, script, style"):
        hidden.decompose()
    return _clean(copy.get_text(sep, strip=True))


def _multiline(element: Tag | None) -> str | None:
    if element is None:
        return None
    copy = BeautifulSoup(str(element), "html.parser")
    for hidden in copy.select(".accesshide, .sr-only, .visually-hidden, script, style"):
        hidden.decompose()
    lines = [_clean(line) for line in copy.get_text("\n", strip=True).splitlines()]
    text = "\n".join(line for line in lines if line)
    return text or None


def epoch_to_iso(value: int | str | None) -> str | None:
    if value in (None, "", 0, "0"):
        return None
    return (
        datetime.fromtimestamp(int(value), UTC)
        .astimezone(JST)
        .replace(tzinfo=None)
        .isoformat(timespec="minutes")
    )


def course_code(fullname: str, idnumber: str | None = None) -> str | None:
    match = re.search(r"\[([0-9][A-Z][0-9A-Z]{3,})\]", fullname or "")
    if match:
        return match.group(1)
    match = re.search(r"_([0-9][A-Z][0-9A-Z]{3,})$", idnumber or "")
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
        "visible": bool(raw.get("visible", True)),
        "hidden": bool(raw.get("hidden", False)),
        "url": str(raw.get("viewurl") or f"{base_url}/course/view.php?id={raw['id']}"),
    }


def _cmid(href: str) -> str | None:
    values = parse_qs(urlparse(href).query).get("id")
    return values[0] if values else None


def pluginfile_links(html: str | Tag, *, base_url: str) -> list[dict[str, str]]:
    soup = html if isinstance(html, Tag) else BeautifulSoup(html or "", "html.parser")
    out: list[dict[str, str]] = []
    seen: set[str] = set()
    for tag in soup.select("a[href*='/pluginfile.php/']"):
        url = urljoin(base_url + "/", str(tag["href"]).strip())
        clean_url = url.split("?", 1)[0]
        if clean_url in seen:
            continue
        seen.add(clean_url)
        name = _text(tag) or clean_url.rsplit("/", 1)[-1]
        out.append({"url": clean_url, "name": name})
    return out


def parse_course_page(html: str, *, base_url: str) -> dict[str, Any]:
    """Return ``{"summary", "sections", "activities"}`` from ``course/view.php``."""
    soup = BeautifulSoup(html or "", "html.parser")
    sections: list[dict[str, Any]] = []
    activities: list[dict[str, Any]] = []
    section_nodes = soup.select("li.section[data-sectionname], li.course-section[data-number]")
    if not section_nodes:
        section_nodes = [soup]  # flat fallback
    summary_parts: list[str] = []
    for node in section_nodes:
        number = node.get("data-number") if isinstance(node, Tag) else None
        name = _clean(node.get("data-sectionname")) if isinstance(node, Tag) else ""
        summary_el = node.select_one(".summarytext, .section-summary-text")
        section_summary = _multiline(summary_el)
        if number in ("0", None) and section_summary:
            summary_parts.append(section_summary)
        sections.append(
            {
                "number": int(number) if number and str(number).isdigit() else None,
                "name": name or None,
                "summary": section_summary,
            }
        )
        for li in node.select("li.activity[data-id]"):
            classes = " ".join(li.get("class", []))
            module_match = re.search(r"modtype_(\w+)", classes)
            if not module_match:
                continue
            module = module_match.group(1)
            cmid = str(li["data-id"])
            link = li.select_one(".activityname a[href], a.aalink[href]")
            href = urljoin(base_url + "/", str(link["href"]).strip()) if link else None
            title_el = li.select_one(".instancename")
            title = _text(title_el) or _clean(
                li.select_one("[data-activityname]").get("data-activityname")
                if li.select_one("[data-activityname]")
                else ""
            )
            content_el = li.select_one(".activity-altcontent, .contentafterlink, .description")
            body = _multiline(content_el)
            badge = _text(li.select_one(".activitybadge"))
            dates_text = _multiline(li.select_one("[data-region='activity-dates'], .activity-dates"))
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
                    "file_type": badge or None,
                    "dates_text": dates_text,
                    "section_number": int(number) if number and str(number).isdigit() else None,
                    "section_name": name or None,
                    "inline_files": pluginfile_links(content_el, base_url=base_url)
                    if content_el
                    else [],
                    "restricted": bool(li.select_one(".availabilityinfo, [data-region='availabilityinfo']")),
                }
            )
    return {
        "summary": "\n".join(summary_parts) or None,
        "sections": sections,
        "activities": activities,
    }


def parse_activity_page(html: str, *, module: str, base_url: str) -> dict[str, Any]:
    """Body text, dates and files from a page/folder/assign view page."""
    soup = BeautifulSoup(html or "", "html.parser")
    main = soup.select_one("#region-main [role='main'], #region-main") or soup
    for noisy in main.select(
        ".activity-header .completion-info, .activity-navigation, form, "
        "[data-region='activity-information'] .completion-info, .submissionlinks"
    ):
        noisy.decompose()
    intro = main.select_one(".activity-description, #intro, .box.generalbox")
    content = main.select_one(".box.generalbox.center, .page-content, #folder_tree0, .foldertree") or intro
    body_parts = [part for part in (_multiline(intro), _multiline(content)) if part]
    body = "\n\n".join(dict.fromkeys(body_parts)) or None
    dates: dict[str, str] = {}
    for row in main.select("[data-region='activity-dates'] div, .activity-dates div"):
        match = re.match(r"\s*([^:：\d]+)[:：]\s*(.+)", _clean(row.get_text(" ", strip=True)))
        if match:
            dates[match.group(1).strip()] = match.group(2).strip()
    status: dict[str, str] = {}
    for tr in main.select(".submissionstatustable tr, table.generaltable tr"):
        th, td = tr.select_one("th"), tr.select_one("td")
        if th and td:
            status[_text(th)] = _text(td)
    deadline = None
    for label, value in list(dates.items()) + list(status.items()):
        if re.search(r"期限|終了|Due|due", label):
            deadline = parse_japanese_datetime(value) or deadline
    submission = next(
        (value for key, value in status.items() if re.search(r"提出ステータス|Submission status", key)),
        None,
    )
    files = pluginfile_links(main, base_url=base_url)
    return {
        "body": body,
        "deadline": deadline,
        "submission_status": submission,
        "dates": dates or None,
        "files": files,
    }


_JP_DT = re.compile(
    r"(20\d{2})年\s*(\d{1,2})月\s*(\d{1,2})日(?:[^\d]{0,16}?(\d{1,2}):(\d{2}))?"
)


def parse_japanese_datetime(text: str | None) -> str | None:
    match = _JP_DT.search(text or "")
    if not match:
        return None
    year, month, day, hour, minute = match.groups()
    value = datetime(int(year), int(month), int(day), int(hour or 0), int(minute or 0))
    return value.isoformat(timespec="minutes")


def parse_forum_discussions(html: str, *, base_url: str) -> list[dict[str, Any]]:
    """Discussion rows from ``mod/forum/view.php`` (Moodle 4.x list view)."""
    soup = BeautifulSoup(html or "", "html.parser")
    out: list[dict[str, Any]] = []
    seen: set[str] = set()
    for link in soup.select("a[href*='/mod/forum/discuss.php']"):
        href = urljoin(base_url + "/", str(link["href"]).strip())
        discussion_id = (parse_qs(urlparse(href).query).get("d") or [None])[0]
        if not discussion_id or discussion_id in seen:
            continue
        title = _text(link)
        if not title:
            continue
        seen.add(discussion_id)
        row = link.find_parent("tr") or link.find_parent(attrs={"data-region": "discussion-list-item"})
        published = None
        if row is not None:
            stamp = row.select_one("time[data-timestamp], [data-timestamp]")
            if stamp is not None:
                published = epoch_to_iso(stamp.get("data-timestamp"))
        out.append(
            {
                "discussion_id": discussion_id,
                "title": title,
                "url": f"{base_url}/mod/forum/discuss.php?d={discussion_id}",
                "published_at": published,
            }
        )
    return out


def parse_forum_discussion(html: str, *, base_url: str) -> dict[str, Any]:
    soup = BeautifulSoup(html or "", "html.parser")
    first = soup.select_one(
        "[data-region-content='forum-post-core'], .post-content-container, .forumpost .content"
    )
    body = _multiline(first)
    stamp = soup.select_one("[data-region='post'] time[datetime], .forumpost time[datetime]")
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
        "body": body,
        "published_at": published,
        "files": pluginfile_links(first, base_url=base_url) if first else [],
    }


def calendar_deadlines(events: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    """Map course-module id -> {deadline, available_from} from calendar events."""
    out: dict[str, dict[str, Any]] = {}
    for event in events:
        url = str(event.get("url") or "")
        cmid = _cmid(url) if "/mod/" in url else None
        if not cmid:
            continue
        entry = out.setdefault(cmid, {"course_moodle_id": (event.get("course") or {}).get("id")})
        when = epoch_to_iso(event.get("timestart"))
        kind = str(event.get("eventtype") or "")
        if kind in {"open", "allowsubmissionsfromdate"}:
            entry["available_from"] = when
        elif kind in {"due", "close", "expectcompletionon", "gradingdue"} or not entry.get("deadline"):
            if kind == "gradingdue":
                continue
            entry["deadline"] = when
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
