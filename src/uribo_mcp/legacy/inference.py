from __future__ import annotations

from collections.abc import Iterable


def diff_submitted(previous_ids: Iterable[str], current_ids: Iterable[str]) -> set[str]:
    return set(previous_ids) - set(current_ids)


def load_task_snapshot(conn) -> set[str]:
    return {row[0] for row in conn.execute("SELECT source_id FROM task_snapshot")}


def save_task_snapshot(conn, current_ids: Iterable[str]) -> None:
    current = set(current_ids)
    if load_task_snapshot(conn) == current:
        return
    conn.execute("DELETE FROM task_snapshot"); conn.executemany("INSERT OR IGNORE INTO task_snapshot (source_id) VALUES (?)", [(x,) for x in current]); conn.commit()


def apply_task_inference(conn, current_task_ids: Iterable[str], *, source: str = "beef") -> set[str]:
    current = set(current_task_ids); disappeared = diff_submitted(load_task_snapshot(conn), current); marked = set()
    for sid in disappeared:
        result = conn.execute("UPDATE items SET is_done=1 WHERE source=? AND source_id=? AND is_done=0", (source, sid))
        if result.rowcount: marked.add(sid)
    conn.commit(); save_task_snapshot(conn, current); return marked
