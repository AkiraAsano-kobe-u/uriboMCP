"""search_course_content: SQL instr() scan must equal the original Python scan."""

from __future__ import annotations

import sqlite3
from collections.abc import Sequence
from pathlib import Path
from typing import Any

import pytest

from uribo_mcp.db import database
from uribo_mcp.models import AttachmentRecord, CourseRecord, ItemRecord
from uribo_mcp.repository import Repository, normalize_search

CHUNKS: dict[str, list[str]] = {
    # file_name -> chunk texts (one chunk per entry)
    "c_intro.pdf": [
        "Ｃ言語入門\n第1回：Ｃ言語の歴史と  コンパイラ。\n\n誤差は扱わない。",
        "c言語のポインタ。TAYLOR 展開は別資料。",
    ],
    "taylor.pdf": [
        "テイラー展開と誤差。Taylor series の打ち切り誤差、丸め誤差、誤差の伝播。",
        "収束判定\t広義積分\r\n行列の固有値",
        "固有値 固有値 固有値 — 行列  の 固有値 を三回",
    ],
    "hidden.pdf": [
        "誤差 Ｃ言語 TAYLOR 固有値 (attachment available=0, must never appear)",
    ],
    "orphan_item.pdf": [
        # The owning item is available=0 but the attachment row is not; today's
        # document scan only checks attachments.available, so this chunk stays
        # visible. The golden test pins that (unchanged) behaviour.
        "誤差 ｃ言語 固有値 (item available=0, attachment available=1)",
    ],
    "assign.pdf": [
        "課題: 誤差評価と固有値計算 (assignment kind)",
    ],
    "other_course.pdf": [
        "別科目の誤差と固有値。TAYLOR。",
    ],
}


def build_fixture(path: Path) -> dict[str, Any]:
    with database(path) as conn:
        repo = Repository(conn)
        c1 = repo.upsert_course(CourseRecord(source="beef", idnumber="C1", course_name="数値解析"))
        c2 = repo.upsert_course(CourseRecord(source="beef", idnumber="C2", course_name="解析学"))
        ids: dict[str, int] = {"c1": c1.id, "c2": c2.id}

        def add(
            source_id: str,
            *,
            category: str,
            title: str,
            body: str | None,
            course: int,
            files: Sequence[str],
        ) -> int:
            item = repo.upsert_item(
                ItemRecord(
                    source="beef",
                    source_id=source_id,
                    category=category,
                    title=title,
                    body=body,
                    course_name="数値解析" if course == c1.id else "解析学",
                    course_id=course,
                )
            )
            for file_name in files:
                attachment = repo.upsert_attachment(
                    AttachmentRecord(
                        stable_key=f"file/{file_name}",
                        file_name=file_name,
                        item_id=item.id,
                        course_id=course,
                    )
                )
                ids[file_name] = attachment.id
                texts = CHUNKS[file_name]
                repo.replace_document_text(
                    attachment.id,
                    [
                        {"page_number": index + 1, "text": text, "extraction_method": "native"}
                        for index, text in enumerate(texts)
                    ],
                    [
                        {
                            "chunk_index": index,
                            "page_start": index + 1,
                            "page_end": index + 1,
                            "text": text,
                        }
                        for index, text in enumerate(texts)
                    ],
                    status="ok",
                    method="native",
                )
            return item.id

        ids["item_c"] = add(
            "material/C1/1",
            category="material",
            title="Ｃ言語 資料",
            body="誤差と  TAYLOR\n展開",
            course=c1.id,
            files=["c_intro.pdf"],
        )
        ids["item_taylor"] = add(
            "material/C1/2",
            category="material",
            title="テイラー展開",
            body="誤差 誤差 誤差",
            course=c1.id,
            files=["taylor.pdf"],
        )
        ids["item_hidden_att"] = add(
            "material/C1/3",
            category="material",
            title="旧資料",
            body=None,
            course=c1.id,
            files=["hidden.pdf"],
        )
        ids["item_unavailable"] = add(
            "material/C1/4",
            category="material",
            title="非公開 誤差 固有値 Ｃ言語",
            body="非公開",
            course=c1.id,
            files=["orphan_item.pdf"],
        )
        ids["item_assign"] = add(
            "assignment/C1/5",
            category="assignment",
            title="課題1 誤差",
            body="固有値を求めよ",
            course=c1.id,
            files=["assign.pdf"],
        )
        ids["item_other"] = add(
            "material/C2/6",
            category="material",
            title="別科目 誤差",
            body="固有値",
            course=c2.id,
            files=["other_course.pdf"],
        )
        ids["item_nodoc"] = add(
            "announcement/C1/7",
            category="announcement",
            title="お知らせ",
            body="固有値の講義は休講",
            course=c1.id,
            files=[],
        )
        # Hide one attachment and one whole item through the regular writer API.
        repo.mark_attachments_missing(item_id=ids["item_hidden_att"], seen_stable_keys=[])
        repo.mark_items_missing(
            source="beef",
            seen_source_ids={
                "material/C1/1",
                "material/C1/2",
                "material/C1/3",
                "assignment/C1/5",
                "material/C2/6",
                "announcement/C1/7",
            },
        )
    return ids


