from __future__ import annotations

import re
from collections.abc import Sequence
from datetime import datetime
from difflib import SequenceMatcher
from typing import Any

from .common import normalize_text


def normalize_title(title: str | None) -> str:
    return re.sub(r"[^\w]", "", normalize_text(title))


class _UnionFind:
    def __init__(self): self.parent: dict[int, int] = {}
    def find(self, x: int) -> int:
        self.parent.setdefault(x, x)
        if self.parent[x] != x: self.parent[x] = self.find(self.parent[x])
        return self.parent[x]
    def union(self, a: int, b: int) -> None:
        a, b = self.find(a), self.find(b)
        if a != b: self.parent[max(a, b)] = min(a, b)


def cluster_items(items: Sequence[dict[str, Any]], *, now: datetime, threshold: float = .7, date_proximity_days: int = 3, window_days: int = 7) -> dict[int, int]:
    candidates = [{**x, "_norm": normalize_title(x.get("title"))} for x in items if x.get("date") and 0 <= (now.date() - x["date"].date()).days <= window_days]
    uf = _UnionFind()
    for n, left in enumerate(candidates):
        for right in candidates[n + 1:]:
            if left["source"] == right["source"] or abs((left["date"].date() - right["date"].date()).days) > date_proximity_days: continue
            if SequenceMatcher(None, left["_norm"], right["_norm"]).ratio() >= threshold: uf.union(left["id"], right["id"])
    groups: dict[int, list[int]] = {}
    for row in candidates: groups.setdefault(uf.find(row["id"]), []).append(row["id"])
    return {item_id: min(group) for group in groups.values() if len(group) > 1 for item_id in group}


def apply(conn, *, now: datetime | None = None, threshold: float = .7, date_proximity_days: int = 3, window_days: int = 7) -> dict[str, int]:
    now = now or datetime.now(); rows = conn.execute("SELECT id,source,title,published_at,created_at FROM items").fetchall()
    from .common import parse_datetime
    mapping = cluster_items([{"id": r["id"], "source": r["source"], "title": r["title"], "date": parse_datetime(r["published_at"]) or parse_datetime(r["created_at"])} for r in rows], now=now, threshold=threshold, date_proximity_days=date_proximity_days, window_days=window_days)
    current = {
        int(row["id"]): row["cluster_id"]
        for row in conn.execute("SELECT id,cluster_id FROM items")
    }
    for item_id, previous in current.items():
        cluster_id = mapping.get(item_id)
        if previous != cluster_id:
            conn.execute(
                "UPDATE items SET cluster_id=? WHERE id=?", (cluster_id, item_id)
            )
    conn.commit(); return {"clusters": len(set(mapping.values())), "clustered_items": len(mapping)}
