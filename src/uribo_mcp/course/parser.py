"""BEEF+ course/detail HTML parsers.

The LMS has changed class names between releases, so this module primarily
uses URLs and data attributes (the stable parts of its rendered HTML) and only
uses CSS classes as fallbacks.  It has no browser, credential, or database
dependency by design.
"""
from __future__ import annotations

import hashlib
import mimetypes
import re
from datetime import datetime
from urllib.parse import parse_qs, urljoin, urlparse

from bs4 import BeautifulSoup, Tag

from .models import Attachment, CourseContent, CoursePage

_ID_KEYS = ("materialId", "reportId", "assignmentId", "surveyId", "examinationId", "testId", "attendanceId", "infoId", "contentId", "id")
_CATEGORY_BY_KEY = {
    "materialId": "material", "reportId": "assignment", "assignmentId": "assignment",
    "surveyId": "survey", "examinationId": "test", "testId": "test",
    "attendanceId": "attendance", "infoId": "announcement",
}
_CATEGORY_BY_MODULE = {
    "material": "material",
    "information": "announcement",
    "announcement": "announcement",
    "report": "assignment",
    "assignment": "assignment",
    "test": "test",
    "examination": "test",
    "question": "survey",
    "survey": "survey",
    "attendance": "attendance",
}
_CATEGORY_WORDS = (("アンケート", "survey"), ("出席", "attendance"), ("テスト", "test"), ("試験", "test"), ("課題", "assignment"), ("レポート", "assignment"), ("お知らせ", "announcement"), ("教材", "material"), ("資料", "material"))


def _clean(value: str | None) -> str:
    return re.sub(r"[\s\u3000]+", " ", value or "").strip()


def _stable(*parts: str) -> str:
    return hashlib.sha256("\x1f".join(parts).encode("utf-8")).hexdigest()[:24]


def _text(element: Tag | None) -> str:
    return _clean(element.get_text(" ", strip=True) if element else "")


def _iso(value: str) -> str | None:
    value = value.translate(str.maketrans("０１２３４５６７８９", "0123456789"))
    match = re.search(r"(20\d{2})[/.年-](\d{1,2})[/.月-](\d{1,2})(?:日)?(?:\s+(\d{1,2}):(\d{2}))?", value)
    if not match:
        return None
    year, month, day, hour, minute = match.groups()
    try:
        date = datetime(int(year), int(month), int(day), int(hour or 0), int(minute or 0))
    except ValueError:
        return None
    return date.isoformat(timespec="minutes") if hour else date.date().isoformat()


def _href_id(href: str) -> tuple[str | None, str | None, str | None]:
    query = parse_qs(urlparse(href).query)
    idnumber = (query.get("idnumber") or [None])[0]
    for key in _ID_KEYS:
        if query.get(key):
            return idnumber, query[key][0], _CATEGORY_BY_KEY.get(key)
    return idnumber, None, None


def _category(element: Tag, href: str | None = None) -> str:
    if href:
        _, _, from_url = _href_id(href)
        if from_url:
            return from_url
    module = str(element.get("data-module") or element.get("data-type") or "").casefold()
    if module in _CATEGORY_BY_MODULE:
        return _CATEGORY_BY_MODULE[module]
    raw = " ".join([" ".join(element.get("class", [])), element.get("data-module", ""), element.get("data-type", ""), _text(element)])
    raw = raw.lower()
    for word, category in _CATEGORY_WORDS:
        if word.lower() in raw:
            return category
    return "material"


def _content_id(element: Tag, href: str | None, category: str, idnumber: str) -> str:
    if href:
        _, value, _ = _href_id(href)
        if value:
            return value
    for key in ("data-content-id", "data-id", "data-module-id", "id"):
        value = (element.get(key) or "").strip()
        if value:
            return value
    return "derived-" + _stable(idnumber, category, _text(element), href or "")


def _named_value(container: Tag, *names: str) -> str | None:
    lowered = {name.lower() for name in names}
    for element in container.select("input, [data-object-name], [data-resource-id]"):
        name = str(element.get("name") or element.get("id") or "").lower()
        if name in lowered:
            value = str(element.get("value") or "").strip()
            if value:
                return value
        for attribute in ("data-object-name", "data-resource-id", "data-content-id"):
            if attribute.replace("data-", "").replace("-", "") in {
                value.replace("-", "").lower() for value in lowered
            }:
                value = str(element.get(attribute) or "").strip()
                if value:
                    return value
    return None


