"""Scan-phase regression tests (pure ones need no exiftool; walk tests are marked)."""

from pathlib import Path

import pytest
from conftest import _gradient, pf, q
from PIL import Image

from photoflow.db import open_db
from photoflow.hashing import HAVE_IMAGEHASH
from photoflow.scan import cmd_scan, phash_pending_images


def test_phash_pass_survives_more_files_than_sqlite_variable_cap(tmp_path):
    # Regression: 48k-file import crashed with "too many SQL variables"
    # because the phash SELECT bound one variable per new path
    # (SQLITE_MAX_VARIABLE_NUMBER is 32766).
    conn = open_db(tmp_path)
    conn.executemany(
        "INSERT INTO files(source_path, kind, status) VALUES (?,?,?)",
        ((str(tmp_path / f"missing_{i}.jpg"), "image", "scanned") for i in range(33000)),
    )
    conn.commit()
    phash_pending_images(conn)  # must not raise sqlite3.OperationalError


@pytest.mark.skipif(not HAVE_IMAGEHASH, reason="ImageHash not installed")
def test_phash_pass_backfills_and_respects_kind_and_status(tmp_path):
    img = tmp_path / "old.jpg"
    Image.new("RGB", (64, 48), (10, 200, 30)).save(img, "JPEG")
    conn = open_db(tmp_path)
    rows = [
        (str(img), "image", "scanned"),  # pending from an interrupted run -> backfilled
        (str(tmp_path / "clip.mp4"), "video", "scanned"),  # not an image -> untouched
        (str(img) + ".copied", "image", "copied"),  # past scanned lifecycle -> untouched
    ]
    conn.executemany("INSERT INTO files(source_path, kind, status) VALUES (?,?,?)", rows)
    conn.commit()

    phash_pending_images(conn)

    got = {
        r["source_path"]: r["phash"]
        for r in conn.execute("SELECT source_path, phash FROM files").fetchall()
    }
    assert got[str(img)], "scanned image missing phash was not backfilled"
    assert got[str(tmp_path / "clip.mp4")] is None
    assert got[str(img) + ".copied"] is None


@pytest.mark.exiftool
def test_scan_prunes_excluded_directories(tmp_path: Path):
    src = tmp_path / "src"
    for rel in ("CaptureOne/Cache", "trash", "Sub/@eaDir", "Sub"):
        (src / rel).mkdir(parents=True, exist_ok=True)
    _gradient(320, 240, seed=41).save(src / "keep_me.jpg", "JPEG", quality=92)
    _gradient(320, 240, seed=42).save(src / "Sub" / "keep_me_too.jpg", "JPEG", quality=92)
    _gradient(320, 240, seed=43).save(src / "CaptureOne" / "Cache" / "proxy.jpg", "JPEG")
    _gradient(320, 240, seed=44).save(src / "trash" / "deleted.jpg", "JPEG")  # case-insensitive
    _gradient(320, 240, seed=45).save(src / "Sub" / "@eaDir" / "thumb.jpg", "JPEG")

    work = tmp_path / "work"
    out = pf(work, "scan", str(src)).stdout
    names = {Path(r["source_path"]).name for r in q(work, "SELECT source_path FROM files")}
    assert names == {"keep_me.jpg", "keep_me_too.jpg"}
    assert "pruned 3 dirs" in out


@pytest.mark.exiftool
def test_scan_skips_files_below_min_size(tmp_path: Path):
    src = tmp_path / "src"
    src.mkdir()
    _gradient(640, 480, seed=46).save(src / "real.jpg", "JPEG", quality=92)  # ~25 KB
    Image.new("RGB", (8, 8), (3, 4, 5)).save(src / "thumb.jpg", "JPEG")  # ~630 B
    work = tmp_path / "work"
    work.mkdir(parents=True)
    (work / "photoflow.toml").write_text("min_size_bytes = 5000\n", encoding="utf-8")

    out = pf(work, "scan", str(src)).stdout
    names = {Path(r["source_path"]).name for r in q(work, "SELECT source_path FROM files")}
    assert names == {"real.jpg"}
    assert "1 below min size" in out


def test_scan_counts_unreadable_entries_instead_of_crashing(tmp_path, monkeypatch, capsys):
    """os.walk's onerror callback must be counted, not raised (C1: one bad entry aborted a scan)."""
    import photoflow.scan as scan_mod

    def fake_walk(top, onerror=None, **kw):
        onerror(PermissionError(13, "Access is denied", str(top)))
        return iter(())

    monkeypatch.setattr(scan_mod, "exiftool_available", lambda: True)
    monkeypatch.setattr(scan_mod.os, "walk", fake_walk)
    src = tmp_path / "src"
    src.mkdir()
    conn = open_db(tmp_path / "work")
    args = type("A", (), {"sources": [str(src)]})()
    from photoflow.config import Config

    cmd_scan(conn, tmp_path / "work", 1, open(tmp_path / "log.jsonl", "w"), args, Config())
    assert "1 unreadable" in capsys.readouterr().out


def test_next_hints_use_the_installed_command_name():
    """C10: photoflow.py was deleted; the hints must name the console script."""
    pkg = Path(__import__("photoflow").__file__).parent
    for name in ("scan.py", "planner.py"):
        assert "python photoflow.py" not in (pkg / name).read_text(encoding="utf-8")
