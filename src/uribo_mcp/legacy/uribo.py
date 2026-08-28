from __future__ import annotations

import re
from urllib.parse import parse_qs, urlparse

from bs4 import BeautifulSoup

HOST = "kym22-web.ofc.kobe-u.ac.jp"
START_URL = f"https://{HOST}/campusweb/"


def _iso(text: str) -> str | None:
    m = re.search(r"(\d{4})/(\d{1,2})/(\d{1,2})(?:\s+(\d{1,2}):(\d{2}))?", text)
    if not m: return None
    y, mo, d, h, mi = m.groups(); base = f"{y}-{int(mo):02d}-{int(d):02d}"
    return f"{base}T{int(h):02d}:{mi}:00" if h else base


def parse_keiji_list(html: str, *, start_url: str = START_URL) -> list[dict]:
    soup = BeautifulSoup(html, "html.parser"); out = []; seen = set()
    for a in soup.find_all("a", href=True):
        q = parse_qs(urlparse(a["href"].replace("&amp;", "&")).query)
        if not q.get("seqNo") or not q.get("keijitype"): continue
        sid = f"keiji/{q['keijitype'][0]}/{q.get('genrecd', [''])[0]}/{q['seqNo'][0]}"
        title = re.sub(r"[\s　]+", " ", a.get_text()).strip()
        if not title or sid in seen: continue
        seen.add(sid); row = a.find_parent("tr"); cells = row.find_all("td") if row else []
        out.append({"source": "uribo", "source_id": sid, "category": "announcement", "title": title, "body": None, "url": start_url, "course_name": None, "published_at": _iso(cells[0].get_text(" ", strip=True)) if cells else None, "deadline": None})
    return out
