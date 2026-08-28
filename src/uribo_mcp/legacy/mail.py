"""Pure Gmail message conversion; OAuth/service creation belongs to the host app."""
from __future__ import annotations

import base64
import re
from datetime import UTC, datetime, timedelta, timezone
from email.utils import parseaddr

from bs4 import BeautifulSoup

JST = timezone(timedelta(hours=9)); MAIL_URL_TEMPLATE = "https://mail.google.com/mail/u/0/#inbox/{id}"
UNIVERSITY_RE = re.compile(r"@(?:[\w-]+\.)*kobe-u\.ac\.jp", re.I)
EXCLUDED_SENDER_HOSTS = {"beefplus.center.kobe-u.ac.jp", "ventureplus.center.kobe-u.ac.jp"}
SUBJECT_PREFIX_RE = re.compile(r"^\s*(?:FW|FWD|Re|転送|返信)\s*[:：]\s*", re.I)


def header_value(message: dict, name: str) -> str:
    return next((h.get("value", "") for h in message.get("payload", {}).get("headers", []) if h.get("name", "").lower() == name.lower()), "")


def all_header_values(message: dict, name: str) -> list[str]:
    return [h.get("value", "") for h in message.get("payload", {}).get("headers", []) if h.get("name", "").lower() == name.lower()]


def strip_subject_prefixes(subject: str) -> str:
    previous = None; value = subject or ""
    while previous != value: previous, value = value, SUBJECT_PREFIX_RE.sub("", value, count=1)
    return value.strip()


def is_target_mail(message: dict) -> bool:
    _, sender = parseaddr(header_value(message, "From")); host = sender.rsplit("@", 1)[-1].lower() if "@" in sender else ""
    if host in EXCLUDED_SENDER_HOSTS: return False
    recipients = " ".join(v for h in ("To", "Cc", "Resent-From", "Resent-To", "Delivered-To", "X-Forwarded-To") for v in all_header_values(message, h))
    return bool(UNIVERSITY_RE.search(recipients))


def _walk_parts(payload: dict):
    if payload.get("parts"):
        for part in payload["parts"]: yield from _walk_parts(part)
    else:
        data = payload.get("body", {}).get("data")
        if data: yield payload.get("mimeType", ""), base64.urlsafe_b64decode(data + "=" * (-len(data) % 4))


def extract_body(message: dict) -> str:
    plain, html = [], []
    for mime, raw in _walk_parts(message.get("payload", {})):
        text = raw.decode("utf-8", errors="replace")
        if mime == "text/plain": plain.append(text)
        elif mime == "text/html": html.append(text)
    value = "\n".join(plain) if plain else BeautifulSoup("\n".join(html), "html.parser").get_text("\n", strip=True)
    return re.sub(r"\n{3,}", "\n\n", value).strip()


def internal_date_to_iso(value: str | int | None) -> str | None:
    return datetime.fromtimestamp(int(value) / 1000, tz=UTC).astimezone(JST).isoformat() if value not in (None, "") else None


def parse_message(message: dict) -> dict:
    msg_id = message["id"]
    return {"source": "mail", "source_id": msg_id, "category": "announcement", "title": strip_subject_prefixes(header_value(message, "Subject")), "body": extract_body(message) or None, "url": MAIL_URL_TEMPLATE.format(id=msg_id), "course_name": None, "published_at": internal_date_to_iso(message.get("internalDate")), "deadline": None}


def build_query(after_epoch: int | None, *, backfill_days: int = 30) -> str:
    excluded = " ".join(f"-from:{host}" for host in sorted(EXCLUDED_SENDER_HOSTS)); base = f"to:kobe-u.ac.jp {excluded}"
    return f"{base} after:{after_epoch}" if after_epoch is not None else f"{base} newer_than:{backfill_days}d"


def fetch_messages(service, query: str, *, pause=None) -> list[dict]:
    """Fetch and filter Gmail resources through an injected Gmail API service.

    Token acquisition is intentionally outside this package; callers may use
    Credential Manager/keyring without exposing secrets to the scraper.
    """
    ids: list[str] = []; page_token = None
    while True:
        page = service.users().messages().list(userId="me", q=query, pageToken=page_token).execute()
        ids.extend(x["id"] for x in page.get("messages", [])); page_token = page.get("nextPageToken")
        if not page_token: break
    items = []
    for message_id in ids:
        message = service.users().messages().get(userId="me", id=message_id, format="full").execute()
        if is_target_mail(message): items.append(parse_message(message))
        if pause: pause()
    return items