def _attachments(container: Tag, base_url: str, idnumber: str) -> tuple[Attachment, ...]:
    attachments: list[Attachment] = []
    seen: set[str] = set()
    candidates = container.select(
        "a[href], a[onclick], button[onclick], button[value], "
        ".fileDownload, .downloadFile"
    )
    for anchor in candidates:
        classes = set(anchor.get("class") or [])
        is_report_download = "downloadFile" in classes
        is_known_trigger = is_report_download or "fileDownload" in classes
        raw_href = str(anchor.get("href") or anchor.get("value") or "").strip()
        onclick = str(anchor.get("onclick") or "")
        direct_target = " ".join((raw_href, onclick, _text(anchor)))
        direct_file = bool(
            anchor.has_attr("download")
            or re.search(
                r"\.(pdf|docx?|pptx?|xlsx?|csv|txt|jpe?g|png|gif|webp)"
                r"(?:$|[?#\s'\"])",
                direct_target,
                re.I,
            )
            or any(
                token in direct_target.lower()
                for token in (
                    "download",
                    "filedown",
                    "setfiledown",
                    "make/tempfile",
                )
            )
        )
        course_result = anchor.find_parent(class_="course-result-list")
        if not is_known_trigger and course_result is None and not direct_file:
            continue
        field_container = (
            course_result
            or anchor.parent
            or container
        )
        quoted = re.findall(r"""['"]([^'"]+)['"]""", onclick)
        href = urljoin(base_url, raw_href) if raw_href and not raw_href.startswith("javascript:") else None
        name = (
            _text(field_container.select_one(".fileName"))
            or _text(anchor)
            or anchor.get("download")
            or _named_value(
                field_container, "fileName", "displayName", "resourceName"
            )
            or "attachment"
        )
        # Course/content links are not attachments; download links and file-like names are.
        target_text = " ".join(filter(None, (href, onclick, name)))
        is_file = bool(
            anchor.has_attr("download")
            or re.search(
                r"\.(pdf|docx?|pptx?|xlsx?|csv|txt|jpe?g|png|gif|webp)(?:$|[?#\s'\"])",
                target_text,
                re.I,
            )
        )
        is_file = is_file or any(
            token in target_text.lower()
            for token in ("download", "filedown", "setfiledown", "make/tempfile")
        )
        if not is_file:
            continue
        resource_id = (
            anchor.get("data-resource-id")
            or _text(field_container.select_one(".resource_Id"))
            or _named_value(field_container, "resourceId", "resource-id")
        )
        content_id = (
            anchor.get("data-content-id")
            or _named_value(
                field_container, "dlMaterialId", "contentId", "materialId"
            )
        )
        report_id = _named_value(container, "reportId")
        if is_report_download and report_id:
            content_id = report_id
        object_name = (
            anchor.get("data-object-name")
            or _text(field_container.select_one(".objectName"))
            or _named_value(field_container, "objectName", "object-name")
        )
        if not object_name:
            object_name = next(
                (
                    value
                    for value in quoted
                    if "/" in value or re.search(r"\.[A-Za-z0-9]{2,6}$", value)
                ),
                None,
            )
        identity = str(resource_id or object_name or href or name)
        stable_id = "file/" + _stable(idnumber, str(content_id or ""), identity)
        if stable_id in seen:
            continue
        seen.add(stable_id)
        size_match = re.search(
            r"([\d,]+)\s*(?:bytes?|b)\b", _text(anchor.parent), re.I
        )
        open_end = (
            _text(field_container.select_one(".openEndDate"))
            or _named_value(
                field_container, "openEndDate", "openEndAt", "openEnd"
            )
        )
        scan_status = (
            _text(field_container.select_one(".scanStatus"))
            or _named_value(field_container, "scanStatus", "virusScanStatus")
        )
        mime_type = mimetypes.guess_type(name)[0]
        metadata = {"onclick": onclick} if onclick else {}
        if open_end:
            metadata["end_date_raw"] = open_end
        if is_report_download:
            metadata["download_kind"] = "report_submission"
            if report_id:
                metadata["report_id"] = report_id
        attachments.append(
            Attachment(
                stable_id,
                name,
                href,
                size_bytes=(
                    int(size_match.group(1).replace(",", "")) if size_match else None
                ),
                content_id=str(content_id) if content_id else None,
                resource_id=str(resource_id) if resource_id else None,
                object_name=str(object_name) if object_name else None,
                scan_status=scan_status,
                open_end_at=_iso(open_end or ""),
                mime_type=mime_type,
                metadata=metadata,
            )
        )
    return tuple(attachments)


def _date_fields(text: str) -> tuple[str | None, str | None, str | None]:
    published = deadline = until = None
    for label, value in re.findall(r"([^：:\n]{1,16})[：:]\s*([^\n]+)", text):
        date = _iso(value)
        if not date:
            continue
        if any(word in label for word in ("期限", "締切", "提出")):
            deadline = date
        elif any(word in label for word in ("公開終了", "終了", "利用期限")):
            until = date
        elif any(word in label for word in ("公開", "掲載", "作成")):
            published = date
    return published, deadline, until


def _status(text: str) -> str | None:
    if any(word in text for word in ("未提出", "未回答", "未受験")):
        return "not_submitted"
    if any(word in text for word in ("提出済", "回答済", "受験済", "完了")):
        return "submitted"
    if "下書き" in text:
        return "draft"
    return None


def _title(element: Tag) -> str:
    candidate = element.select_one(
        ".title, .contents-title, .material-title, .report-title, "
        ".fileDownload, .course-view-material-file-name, "
        ".material-file-name, h1, h2, h3, h4, a"
    )
    return _text(candidate) or _text(element).split(" ")[0]