def reference_search(
    conn: sqlite3.Connection,
    query: str,
    *,
    course_id: int | None = None,
    kinds: Sequence[str] | None = None,
    limit: int = 20,
) -> list[dict[str, Any]]:
    """Verbatim copy of the pre-instr() implementation (Python-only scan)."""
    needle = normalize_search(query)
    if not needle:
        return []
    tokens = [token for token in needle.split(" ") if token]
    where = ["i.available=1"]
    params: list[Any] = []
    if course_id is not None:
        where.append("i.course_id=?")
        params.append(course_id)
    if kinds:
        where.append(f"i.category IN ({','.join('?' for _ in kinds)})")
        params.extend(kinds)
    results: list[dict[str, Any]] = []
    for row in conn.execute(
        f"SELECT i.* FROM items i WHERE {' AND '.join(where)} ORDER BY i.updated_at DESC",
        params,
    ):
        haystack = normalize_search(
            " ".join(filter(None, (row["title"], row["body"], row["course_name"])))
        )
        if all(token in haystack for token in tokens):
            results.append(
                {
                    "result_type": "item",
                    "item": Repository._public_item(row, include_body=True),
                    "score": sum(haystack.count(token) for token in tokens),
                }
            )
    doc_where = ["a.available=1"]
    doc_params: list[Any] = []
    if course_id is not None:
        doc_where.append("a.course_id=?")
        doc_params.append(course_id)
    if kinds:
        doc_where.append(f"i.category IN ({','.join('?' for _ in kinds)})")
        doc_params.extend(kinds)
    rows = conn.execute(
        f"""SELECT dc.*, a.file_name, a.item_id, a.course_id, c.course_name, i.category
            FROM document_chunks dc
            JOIN attachments a ON a.id=dc.attachment_id
            JOIN items i ON i.id=a.item_id
            LEFT JOIN courses c ON c.id=a.course_id
            WHERE {' AND '.join(doc_where)}
            ORDER BY dc.id DESC""",
        doc_params,
    )
    for row in rows:
        search_text = row["search_text"]
        if all(token in search_text for token in tokens):
            results.append(
                {
                    "result_type": "document",
                    "attachment_id": row["attachment_id"],
                    "file_name": row["file_name"],
                    "course_id": row["course_id"],
                    "course_name": row["course_name"],
                    "category": row["category"],
                    "page_start": row["page_start"],
                    "page_end": row["page_end"],
                    "text": row["text"],
                    "score": sum(search_text.count(token) for token in tokens),
                }
            )
    results.sort(key=lambda item: item["score"], reverse=True)
    return results[:limit]


def _key(result: dict[str, Any]) -> tuple[Any, ...]:
    if result["result_type"] == "item":
        return ("item", result["item"]["id"], result["score"])
    return (
        "document",
        result["attachment_id"],
        result["page_start"],
        result["page_end"],
        result["text"],
        result["score"],
    )


REFERENCE_KEYS = {
    "result_type",
    "attachment_id",
    "file_name",
    "course_id",
    "course_name",
    "category",
    "page_start",
    "page_end",
    "text",
    "score",
}


@pytest.fixture
def fixture_db(tmp_path: Path) -> tuple[Path, dict[str, Any]]:
    path = tmp_path / "search.db"
    return path, build_fixture(path)


GOLDEN_QUERIES: list[tuple[str, dict[str, Any]]] = [
    ("誤差", {}),
    ("固有値", {}),
    ("Ｃ言語", {}),
    ("c言語", {}),
    ("C 言語", {}),
    ("TAYLOR", {}),
    ("taylor", {}),
    ("行列", {}),
    ("収束", {}),
    ("広義積分 収束判定", {}),
    ("誤差 固有値", {}),
    ("テイラー 誤差", {}),
    ("存在しない語", {}),
    ("誤差", {"limit": 2}),
    ("誤差", {"kinds": ["assignment"]}),
    ("固有値", {"kinds": ["material", "announcement"]}),
    ("   ", {}),
    ("", {}),
]


