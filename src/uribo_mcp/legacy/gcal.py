"""Pure Google Calendar projection/diff functions; API client is host-injected."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

from .common import CATEGORY_LABELS, is_actionable_open, parse_datetime

EXT_KEY = "kobedai_key"; CALENDAR_TIME_ZONE = "Asia/Tokyo"; JST = timezone(timedelta(hours=9))


def make_key(source: str, source_id: str) -> str: return f"{source}:{source_id}"


def _description(item: dict) -> str: return "\n".join(x for x in (item.get("url"), f"ソース: {item.get('source', '')}") if x)


def build_desired_events(items: list[dict[str, Any]], now: datetime, *, past_days: int = 3, future_days: int = 120) -> list[dict]:
    result: dict[str, dict] = {}
    for item in items:
        deadline, published = parse_datetime(item.get("deadline")), parse_datetime(item.get("published_at"))
        date = deadline if is_actionable_open(item.get("category"), item.get("is_done"), item.get("deadline")) else (deadline or published if item.get("category") == "event" else None)
        if not date or not -past_days <= (date.date() - now.date()).days <= future_days: continue
        all_day = item.get("category") == "event"; key = make_key(item["source"], item["source_id"])
        result.setdefault(key, {"key": key, "summary": f"[{CATEGORY_LABELS.get(item.get('category'), 'その他')}] {item['title']}", "description": _description(item), "all_day": all_day, "start": date.date().isoformat() if all_day else date.isoformat(), "end": (date.date() + timedelta(days=1)).isoformat() if all_day else (date + timedelta(minutes=30)).isoformat()})
    return list(result.values())


def _equal(desired: dict, existing: dict) -> bool:
    return all(desired[k] == existing.get(k) for k in ("summary", "description", "all_day", "start", "end"))


def diff_events(desired: list[dict], existing: list[dict]) -> dict[str, list[dict]]:
    current = {x["key"]: x for x in existing if x.get("key")}; wanted = {x["key"] for x in desired}
    return {"create": [x for x in desired if x["key"] not in current], "update": [{**x, "event_id": current[x["key"]]["event_id"]} for x in desired if x["key"] in current and not _equal(x, current[x["key"]])], "delete": [x for key, x in current.items() if key not in wanted]}


def normalize_existing(event: dict) -> dict | None:
    key = ((event.get("extendedProperties") or {}).get("private") or {}).get(EXT_KEY)
    if not key: return None
    start, end = event.get("start", {}), event.get("end", {}); all_day = "date" in start
    return {"key": key, "event_id": event.get("id"), "summary": event.get("summary"), "description": event.get("description"), "all_day": all_day, "start": start.get("date") if all_day else start.get("dateTime"), "end": end.get("date") if all_day else end.get("dateTime")}


def to_gcal_body(event: dict) -> dict:
    body = {"summary": event["summary"], "description": event["description"], "extendedProperties": {"private": {EXT_KEY: event["key"]}}}
    body["start"] = {"date": event["start"]} if event["all_day"] else {"dateTime": event["start"], "timeZone": CALENDAR_TIME_ZONE}
    body["end"] = {"date": event["end"]} if event["all_day"] else {"dateTime": event["end"], "timeZone": CALENDAR_TIME_ZONE}; return body


def apply_diff(service, calendar_id: str, changes: dict[str, list[dict]], *, pause=None) -> dict[str, int]:
    """Apply a precomputed diff using an injected Calendar v3 service."""
    counts = {"create": 0, "update": 0, "delete": 0}
    for event in changes["create"]:
        service.events().insert(calendarId=calendar_id, body=to_gcal_body(event)).execute(); counts["create"] += 1
        if pause: pause()
    for event in changes["update"]:
        service.events().update(calendarId=calendar_id, eventId=event["event_id"], body=to_gcal_body(event)).execute(); counts["update"] += 1
        if pause: pause()
    for event in changes["delete"]:
        service.events().delete(calendarId=calendar_id, eventId=event["event_id"]).execute(); counts["delete"] += 1
        if pause: pause()
    return counts
