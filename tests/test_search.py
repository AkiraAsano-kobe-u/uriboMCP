"""search_course_content: SQL instr() scan must equal the original Python scan."""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Sequence
from pathlib import Path
from typing import Any

import pytest

from uribo_mcp.db import database
from uribo_mcp.models import AttachmentRecord, CourseRecord, ItemRecord
from uribo_mcp.repository import (
    COURSE_SUMMARY_FIELDS,
    ITEM_SUMMARY_FIELDS,
    Repository,
    _first_match_offset,
    make_snippet,
    normalize_search,
)

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
    token_count = len(set(normalize_search(query).split()))
    for course_id in (None, ids["c1"], ids["c2"]):
        with database(path, readonly=True) as conn:
            expected = reference_search(conn, query, course_id=course_id, **options)
            detail = Repository(conn).search_course_content_detail(
                query, course_id=course_id, **options
            )
        actual = detail["results"]
        if not expected and token_count >= 2:
            # The only intended divergence from the old scan: a multi-token query
            # with no all-token row falls back to per-token matches.
            assert detail["match_mode"] == "any", (query, course_id)
            continue
        assert detail["match_mode"] == "all", (query, course_id)
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


# ----------------------------------------------------------- AND-first / ANY fallback


def test_search_prefers_all_token_matches(fixture_db: tuple[Path, dict[str, Any]]) -> None:
    path, ids = fixture_db
    with database(path, readonly=True) as conn:
        search = Repository(conn).search_course_content_detail("テイラー 誤差", limit=50)
    assert search["match_mode"] == "all"
    assert search["tokens"] == ["テイラー", "誤差"]
    assert search["tokens_unmatched"] == []
    assert search["results"]
    for result in search["results"]:
        assert result["tokens_matched"] == ["テイラー", "誤差"]
    # Rows that contain only one of the tokens (e.g. c_intro.pdf 誤差) are excluded.
    assert all(
        r["result_type"] != "document" or r["file_name"] != "c_intro.pdf"
        for r in search["results"]
    )


def test_search_falls_back_to_any_token_match(fixture_db: tuple[Path, dict[str, Any]]) -> None:
    path, ids = fixture_db
    with database(path, readonly=True) as conn:
        repo = Repository(conn)
        # No chunk/item contains both tokens: the old AND-only scan returned nothing.
        assert reference_search(conn, "広義積分 ポインタ", limit=50) == []
        search = repo.search_course_content_detail("広義積分 ポインタ", limit=50)
    assert search["match_mode"] == "any"
    assert search["tokens_unmatched"] == []
    docs = [r for r in search["results"] if r["result_type"] == "document"]
    assert {r["file_name"] for r in docs} == {"taylor.pdf", "c_intro.pdf"}
    for result in search["results"]:
        assert len(result["tokens_matched"]) == 1
    with database(path, readonly=True) as conn:
        search = Repository(conn).search_course_content_detail(
            "固有値 ポインタ 広義積分", course_id=ids["c1"], limit=50
        )
    assert search["match_mode"] == "any"
    matched_counts = [len(r["tokens_matched"]) for r in search["results"]]
    assert matched_counts == sorted(matched_counts, reverse=True)
    assert matched_counts[0] == 2  # taylor.pdf p2 has 収束判定 広義積分 / 固有値
    top = search["results"][0]
    assert top["result_type"] == "document" and top["page_start"] == 2
    # Within an equal tokens_matched count, higher score first.
    pairs = [(len(r["tokens_matched"]), r["score"]) for r in search["results"]]
    assert pairs == sorted(pairs, reverse=True)


def test_search_reports_unmatched_tokens(fixture_db: tuple[Path, dict[str, Any]]) -> None:
    path, ids = fixture_db
    with database(path, readonly=True) as conn:
        search = Repository(conn).search_course_content_detail("隤週 誤差 収束判定", limit=50)
    assert search["match_mode"] == "any"
    assert search["tokens_unmatched"] == ["隤週"]
    assert search["results"]
    assert all("隤週" not in r["tokens_matched"] for r in search["results"])