@pytest.mark.parametrize(("query", "options"), GOLDEN_QUERIES)
def test_search_sql_scan_matches_python_reference(
    fixture_db: tuple[Path, dict[str, Any]], query: str, options: dict[str, Any]
) -> None:
    path, ids = fixture_db
    for course_id in (None, ids["c1"], ids["c2"]):
        with database(path, readonly=True) as conn:
            expected = reference_search(conn, query, course_id=course_id, **options)
            actual = Repository(conn).search_course_content(
                query, course_id=course_id, **options
            )
        assert [_key(r) for r in actual] == [_key(r) for r in expected], (query, course_id)
        for got, want in zip(actual, expected, strict=True):
            if got["result_type"] == "document":
                assert {k: got[k] for k in REFERENCE_KEYS} == want
            else:
                assert got["item"] == want["item"]
                assert got["score"] == want["score"]


def test_search_golden_fixture_exercises_every_branch(
    fixture_db: tuple[Path, dict[str, Any]],
) -> None:
    """Guard against a hollow golden test: the fixture must actually yield hits."""
    path, ids = fixture_db
    with database(path, readonly=True) as conn:
        repo = Repository(conn)
        docs = [
            r for r in repo.search_course_content("誤差", limit=50) if r["result_type"] == "document"
        ]
        items = [r for r in repo.search_course_content("誤差", limit=50) if r["result_type"] == "item"]
        assert len(docs) >= 3 and len(items) >= 3
        assert any(r["file_name"] == "c_intro.pdf" for r in docs)
        assert repo.search_course_content("誤差 固有値", limit=50)
        assert repo.search_course_content("誤差", kinds=["assignment"], limit=50)
        assert repo.search_course_content("誤差", course_id=ids["c2"], limit=50)


def test_search_query_and_index_are_normalized_the_same_way(
    fixture_db: tuple[Path, dict[str, Any]],
) -> None:
    path, ids = fixture_db
    with database(path, readonly=True) as conn:
        repo = Repository(conn)
        variants = ["Ｃ言語", "c言語", "C言語", "ｃ言語"]
        hits = [
            {
                (r["attachment_id"], r["page_start"])
                for r in repo.search_course_content(q, limit=50)
                if r["result_type"] == "document"
            }
            for q in variants
        ]
        assert hits[0] == {
            (ids["c_intro.pdf"], 1),
            (ids["c_intro.pdf"], 2),
            (ids["orphan_item.pdf"], 1),
        }
        assert all(h == hits[0] for h in hits)
        # Multiple spaces / newlines inside the query collapse to one token boundary.
        assert repo.search_course_content("taylor") == repo.search_course_content("  TAYLOR \n")


def test_search_respects_course_kinds_and_availability(
    fixture_db: tuple[Path, dict[str, Any]],
) -> None:
    path, ids = fixture_db
    with database(path, readonly=True) as conn:
        repo = Repository(conn)
        everything = repo.search_course_content("誤差", limit=100)
        files = {r["file_name"] for r in everything if r["result_type"] == "document"}
        titles = {r["item"]["title"] for r in everything if r["result_type"] == "item"}
        assert "hidden.pdf" not in files
        assert "非公開 誤差 固有値 Ｃ言語" not in titles
        assert titles  # the available items with 誤差 are still found

        only_c2 = repo.search_course_content("誤差", course_id=ids["c2"], limit=100)
        assert {r["course_id"] for r in only_c2 if r["result_type"] == "document"} == {ids["c2"]}
        assert {r["item"]["course_id"] for r in only_c2 if r["result_type"] == "item"} == {
            ids["c2"]
        }

        assignments = repo.search_course_content("誤差", kinds=["assignment"], limit=100)
        assert assignments
        assert {r["category"] for r in assignments if r["result_type"] == "document"} == {
            "assignment"
        }
        assert {r["item"]["category"] for r in assignments if r["result_type"] == "item"} == {
            "assignment"
        }


def test_search_ranks_by_occurrence_count(fixture_db: tuple[Path, dict[str, Any]]) -> None:
    path, ids = fixture_db
    with database(path, readonly=True) as conn:
        docs = [
            r
            for r in Repository(conn).search_course_content(
                "固有値", course_id=ids["c1"], limit=50
            )
            if r["result_type"] == "document"
        ]
    assert docs[0]["attachment_id"] == ids["taylor.pdf"] and docs[0]["page_start"] == 3
    assert docs[0]["score"] == 4
    assert docs[0]["score"] > docs[1]["score"]
    assert [d["score"] for d in docs] == sorted((d["score"] for d in docs), reverse=True)
