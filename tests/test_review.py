"""Unit tests for cmd_review: decision carry-forward across regenerations."""

import csv
from pathlib import Path

import pytest
from conftest import pf

pytestmark = pytest.mark.exiftool


def _review_twice(photo_fixture: Path, work: Path) -> list[dict]:
    """scan+plan+review, mark sunset_small skip, review again, return new rows."""
    pf(work, "scan", str(photo_fixture))
    pf(work, "plan")
    pf(work, "review")
    dec = work / "decisions.csv"
    with open(dec, newline="", encoding="utf-8") as f:
        rows = list(csv.DictReader(f))
    for r in rows:
        if "sunset_small" in r["source_path"]:
            r["decision"] = "skip"
    with open(dec, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=rows[0].keys())
        w.writeheader()
        w.writerows(rows)
    pf(work, "review")
    with open(dec, newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))


def test_carry_forward(photo_fixture: Path, tmp_path: Path):
    rows = _review_twice(photo_fixture, tmp_path / "work")
    small = next(r for r in rows if "sunset_small" in r["source_path"])
    assert small["decision"] == "skip"  # carried forward by file_id
    assert all(r["decision"] == "" for r in rows if r is not small)


def test_blank_decisions_stay_blank(photo_fixture: Path, tmp_path: Path):
    rows = _review_twice(photo_fixture, tmp_path / "work")
    big = next(r for r in rows if "sunset_big" in r["source_path"])
    assert big["decision"] == ""
    assert big["merge_from_file_id"] == ""


def test_review_regeneration_relocks_copied_members(tmp_path: Path):
    """Invariant #4 says decisions carry forward - but a copied member's decision is not
    the user's to change, so a stale 'skip' for it is overridden back to 'keep'."""
    from photoflow.db import new_run, open_db
    from photoflow.review import cmd_review

    work = tmp_path / "work"
    conn = open_db(work)
    conn.executemany(
        "INSERT INTO files(source_path, kind, ext, role, status, group_id, width, height, size)"
        " VALUES (?,?,?,?,?,?,?,?,?)",
        [
            (str(tmp_path / "a.jpg"), "image", ".jpg", "review", "copied", 7, 4000, 3000, 10),
            (str(tmp_path / "b.jpg"), "image", ".jpg", "review", "review", 7, 1600, 1200, 10),
        ],
    )
    conn.commit()
    ids = [r["id"] for r in conn.execute("SELECT id FROM files ORDER BY id")]
    dec = work / "decisions.csv"
    dec.write_text(
        "group_id,file_id,source_path,resolution,size_kb,suggestion,decision,"
        "merge_from_file_id\n"
        f"7,{ids[0]},x,4000x3000,0,keep,skip,\n",
        encoding="utf-8",
    )
    run_id = new_run(conn, "review", {})
    (work / "logs").mkdir(exist_ok=True)
    with open(work / "logs" / "t.jsonl", "a", encoding="utf-8") as fh:
        cmd_review(conn, work, run_id, fh, None, None)

    with open(dec, newline="", encoding="utf-8") as f:
        rows = {r["file_id"]: r["decision"] for r in csv.DictReader(f)}
    assert rows[str(ids[0])] == "keep"  # locked: the CSV skip was overridden
    assert rows[str(ids[1])] == ""
