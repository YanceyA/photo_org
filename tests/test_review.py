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
