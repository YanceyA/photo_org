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


@pytest.mark.exiftool
def test_rescan_rehashes_rows_left_without_a_content_hash(tmp_path: Path):
    """H8: an interrupted scan leaves content_hash NULL; size+mtime alone would skip it forever."""
    src = tmp_path / "src"
    src.mkdir()
    _gradient(640, 480, seed=47).save(src / "photo.jpg", "JPEG", quality=92)
    work = tmp_path / "work"
    pf(work, "scan", str(src))
    conn = open_db(work)
    conn.execute("UPDATE files SET content_hash=NULL, meta_read=0, exif_date=NULL")
    conn.commit()
    conn.close()

    pf(work, "scan", str(src))  # same tree, unchanged size+mtime

    row = q(work, "SELECT content_hash, meta_read FROM files")[0]
    assert row["content_hash"], "NULL-hash row was skipped by the size+mtime rule"
    assert row["meta_read"] == 1


@pytest.mark.exiftool
def test_read_metadata_pending_is_manifest_driven_and_marks_rows_done(tmp_path: Path):
    from conftest import _set_exif

    from photoflow.config import Config
    from photoflow.scan import read_metadata_pending

    img = tmp_path / "shot.jpg"
    _gradient(320, 240, seed=48).save(img, "JPEG", quality=92)
    _set_exif(img, DateTimeOriginal="2015:07:14 10:30:00", Model="Canon EOS 70D")

    conn = open_db(tmp_path / "work")
    conn.execute(
        "INSERT INTO files(source_path, kind, status, content_hash, meta_read) VALUES (?,?,?,?,0)",
        (str(img), "image", "scanned", "deadbeef" * 8),
    )
    # not a candidate: no content_hash yet (interrupted hashing pass)
    conn.execute(
        "INSERT INTO files(source_path, kind, status, meta_read) VALUES (?,?,?,0)",
        (str(tmp_path / "nohash.jpg"), "image", "scanned"),
    )
    conn.commit()

    assert read_metadata_pending(conn, Config()) == 1
    row = conn.execute("SELECT * FROM files WHERE source_path=?", (str(img),)).fetchone()
    assert row["exif_date"] == "2015:07:14 10:30:00"
    assert row["camera"] == "Canon EOS 70D"
    assert row["meta_read"] == 1

    # done rows are never re-read
    conn.execute("UPDATE files SET exif_date='TOUCHED' WHERE source_path=?", (str(img),))
    conn.commit()
    assert read_metadata_pending(conn, Config()) == 0
    assert (
        conn.execute("SELECT exif_date FROM files WHERE source_path=?", (str(img),)).fetchone()[
            "exif_date"
        ]
        == "TOUCHED"
    )
    conn.close()


def test_read_metadata_pending_does_not_clobber_when_exiftool_returns_nothing(
    tmp_path: Path, monkeypatch
):
    """No record for a path means missing/offline - never overwrite what the manifest knows."""
    import photoflow.scan as scan_mod
    from photoflow.config import Config

    conn = open_db(tmp_path / "work")
    sp = str(tmp_path / "offline.jpg")
    conn.execute(
        "INSERT INTO files(source_path, kind, status, content_hash, meta_read,"
        " exif_date, camera, width) VALUES (?,?,?,?,0,?,?,?)",
        (sp, "image", "scanned", "beefcafe" * 8, "2011:01:02 03:04:05", "Nikon D90", 4000),
    )
    conn.commit()
    known = ("2011:01:02 03:04:05", "Nikon D90", 4000)

    def cols():
        r = conn.execute("SELECT * FROM files WHERE source_path=?", (sp,)).fetchone()
        return (r["exif_date"], r["camera"], r["width"]), r["meta_read"]

    # whole batch came back empty (exiftool_json returns {} on JSONDecodeError/OSError)
    monkeypatch.setattr(scan_mod, "exiftool_json", lambda paths, batch, **kw: {})
    scan_mod.read_metadata_pending(conn, Config())
    assert cols() == (known, 0), "a transient exiftool failure must stay retryable"

    # batch worked but this path got no record: individually unreadable -> done, not clobbered
    monkeypatch.setattr(
        scan_mod,
        "exiftool_json",
        lambda paths, batch, **kw: {"C:/somewhere/else.jpg": {"Model": "X"}},
    )
    scan_mod.read_metadata_pending(conn, Config())
    assert cols() == (known, 1)
    conn.close()


def test_read_metadata_pending_picks_up_planned_and_copied_rows(tmp_path: Path, monkeypatch):
    """An interrupted pass leaves rows that plan then advances; they must still be re-read."""
    import photoflow.scan as scan_mod
    from photoflow.config import Config

    conn = open_db(tmp_path / "work")
    for status in ("planned", "copied", "error"):
        conn.execute(
            "INSERT INTO files(source_path, kind, status, content_hash, meta_read)"
            " VALUES (?,?,?,?,0)",
            (str(tmp_path / f"{status}.jpg"), "image", status, "cafe1234" * 8),
        )
    conn.commit()

    monkeypatch.setattr(
        scan_mod, "exiftool_json", lambda paths, batch, **kw: {p: {"Model": "Echo"} for p in paths}
    )
    scan_mod.read_metadata_pending(conn, Config())

    state = {
        r["status"]: (r["meta_read"], r["camera"])
        for r in conn.execute("SELECT status, meta_read, camera FROM files")
    }
    assert state["planned"] == (1, "Echo")
    assert state["copied"] == (1, "Echo")
    assert state["error"] == (0, None), "error rows are durable - leave them alone"
    conn.close()


