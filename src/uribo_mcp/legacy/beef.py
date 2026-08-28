"""BEEF+ / BEEF+ Venture parsers and injectable Selenium access helpers."""
from __future__ import annotations

import re
from collections.abc import Callable
from typing import Protocol
from urllib.parse import parse_qs, urljoin, urlparse

from bs4 import BeautifulSoup

from .common import clean

BASE = "https://beefplus.center.kobe-u.ac.jp"
VENTURE_BASE = "https://ventureplus.center.kobe-u.ac.jp"
VENTURE_IDNUMBER = "99992025-10079902"


class PageDriver(Protocol):
    current_url: str
    page_source: str
    def get(self, url: str) -> None: ...


def fetch_pages(driver: PageDriver, urls: dict[str, str], *, logged_in: Callable[[PageDriver], bool] | None = None) -> dict[str, str]:
    """Fetch named pages with an already authenticated, caller-owned driver."""
    pages: dict[str, str] = {}
    for name, url in urls.items():
        driver.get(url)
        if logged_in and not logged_in(driver):
            raise RuntimeError(f"session is not authenticated while opening {url}")
        pages[name] = driver.page_source
    return pages


def _iso(text: str) -> str | None:
    m = re.search(r"(\d{4})/(\d{1,2})/(\d{1,2})(?:\s+(\d{1,2}):(\d{2}))?", text)
    if not m: return None
    y, mo, d, h, mi = m.groups(); date = f"{y}-{int(mo):02d}-{int(d):02d}"
    return f"{date}T{int(h):02d}:{mi}:00" if h else date


def _id_from_url(url: str) -> tuple[str | None, str | None]:
    q = parse_qs(urlparse(url).query)
    idnumber = (q.get("idnumber") or [None])[0]
    for key in ("examinationId", "surveyId", "reportId", "contentId", "taskId"):
        if q.get(key): return idnumber, q[key][0]
    return idnumber, None


def parse_timetable(html: str) -> list[dict]:
    soup = BeautifulSoup(html, "html.parser")
    term = soup.select_one("#kikanCd option[selected]")
    quarter = clean(term.get_text()) if term else None
    rows: list[dict] = []
    for tr in soup.select(".div-table-data-row"):
        period_el = tr.select_one(".div-table-colomn-period")
        period_m = re.search(r"(\d)", (period_el.get_text() if period_el else "").translate(str.maketrans("０１２３４５６７８９", "0123456789")))
        if not period_m: continue
        for cell in tr.select("div[class*='-yobicol']"):
            day_m = re.search(r"(\d)-yobicol", " ".join(cell.get("class", [])))
            if not day_m: continue
            for block in cell.select(".clearfix"):
                header = block.select_one(".divTableCellHeader")
                if not header: continue
                name = re.sub(r"^[0-9A-Za-z]+\s+", "", clean(header.get_text()))
                room = block.select_one(".div-table-cell-info")
                rows.append({"day_of_week": int(day_m.group(1)), "period": int(period_m.group(1)), "course_name": name,
                             "room": clean(room.get_text()) or None if room else None, "quarter": quarter or None,
                             "idnumber": (header.get("id") or "").strip() or None})
    return rows


def parse_tasks(html: str, *, base: str = BASE, source: str = "beef") -> list[dict]:
    soup = BeautifulSoup(html, "html.parser"); result = []
    category_map = (("テスト", "test"), ("アンケート", "survey"), ("課題", "assignment"), ("レポート", "assignment"))
    for row in soup.select("#taskList .result_list_content .result_list_line"):
        title_el = row.select_one(".tasklist-title")
        if not title_el: continue
        title = clean(title_el.get_text())
        if not title: continue
        course, content, deadline_el = (row.select_one(x) for x in (".tasklist-course", ".tasklist-contents", ".tasklist-deadline"))
        content_text = clean(content.get_text()) if content else ""
        category = next((v for k, v in category_map if k in content_text), "assignment")
        link = title_el.find("a", href=True) or row.find("a", href=True)
        url = urljoin(base, link["href"]) if link else f"{base}/lms/task"
        idnumber, cid = _id_from_url(url)
        digest = cid or f"{abs(hash((title, url))):x}"
        result.append({"source": source, "source_id": f"{category}/{idnumber or 'unknown'}/{digest}", "category": category,
                       "title": title, "body": None, "url": url, "course_name": clean(course.get_text()) if course else None,
                       "published_at": None, "deadline": _iso(deadline_el.get_text()) if deadline_el else None})
    return result


