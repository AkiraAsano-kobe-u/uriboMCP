from __future__ import annotations

import datetime as dt
import hashlib
import io
import re
from typing import Protocol

BASE_URL = "https://www.csi.kobe-u.ac.jp/current_students/schedule/"


class PdfOpener(Protocol):
    def __call__(self, source: io.BytesIO): ...


def term_pdf_name(year: int, first_half: bool) -> str:
    return f"{year}{'a' if first_half else 'b'}_schedule.pdf"


def current_term(today: dt.date | None = None) -> tuple[int, bool]:
    today = today or dt.date.today()
    if 4 <= today.month <= 9: return today.year, True
    return (today.year, False) if today.month >= 10 else (today.year - 1, False)


def parse_calendar(pdf_bytes: bytes, pdf_open) -> list[dict]:
    """Parse with an injected ``pdfplumber.open`` compatible callable."""
    rows: list[list] = []; title = ""
    with pdf_open(io.BytesIO(pdf_bytes)) as pdf:
        lines = (pdf.pages[0].extract_text() or "").splitlines(); title = lines[0].strip() if lines else ""
        for page in pdf.pages:
            for table in page.extract_tables():
                head = ["".join((c or "").split()) for c in (table[0] if table else [])]
                if head[:5] == ["年", "月", "日", "曜", "一般行事"]: rows.extend(table[1:])
    year = month = day = None; weekday = None; previous_month = None; out = []
    for raw in rows:
        y, mo, d, w, event = (list(raw) + [None] * 5)[:5]
        year_given = bool(y and str(y).strip().isdigit())
        if year_given: year = int(str(y).strip())
        if mo and str(mo).strip().isdigit():
            new_month = int(str(mo).strip())
            if not year_given and year is not None and previous_month is not None and new_month < previous_month: year += 1
            month, previous_month = new_month, new_month
        if d and str(d).strip().isdigit(): day = int(str(d).strip())
        if w and str(w).strip(): weekday = str(w).strip()
        if not event or year is None or month is None or day is None: continue
        try: date = dt.date(year, month, day)
        except ValueError: continue
        for line in str(event).split("\n"):
            text = re.sub(r"\s+", " ", line).strip()
            if not text: continue
            iso = date.isoformat(); digest = hashlib.sha1(f"{iso}|{text}".encode()).hexdigest()[:16]
            out.append({"source": "pdf_calendar", "source_id": f"{iso}/{digest}", "category": "event", "title": f"{date.month}/{date.day}{f'({weekday})' if weekday else ''} {text}", "body": title or None, "url": None, "course_name": None, "published_at": None, "deadline": iso})
    return out


def fetch_pdf(get, year: int, first_half: bool, *, base_url: str = BASE_URL) -> bytes:
    response = get(base_url + term_pdf_name(year, first_half)); response.raise_for_status(); return response.content