def test_single_token_query_never_uses_any_mode(fixture_db: tuple[Path, dict[str, Any]]) -> None:
    path, ids = fixture_db
    with database(path, readonly=True) as conn:
        repo = Repository(conn)
        hit = repo.search_course_content_detail("誤差", limit=50)
        miss = repo.search_course_content_detail("存在しない語", limit=50)
        # Repeated tokens are one token.
        dup = repo.search_course_content_detail("誤差 誤差", limit=50)
    assert hit["match_mode"] == "all" and hit["results"]
    assert miss["match_mode"] == "all" and miss["results"] == []
    assert miss["tokens_unmatched"] == ["存在しない語"]
    assert dup["match_mode"] == "all" and dup["tokens"] == ["誤差"]


def test_single_token_results_identical_to_legacy_api(
    fixture_db: tuple[Path, dict[str, Any]],
) -> None:
    path, ids = fixture_db
    with database(path, readonly=True) as conn:
        repo = Repository(conn)
        detail = repo.search_course_content_detail("固有値", limit=50)["results"]
        legacy = repo.search_course_content("固有値", limit=50)
        reference = reference_search(conn, "固有値", limit=50)
    assert [_key(r) for r in detail] == [_key(r) for r in legacy] == [_key(r) for r in reference]


def test_build_study_context_passes_match_mode(fixture_db: tuple[Path, dict[str, Any]]) -> None:
    path, ids = fixture_db
    with database(path, readonly=True) as conn:
        repo = Repository(conn)
        strict = repo.build_study_context("テイラー 誤差", course_id=ids["c1"])
        loose = repo.build_study_context("広義積分 ポインタ", course_id=ids["c1"])
    assert strict["match_mode"] == "all"
    assert strict["tokens"] == ["テイラー", "誤差"]
    assert strict["tokens_unmatched"] == []
    assert all(e["tokens_matched"] == ["テイラー", "誤差"] for e in strict["evidence"])
    assert loose["match_mode"] == "any"
    assert "match_mode=any" in loose["note"]
    assert loose["evidence"]
    assert all(len(e["tokens_matched"]) == 1 for e in loose["evidence"])


# ------------------------------------------------------------- snippet / include_text


LONG_TEXT = ("前置き" * 100) + "ここに誤差の説明がある。" + ("後置き" * 100)


def _long_fixture(path: Path) -> dict[str, Any]:
    with database(path) as conn:
        repo = Repository(conn)
        course = repo.upsert_course(CourseRecord(source="beef", idnumber="L1", course_name="長文"))
        item = repo.upsert_item(
            ItemRecord(
                source="beef",
                source_id="material/L1/1",
                category="material",
                title="長文資料",
                course_name="長文",
                course_id=course.id,
            )
        )
        attachment = repo.upsert_attachment(
            AttachmentRecord(
                stable_key="file/long.pdf", file_name="long.pdf", item_id=item.id, course_id=course.id
            )
        )
        texts = [LONG_TEXT, "短い誤差の章", "ＧＯＳＡ".replace("ＧＯＳＡ", "Ａ" * 200) + "誤差" + "Ｂ" * 200]
        repo.replace_document_text(
            attachment.id,
            [
                {"page_number": i + 1, "text": t, "extraction_method": "native"}
                for i, t in enumerate(texts)
            ],
            [
                {"chunk_index": i, "page_start": i + 1, "page_end": i + 1, "text": t}
                for i, t in enumerate(texts)
            ],
            status="ok",
            method="native",
        )
    return {"course": course.id, "attachment": attachment.id}


def test_search_snippet_centers_first_match(tmp_path: Path) -> None:
    path = tmp_path / "long.db"
    ids = _long_fixture(path)
    with database(path, readonly=True) as conn:
        results = Repository(conn).search_course_content(
            "誤差", course_id=ids["course"], snippet_chars=300
        )
    by_page = {r["page_start"]: r for r in results if r["result_type"] == "document"}
    long = by_page[1]
    assert long["text_chars"] == len(LONG_TEXT)
    assert long["text"] == LONG_TEXT
    assert "誤差" in long["snippet"]
    assert long["snippet"].startswith("…") and long["snippet"].endswith("…")
    assert len(long["snippet"]) <= 300 + 2
    # The match sits in the middle of the window, not at the start.
    position = long["snippet"].index("誤差")
    assert 100 < position < 200
    short = by_page[2]
    assert short["snippet"] == "短い誤差の章" and short["text_chars"] == len("短い誤差の章")
    assert "…" not in short["snippet"]
    # Full-width text: the snippet still contains the match (NFKC offset mapping).
    assert "誤差" in by_page[3]["snippet"]