def info_idnumbers(*htmls: str) -> dict[str, str]:
    mapping: dict[str, str] = {}
    for html in htmls:
        soup = BeautifulSoup(html or "", "html.parser")
        for cell in soup.select(".update-info-cell"):
            def get(name, current=cell):
                return (current.find("input", id=name) or {}).get("value")

            if get("module") == "information" and get("contentId") and get("idnumber"):
                mapping.setdefault(get("contentId"), get("idnumber"))
        for link in soup.select("#ctrl_menu_info a[data1][data2]"):
            mapping.setdefault(link["data1"], link["data2"])
    return mapping


def parse_information_list(html: str, idnumber_by_info: dict[str, str] | None = None, *, base: str = BASE, source: str = "beef") -> list[dict]:
    soup = BeautifulSoup(html, "html.parser"); ids = {**info_idnumbers(html), **(idnumber_by_info or {})}; out = []
    for link in soup.select("a.information-list-title"):
        m = re.search(r"InfoDetail\(event,\s*(\d+)\)", link.get("onclick", ""))
        if not m: continue
        info_id = m.group(1); row = link.parent; span = link.find("span"); title = clean((span or link).get_text())
        if not title: continue
        idn = ids.get(info_id)
        out.append({"source": source, "source_id": f"announcement/info/{info_id}", "category": "announcement", "title": title, "body": None,
                    "url": f"{base}/lms/course?idnumber={idn}" if idn else f"{base}/lms/course/information/list",
                    "course_name": clean(row.select_one(".information-list-course-name").get_text()) if row.select_one(".information-list-course-name") else None,
                    "published_at": _iso(row.select_one(".information-list-date-from").get_text()) if row.select_one(".information-list-date-from") else None, "deadline": None})
    return out


def parse_updateinfo(html: str, *, base: str = BASE, source: str = "beef", include_materials: bool = False) -> list[dict]:
    soup = BeautifulSoup(html, "html.parser"); out = []; seen: set[str] = set(); categories = {"question": "survey", "test": "test", "report": "assignment"}
    for cell in soup.select(".update-info-student.update-info-cell"):
        def get(name, current=cell):
            return (current.find("input", id=name) or {}).get("value")

        module, action = get("module"), get("info_action")
        category = "material" if module == "material" and include_materials and action in {"add", "update"} else categories.get(module or "")
        if not category or (module != "material" and action != "add"): continue
        idn, cid = get("idnumber"), get("contentId")
        if not cid: continue
        sid = f"{category}/{idn or 'unknown'}/{cid}"
        if sid in seen: continue
        button = cell.select_one(".update-info-url button"); spans = button.find_all("span") if button else []
        message = clean(spans[-1].get_text()) if spans else ""; tm = re.search(r"[(（](.+)[)）]が(?:追加|更新)されました", message)
        title = tm.group(1) if tm else message.lstrip("・")
        if not title: continue
        course_m = re.search(r"\[\S*\s+(.+?)\]", clean(spans[0].get_text())) if spans else None
        label = cell.select_one(".update-info-url label")
        seen.add(sid); out.append({"source": source, "source_id": sid, "category": category, "title": title, "body": None,
                                   "url": urljoin(base, button.get("value", "")) if button else f"{base}/updateinfo", "course_name": course_m.group(1) if course_m else None,
                                   "published_at": _iso(label.get_text()) if label else None, "deadline": None})
    return out


def parse_venture_pages(pages: dict[str, str]) -> dict[str, list[dict]]:
    """Parse an injected Venture page set; caller controls authentication/fetching."""
    ids = info_idnumbers(pages.get("updateinfo", ""), pages.get("information", ""))
    items = parse_tasks(pages.get("task", ""), base=VENTURE_BASE, source="venture") + parse_information_list(pages.get("information", ""), ids, base=VENTURE_BASE, source="venture") + parse_updateinfo(pages.get("updateinfo", ""), base=VENTURE_BASE, source="venture", include_materials=True)
    return {"items": [i for i in items if VENTURE_IDNUMBER in i["source_id"] or i["source_id"].startswith("announcement/")]}
