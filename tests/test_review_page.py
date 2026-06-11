"""Unit tests for review_page pure helpers (no exiftool, no Pillow needed)."""

import csv

from photoflow.review_page import (
    CSV_COLUMNS,
    build_payload,
    decision_rows,
    write_decisions_csv,
)


def g(**kw):
    base = dict(
        id=1,
        source_path="C:/src/a.jpg",
        width=4000,
        height=3000,
        size=2_000_000,
        ext="jpg",
        kind="image",
        camera="X100",
        date_taken="2024:01:01 10:00:00",
    )
    base.update(kw)
    return base


GROUPS = {
    7: [
        g(id=1),
        g(id=2, source_path="C:/src/b, with comma.jpg", width=1600, height=1200, size=300_000),
    ]
}


def test_decision_rows_suggests_most_pixels():
    rows = decision_rows(GROUPS, {})
    assert [r["suggestion"] for r in rows] == ["keep", "keep?"]
    assert rows[0]["resolution"] == "4000x3000"
    assert rows[1]["size_kb"] == 293
    assert all(r["decision"] == "" for r in rows)


def test_decision_rows_carry_forward_by_file_id():
    prior = {"2": {"decision": "skip", "merge_from_file_id": ""}}
    rows = decision_rows(GROUPS, prior)
    assert rows[1]["decision"] == "skip"
    assert rows[0]["decision"] == ""


def test_csv_round_trip(tmp_path):
    rows = decision_rows(GROUPS, {"1": {"decision": "keep", "merge_from_file_id": "2"}})
    p = tmp_path / "decisions.csv"
    write_decisions_csv(p, rows)
    with open(p, newline="", encoding="utf-8") as f:
        back = list(csv.DictReader(f))
    assert list(back[0].keys()) == CSV_COLUMNS
    assert [{c: str(r[c]) for c in CSV_COLUMNS} for r in rows] == back


def test_payload_marks_best_suggested_and_decisions():
    rows = decision_rows(GROUPS, {"1": {"decision": "keep", "merge_from_file_id": "2"}})
    p = build_payload(GROUPS, rows, "C:/work", thumbs_ok={1})
    assert p["workdir"] == "C:/work"
    (grp,) = p["groups"]
    assert grp["gid"] == 7
    f1, f2 = grp["files"]
    assert f1["suggested"] and f1["bestRes"] and f1["bestSize"]
    assert not (f2["suggested"] or f2["bestRes"] or f2["bestSize"])
    assert f1["decision"] == "keep" and f1["merge"] == "2"
    assert f2["decision"] == "" and f2["merge"] == ""
    assert f1["thumb"] == "thumbs/1.jpg" and f2["thumb"] is None
    assert f1["uri"].startswith("file:///")
    assert f1["w"] == 4000 and f1["size"] == 2_000_000
    assert f1["ext"] == "jpg" and f1["kind"] == "image"
    assert f1["camera"] == "X100" and f1["date"] == "2024:01:01 10:00:00"
    # the exact CSV cell values ride along so the JS can re-serialize byte-compatibly
    assert f1["csv"] == {"resolution": "4000x3000", "size_kb": 1953, "suggestion": "keep"}


def test_payload_relative_path_gets_no_uri():
    groups = {1: [g(id=5, source_path="not/absolute.jpg")]}
    rows = decision_rows(groups, {})
    p = build_payload(groups, rows, "w", thumbs_ok=set())
    assert p["groups"][0]["files"][0]["uri"] is None