@pytest.mark.parametrize("original", ["ｶﾞ", "カ\u3099", "e\u0301", "가", "Ｃ言語", "Straße"])
def test_snippet_matches_whole_string_nfkc(original: str) -> None:
    text = "前" * 400 + original + "後" * 400
    snippet = make_snippet(text, [normalize_search(original)], snippet_chars=100)
    assert original in snippet
    assert 45 <= snippet.index(original) <= 55


def test_search_snippet_window_at_text_edges(tmp_path: Path) -> None:
    path = tmp_path / "edges.db"
    with database(path) as conn:
        repo = Repository(conn)
        course = repo.upsert_course(CourseRecord(source="beef", idnumber="E1", course_name="端"))
        item = repo.upsert_item(
            ItemRecord(
                source="beef", source_id="m/E1/1", category="material", title="端", course_id=course.id
            )
        )
        att = repo.upsert_attachment(
            AttachmentRecord(stable_key="f/e", file_name="e.pdf", item_id=item.id, course_id=course.id)
        )
        head = "誤差" + "x" * 500
        tail = "y" * 500 + "誤差"
        repo.replace_document_text(
            att.id,
            [{"page_number": 1, "text": head}, {"page_number": 2, "text": tail}],
            [
                {"chunk_index": 0, "page_start": 1, "page_end": 1, "text": head},
                {"chunk_index": 1, "page_start": 2, "page_end": 2, "text": tail},
            ],
            status="ok",
            method="native",
        )
    with database(path, readonly=True) as conn:
        results = Repository(conn).search_course_content("誤差", course_id=course.id, snippet_chars=100)
    by_page = {r["page_start"]: r["snippet"] for r in results if r["result_type"] == "document"}
    assert by_page[1].startswith("誤差") and by_page[1].endswith("…") and len(by_page[1]) == 101
    assert by_page[2].startswith("…") and by_page[2].endswith("誤差") and len(by_page[2]) == 101


def test_search_include_text_false_omits_text_but_keeps_snippet(tmp_path: Path) -> None:
    path = tmp_path / "long.db"
    ids = _long_fixture(path)
    with database(path, readonly=True) as conn:
        repo = Repository(conn)
        slim = repo.search_course_content("誤差", course_id=ids["course"], include_text=False)
        full = repo.search_course_content("誤差", course_id=ids["course"], include_text=True)
    assert slim and len(slim) == len(full)
    for result in slim:
        assert result["result_type"] == "document"
        assert "text" not in result
        assert "誤差" in result["snippet"]
        assert result["text_chars"] > 0
    for result in full:
        assert "text" in result and "snippet" in result
    # Dropping "text" is the only difference.
    assert slim == [{k: v for k, v in r.items() if k != "text"} for r in full]
    slim_bytes = len(json.dumps(slim, ensure_ascii=False))
    full_bytes = len(json.dumps(full, ensure_ascii=False))
    assert slim_bytes < full_bytes - len(LONG_TEXT)


def test_build_study_context_uses_full_text_not_snippet(tmp_path: Path) -> None:
    path = tmp_path / "long.db"
    ids = _long_fixture(path)
    with database(path, readonly=True) as conn:
        context = Repository(conn).build_study_context("誤差", course_id=ids["course"])
    texts = {e["text"] for e in context["evidence"]}
    assert LONG_TEXT in texts
    assert all("snippet" not in e for e in context["evidence"])


# --------------------------------------------------------------- fields="summary"


@pytest.mark.parametrize("suffix", ["", "z"])
def test_snippet_canonical_reordering_maps_original_origin(suffix):
    text = "前" * 400 + "a\u0301" + "\u0323" * 200 + suffix + "後" * 400
    token = "\u0301" + suffix
    assert token in normalize_search(text)
    assert _first_match_offset(text, [token]) == 401
    snippet = make_snippet(text, [token], snippet_chars=300)
    assert "\u0301" in snippet
    assert snippet.strip("…") in text



