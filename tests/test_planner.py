"""Unit tests for cmd_plan: keeper preference, idempotence, live-pair dates."""

import os
import shutil
from collections import defaultdict
from pathlib import Path

import pytest
from conftest import _gradient, _set_exif, pf, q

pytestmark = pytest.mark.exiftool

MTIME_2018 = 1514764800  # 2018-01-01 UTC
MTIME_2020 = 1577836800  # 2020-01-01 UTC


def test_keeper_prefers_earliest_mtime(tmp_path: Path):
    work = tmp_path / "work"
    src = tmp_path / "src"
    src.mkdir()
    later = src / "later.jpg"
    _gradient(640, 480, seed=21).save(later, "JPEG", quality=92)
    earlier = src / "earlier.jpg"
    shutil.copy2(later, earlier)
    os.utime(later, (MTIME_2020, MTIME_2020))
    os.utime(earlier, (MTIME_2018, MTIME_2018))

    pf(work, "scan", str(src))
    pf(work, "plan")

    rows = {Path(r["source_path"]).name: r for r in q(work, "SELECT * FROM files")}
    assert rows["earlier.jpg"]["role"] == "keep"
    assert rows["later.jpg"]["role"] == "exact_dupe"
    assert rows["later.jpg"]["dupe_of"] == rows["earlier.jpg"]["id"]


def test_keeper_prefers_already_copied(tmp_path: Path):
    work = tmp_path / "work"
    lib = tmp_path / "lib"
    src1 = tmp_path / "src1"
    src1.mkdir()
    orig = src1 / "photo.jpg"
    _gradient(640, 480, seed=22).save(orig, "JPEG", quality=92)
    os.utime(orig, (MTIME_2020, MTIME_2020))

    pf(work, "scan", str(src1))
    pf(work, "plan")
    pf(work, "apply", "--out", str(lib))
    assert q(work, "SELECT status FROM files")[0]["status"] == "copied"

    # byte-copy with an EARLIER mtime: mtime alone would crown it keeper,
    # proving copied-status takes precedence over mtime in the sort key
    src2 = tmp_path / "src2"
    src2.mkdir()
    twin = src2 / "photo_twin.jpg"
    shutil.copy2(orig, twin)
    os.utime(twin, (MTIME_2018, MTIME_2018))

    pf(work, "scan", str(src2))
    pf(work, "plan")

    rows = {Path(r["source_path"]).name: r for r in q(work, "SELECT * FROM files")}
    assert rows["photo.jpg"]["role"] == "keep"
    assert rows["photo_twin.jpg"]["role"] == "exact_dupe"
    assert rows["photo_twin.jpg"]["dupe_of"] == rows["photo.jpg"]["id"]

    before = {p for p in lib.rglob("*") if p.is_file()}
    pf(work, "apply", "--out", str(lib))
    after = {p for p in lib.rglob("*") if p.is_file()}
    assert after == before  # library gained zero files


def test_plan_is_idempotent(photo_fixture: Path, tmp_path: Path):
    work = tmp_path / "work"
    pf(work, "scan", str(photo_fixture))

    def snapshot():
        rows = q(
            work,
            "SELECT source_path, role, dupe_of, status, group_id FROM files ORDER BY source_path",
        )
        flat = [(r["source_path"], r["role"], r["dupe_of"], r["status"]) for r in rows]
        # group_id numbering may differ between runs; compare group membership
        groups = defaultdict(set)
        for r in rows:
            if r["group_id"] is not None:
                groups[r["group_id"]].add(r["source_path"])
        return flat, {frozenset(v) for v in groups.values()}

    pf(work, "plan")
    first = snapshot()
    pf(work, "plan")
    second = snapshot()
    assert first == second


def test_live_pair_video_inherits_date(tmp_path: Path):
    work = tmp_path / "work"
    src = tmp_path / "src"
    src.mkdir()
    jpg = src / "clip.jpg"
    _gradient(640, 480, seed=23).save(jpg, "JPEG", quality=92)
    _set_exif(jpg, DateTimeOriginal="2017:05:01 09:15:30", Model="Apple iPhone 7")
    # kind classification is by extension; content (and missing exif) don't matter
    (src / "clip.mp4").write_bytes(b"fakevideo" * 64)

    pf(work, "scan", str(src))
    pf(work, "plan")

    rows = {Path(r["source_path"]).name: r for r in q(work, "SELECT * FROM files")}
    mp4 = rows["clip.mp4"]
    assert mp4["role"] == "live_pair"
    assert mp4["date_source"] == "exif"
    assert mp4["date_taken"] == rows["clip.jpg"]["date_taken"] == "2017-05-01T09:15:30"