def _content_candidates(soup: BeautifulSoup) -> list[Tag]:
    selectors = (
        "[data-module]",
        "[data-content-id]",
        ".material, .contents-material, .course-content, .contents-item, "
        ".result_list_line, .course-result-list",
    )
    candidates: list[Tag] = []
    seen: set[int] = set()
    for selector in selectors:
        for element in soup.select(selector):
            if id(element) not in seen:
                seen.add(id(element))
                candidates.append(element)
    # Link-only layouts are common after a responsive theme upgrade.
    for link in soup.select("a[href]"):
        _, content_id, category = _href_id(link["href"])
        if content_id and category and id(link) not in seen:
            seen.add(id(link))
            candidates.append(link)
    return candidates


def parse_course_page(html: str, *, idnumber: str | None = None, base_url: str = "https://beefplus.center.kobe-u.ac.jp") -> dict:
    """Parse a course page without side effects.

    Returns ``CoursePage.to_dict()``. Content ``stable_id`` values use LMS IDs
    whenever present and a deterministic SHA-256 fallback otherwise.
    """
    soup = BeautifulSoup(html, "html.parser")
    page_id = idnumber or (soup.select_one("input[name=idnumber]") or {}).get("value") or "unknown"
    heading = soup.select_one("#courseName, .course-name, .course-title, h1")
    title = _text(heading) or page_id
    overview_el = soup.select_one("#courseOverview, .course-overview, .course-description, .course-summary")
    overview = _text(overview_el) or None
    contents: list[CourseContent] = []
    seen: set[str] = set()
    for element in _content_candidates(soup):
        link = element if element.name == "a" and element.has_attr("href") else element.select_one("a[href]")
        href = urljoin(base_url, link["href"]) if link else None
        category = _category(element, href)
        content_id = _content_id(element, href, category, page_id)
        stable_id = f"{category}/{page_id}/{content_id}"
        if stable_id in seen:
            continue
        content_title = _title(element)
        if not content_title:
            continue
        seen.add(stable_id)
        raw_text = _text(element)
        published, deadline, until = _date_fields(raw_text)
        group = element.find_parent(class_=re.compile(r"group|section|topic", re.I))
        group_title = _text(group.select_one("h1,h2,h3,h4,.title,.group-title")) if group else None
        attachments = _attachments(element, base_url, page_id)
        body_el = element.select_one(".description, .contents-description, .summary, .detail")
        query = parse_qs(urlparse(href).query) if href else {}
        metadata = {
            key: values[0]
            for key, values in query.items()
            if values
            and key
            in {
                "idnumber",
                "informationId",
                "infoId",
                "rishunen",
                "materialId",
                "reportId",
                "testId",
                "surveyId",
                "attendanceId",
            }
        }
        hidden_rishunen = _named_value(element, "rishunen")
        if hidden_rishunen:
            metadata["rishunen"] = hidden_rishunen
        contents.append(CourseContent(
            stable_id=stable_id, category=category, title=content_title, url=href,
            body=_text(body_el) or None, published_at=published, deadline=deadline,
            available_until=until, submission_status=_status(raw_text), group_title=group_title or None,
            attachments=attachments, metadata=metadata,
        ))
    return CoursePage(str(page_id), title, overview, tuple(contents)).to_dict()


def _parse_detail(html: str, *, category: str, idnumber: str | None, content_id: str | None, base_url: str) -> dict:
    soup = BeautifulSoup(html, "html.parser")
    root = soup.select_one("#modalContents, #contents, .modal-body, .detail, main") or soup
    title = _text(root.select_one(".title, .contents-title, h1, h2, h3")) or "Untitled"
    text = _text(root)
    inferred_id, url_id, _ = _href_id(next((a["href"] for a in root.select("a[href]") if _href_id(a["href"])[1]), ""))
    resolved_course = idnumber or inferred_id or "unknown"
    resolved_content = content_id or url_id or "derived-" + _stable(category, resolved_course, title, text)
    published, deadline, until = _date_fields(text)
    body_el = root.select_one(".body, .description, .detail-body, .contents-detail")
    content = CourseContent(
        stable_id=f"{category}/{resolved_course}/{resolved_content}", category=category,
        title=title, url=None, body=_text(body_el) or text or None, published_at=published,
        deadline=deadline, available_until=until, submission_status=_status(text),
        attachments=_attachments(root, base_url, resolved_course),
    )
    return content.to_dict()


def parse_announcement_detail(html: str, *, idnumber: str | None = None, info_id: str | None = None, base_url: str = "https://beefplus.center.kobe-u.ac.jp") -> dict:
    """Parse the HTML returned by BEEF+'s announcement detail modal."""
    return _parse_detail(html, category="announcement", idnumber=idnumber, content_id=info_id, base_url=base_url)


def parse_assignment_detail(html: str, *, idnumber: str | None = None, report_id: str | None = None, base_url: str = "https://beefplus.center.kobe-u.ac.jp") -> dict:
    """Parse an assignment detail page; it never opens a submission form."""
    return _parse_detail(html, category="assignment", idnumber=idnumber, content_id=report_id, base_url=base_url)