def test_list_course_content_summary_fields(fixture_db: tuple[Path, dict[str, Any]]) -> None:
    path, ids = fixture_db
    dropped = {
        "metadata",
        "content_hash",
        "source_id",
        "first_seen_at",
        "last_seen_at",
        "created_at",
        "remote_updated_at",
        "cluster_id",
        "is_read",
        "importance",
        "body",
    }
    with database(path, readonly=True) as conn:
        repo = Repository(conn)
        summary = repo.list_course_content(ids["c1"], fields="summary")
        full = repo.list_course_content(ids["c1"], fields="full")
        default = repo.list_course_content(ids["c1"])
        with pytest.raises(ValueError):
            repo.list_course_content(ids["c1"], fields="bogus")
    assert summary and len(summary) == len(full)
    assert default == full  # Existing callers retain the complete payload.
    for item in summary:
        assert set(item) == set(ITEM_SUMMARY_FIELDS)
        assert not (set(item) & dropped)
        assert isinstance(item["is_done"], bool) and isinstance(item["available"], bool)
    for item in full:
        assert dropped - {"body"} <= set(item)
        assert "body" not in item
        assert isinstance(item["metadata"], dict)
    assert [i["id"] for i in summary] == [i["id"] for i in full]


def test_list_courses_summary_fields(fixture_db: tuple[Path, dict[str, Any]]) -> None:
    path, ids = fixture_db
    with database(path, readonly=True) as conn:
        repo = Repository(conn)
        summary = repo.list_courses(fields="summary")
        full = repo.list_courses(fields="full")
        assert repo.list_courses() == full
    assert len(summary) == len(full) == 2
    for course in summary:
        assert set(course) == set(COURSE_SUMMARY_FIELDS)
        assert isinstance(course["available"], bool)
    for course in full:
        assert {"instructors", "metadata", "summary", "source_url", "content_hash"} <= set(course)
    assert [c["id"] for c in summary] == [c["id"] for c in full]


@pytest.mark.parametrize("mode", ["all", "any"])
def test_search_1001_unique_tokens_preserves_all_tokens(fixture_db, mode):
    path, ids = fixture_db
    tokens = [f"q{index:04d}" for index in range(1001)]
    text = " ".join(tokens) if mode == "all" else tokens[-1]
    with database(path) as conn:
        conn.execute("UPDATE document_chunks SET text=?, search_text=? WHERE attachment_id=?",
                     (text, text, ids["taylor.pdf"]))
        # A match outside the requested course must not influence tokens_unmatched.
        conn.execute("UPDATE document_chunks SET text=?, search_text=? WHERE attachment_id=?",
                     (tokens[0], tokens[0], ids["other_course.pdf"]))
        conn.commit()
    with database(path, readonly=True) as conn:
        result = Repository(conn).search_course_content_detail(
            " ".join(tokens), course_id=ids["c1"], kinds=["material"]
        )
    assert result["tokens"] == tokens
    assert result["match_mode"] == mode
    assert result["tokens_unmatched"] == ([] if mode == "all" else tokens[:-1])
    assert result["results"]
    expected = tokens if mode == "all" else [tokens[-1]]
    assert all(hit["tokens_matched"] == expected for hit in result["results"])


def test_document_candidate_transfer_guard(fixture_db):
    """Guard SQL selectivity by transferred rows, never by elapsed time."""
    path, ids = fixture_db
    with database(path) as conn:
        attachment = ids["taylor.pdf"]
        for index in range(200):
            conn.execute(
                "INSERT INTO document_chunks(attachment_id, chunk_index, page_start, page_end, text, search_text) "
                "VALUES (?, ?, 1, 1, 'unrelated text', 'unrelated text')",
                (attachment, 100 + index),
            )
        conn.commit()
    transferred = []
    with database(path, readonly=True) as conn:
        def track(cursor, values):
            row = sqlite3.Row(cursor, values)
            if "search_text" in row.keys():
                transferred.append(row["id"])
            return row

        expected = conn.execute(
            "SELECT count(*) FROM document_chunks dc "
            "JOIN attachments a ON a.id=dc.attachment_id "
            "JOIN items i ON i.id=a.item_id "
            "WHERE a.available=1 AND instr(dc.search_text, ?)>0", ("誤差",)
        ).fetchone()[0]
        conn.row_factory = track
        results = Repository(conn).search_course_content("誤差", include_text=False)
    assert results and expected > 0
    assert len(transferred) == expected
    assert len(transferred) < 20
