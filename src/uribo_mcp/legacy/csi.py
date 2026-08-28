from __future__ import annotations

import hashlib
import re
from urllib.parse import urljoin

from bs4 import BeautifulSoup

PAGE_URL = "https://www.csi.kobe-u.ac.jp/current_students/"


def parse_announcements(html: str, *, page_url: str = PAGE_URL) -> list[dict]:
    soup = BeautifulSoup(html, "html.parser"); out = []
    for li in soup.select("ul.newsList li"):
        txt = li.select_one("p.news-txt")
        if not txt: continue
        title = re.sub(r"[\s　]+", " ", txt.get_text(" ", strip=True)).strip()
        if not title: continue
        time = li.select_one("time.news-date"); raw = (time.get_text(strip=True) if time else "") or (time.get("datetime", "") if time else "")
        m = re.search(r"(\d{4})[/-](\d{1,2})[/-](\d{1,2})", raw)
        published = f"{m.group(1)}-{int(m.group(2)):02d}-{int(m.group(3)):02d}" if m else None
        a = li.find("a", href=True)
        out.append({"source": "csi", "source_id": f"{published or 'nodate'}/{hashlib.sha1(title.encode()).hexdigest()[:16]}", "category": "announcement", "title": title, "body": None, "url": urljoin(page_url, a["href"]) if a else page_url, "course_name": None, "published_at": published, "deadline": None})
    return out


def fetch_html(get, *, page_url: str = PAGE_URL) -> str:
    """Fetch through an injected requests-compatible ``get`` function."""
    response = get(page_url); response.raise_for_status(); return response.content.decode("utf-8", errors="replace")