def test_video_metadata_prefers_creation_date_over_create_date(tmp_path, monkeypatch):
    """CreationDate (QuickTime Keys, tz-aware, what iPhones write) wins over CreateDate."""
    from photoflow.config import Config
    from photoflow.scan import read_metadata_pending

    conn = open_db(tmp_path / "work")
    conn.execute(
        "INSERT INTO files(source_path, kind, status, content_hash, meta_read) VALUES (?,?,?,?,0)",
        ("C:/clips/IMG_0735.MOV", "video", "scanned", "cafe" * 16),
    )
    conn.commit()

    import photoflow.scan as scan_mod

    monkeypatch.setattr(
        scan_mod,
        "exiftool_json",
        lambda paths, batch, **kw: {
            p: {
                "CreationDate": "2010:09:04 04:03:31+12:00",
                "CreateDate": "2010:09:03 16:03:31",
                "MediaCreateDate": "2010:09:03 16:03:31",
            }
            for p in paths
        },
    )
    read_metadata_pending(conn, Config())
    assert conn.execute("SELECT exif_date FROM files").fetchone()[0] == "2010:09:04 04:03:31+12:00"
    conn.close()


def test_failed_video_subcall_does_not_mark_video_rows_done(tmp_path: Path, monkeypatch):
    """The 'batch came back non-empty' heuristic is per exiftool invocation, not per row batch.

    An image sub-call that succeeds must not make a failed video sub-call look successful.
    """
    import photoflow.scan as scan_mod
    from photoflow.config import Config

    conn = open_db(tmp_path / "work")
    for name, kind in (("photo.jpg", "image"), ("clip.mov", "video")):
        conn.execute(
            "INSERT INTO files(source_path, kind, status, content_hash, meta_read)"
            " VALUES (?,?,?,?,0)",
            (str(tmp_path / name), kind, "scanned", "d0d0" * 16 + name[:1]),
        )
    conn.commit()

    def fake(paths, batch, *, fast=True):
        # images (fast=True) come back with a record for a path nobody asked about; the
        # video call (fast=False) fails outright and returns nothing.
        return {"C:/somewhere/else.jpg": {"Model": "X"}} if fast else {}

    monkeypatch.setattr(scan_mod, "exiftool_json", fake)
    scan_mod.read_metadata_pending(conn, Config())

    state = {r["kind"]: r["meta_read"] for r in conn.execute("SELECT kind, meta_read FROM files")}
    assert state["image"] == 1, "the image sub-call worked: that path is individually unreadable"
    assert state["video"] == 0, "the video sub-call failed: retry it next run"
    conn.close()


@pytest.mark.parametrize(
    "rec, expect",
    [
        # a garbage CreationDate must not shadow a good DateTimeOriginal (wild EXIF really
        # does contain "0000:00:00 00:00:00" - CLAUDE.md calls it out)
        (
            {
                "CreationDate": "0000:00:00 00:00:00",
                "DateTimeOriginal": "2015:07:14 10:30:00",
                "CreateDate": "2015:07:14 10:30:01",
            },
            "2015:07:14 10:30:00",
        ),
        # fallback chain with CreationDate absent
        (
            {
                "DateTimeOriginal": "2015:07:14 10:30:00",
                "CreateDate": "2016:01:01 00:00:00",
                "MediaCreateDate": "2017:01:01 00:00:00",
            },
            "2015:07:14 10:30:00",
        ),
        (
            {"CreateDate": "2016:01:01 00:00:00", "MediaCreateDate": "2017:01:01 00:00:00"},
            "2016:01:01 00:00:00",
        ),
        ({"MediaCreateDate": "2017:01:01 00:00:00"}, "2017:01:01 00:00:00"),
        # nothing parses -> keep the first value the file claimed, so plan still sees it
        ({"CreationDate": "0000:00:00 00:00:00"}, "0000:00:00 00:00:00"),
        ({"Model": "Canon EOS 70D"}, None),
    ],
)
def test_read_metadata_pending_stores_the_first_parseable_date(
    tmp_path: Path, monkeypatch, rec, expect
):
    import photoflow.scan as scan_mod
    from photoflow.config import Config

    conn = open_db(tmp_path / "work")
    conn.execute(
        "INSERT INTO files(source_path, kind, status, content_hash, meta_read) VALUES (?,?,?,?,0)",
        (str(tmp_path / "x.jpg"), "image", "scanned", "1234abcd" * 8),
    )
    conn.commit()

    monkeypatch.setattr(
        scan_mod, "exiftool_json", lambda paths, batch, **kw: {p: rec for p in paths}
    )
    scan_mod.read_metadata_pending(conn, Config())

    assert conn.execute("SELECT exif_date FROM files").fetchone()[0] == expect
    conn.close()
