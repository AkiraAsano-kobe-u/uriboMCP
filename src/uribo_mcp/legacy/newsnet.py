from __future__ import annotations

from datetime import UTC, datetime, timedelta, timezone

from bs4 import BeautifulSoup

FEED_URL = "https://kobe-u-newsnet.com/feed/"
JST = timezone(timedelta(hours=9))


def parse_feed(content: bytes, parser) -> list[dict]:
    """Convert a feedparser-compatible parsed feed to canonical items."""
    out = []
    for entry in parser(content).entries:
        title, link = (entry.get("title") or "").strip(), entry.get("link")
        if not title or not link: continue
        date = entry.get("published_parsed") or entry.get("updated_parsed")
        published = datetime(*date[:6], tzinfo=UTC).astimezone(JST).isoformat() if date else None
        summary = entry.get("summary"); body = BeautifulSoup(summary, "html.parser").get_text(" ", strip=True) if summary else None
        out.append({"source": "newsnet", "source_id": entry.get("id") or link, "category": "news", "title": title, "body": body, "url": link, "course_name": None, "published_at": published, "deadline": None})
    return out


def fetch_feed(get, *, feed_url: str = FEED_URL) -> bytes:
    response = get(feed_url); response.raise_for_status(); return response.content
