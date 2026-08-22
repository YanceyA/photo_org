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


@pytest.mark.exiftool
def test_scan_prunes_glob_excluded_directories(tmp_path: Path):
    """Lightroom preview bundles carry the catalog name, so only a glob entry catches them."""
    src = tmp_path / "src"
    for seed, rel in enumerate(("My Catalog Previews.lrdata", "Foo Smart Previews.lrdata"), 70):
        (src / rel).mkdir(parents=True)
        _gradient(320, 240, seed=seed).save(src / rel / "preview.jpg", "JPEG")
    _gradient(320, 240, seed=47).save(src / "keep_me.jpg", "JPEG", quality=92)

    work = tmp_path / "work"
    out = pf(work, "scan", str(src)).stdout
    names = {Path(r["source_path"]).name for r in q(work, "SELECT source_path FROM files")}
    assert names == {"keep_me.jpg"}
    assert "pruned 2 dirs" in out


@pytest.mark.exiftool
def test_min_size_does_not_drop_sidecars(tmp_path: Path):
    """An .xmp is a few hundred bytes by nature - min_size_bytes must not strand it."""
    src = tmp_path / "src"
    src.mkdir()
    _gradient(640, 480, seed=48).save(src / "photo.jpg", "JPEG", quality=92)  # ~25 KB
    (src / "photo.xmp").write_text("<x:xmpmeta xmlns:x='adobe:ns:meta/'/>", encoding="utf-8")
    Image.new("RGB", (8, 8), (9, 9, 9)).save(src / "thumb.jpg", "JPEG")  # ~630 B
    work = tmp_path / "work"
    work.mkdir(parents=True)
    (work / "photoflow.toml").write_text("min_size_bytes = 5000\n", encoding="utf-8")

    out = pf(work, "scan", str(src)).stdout
    rows = {Path(r["source_path"]).name: r["kind"] for r in q(work, "SELECT * FROM files")}
    assert rows == {"photo.jpg": "image", "photo.xmp": "sidecar"}
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


def test_first_parseable_date_respects_configured_min_year():
    import dataclasses

    from photoflow.config import Config
    from photoflow.scan import _first_parseable_date

    rec = {
        "CreationDate": "1985:06:01 12:00:00",
        "DateTimeOriginal": "2015:07:14 10:30:00",
    }
    lenient = dataclasses.replace(Config(), min_year=1970)
    assert _first_parseable_date(rec, lenient.min_year) == "1985:06:01 12:00:00"
    assert _first_parseable_date(rec, Config().min_year) == "2015:07:14 10:30:00"


def test_like_prefix_escapes_sql_wildcards():
    # Windows source roots contain '_' constantly (H:\_photos_backup); '_' is a LIKE wildcard.
    from photoflow.scan import _like_prefix

    assert _like_prefix(r"H:\_photos") == r"H:\~_photos%"
    assert _like_prefix("a%b~c") == "a~%b~~c%"


@pytest.mark.exiftool
def test_refresh_meta_rereads_copied_rows_without_rehashing(tmp_path: Path):
    from conftest import _set_exif

    src = tmp_path / "src"
    src.mkdir()
    img = src / "beach.jpg"
    _gradient(640, 480, seed=49).save(img, "JPEG", quality=92)
    _set_exif(img, DateTimeOriginal="2015:07:14 10:30:00", Model="Canon EOS 70D")
    work, lib = tmp_path / "work", tmp_path / "lib"
    pf(work, "scan", str(src))
    pf(work, "plan")
    pf(work, "apply", "--out", str(lib))

    before = q(work, "SELECT content_hash, status FROM files")[0]
    assert before["status"] == "copied"
    conn = open_db(work)
    conn.execute("UPDATE files SET exif_date='STALE', camera=NULL")
    conn.commit()
    conn.close()

    out = pf(work, "scan", "--refresh-meta", "--kind", "image").stdout
    row = q(work, "SELECT * FROM files")[0]
    assert row["exif_date"] == "2015:07:14 10:30:00"
    assert row["camera"] == "Canon EOS 70D"
    assert row["content_hash"] == before["content_hash"]  # never re-hashed
    assert row["status"] == "copied"  # lifecycle untouched
    assert "1 manifest rows marked for metadata refresh" in out
    assert "Next: photoflow plan" in out


@pytest.mark.exiftool
def test_refresh_meta_kind_and_prefix_filters(tmp_path: Path):
    a, b = tmp_path / "a", tmp_path / "b"
    a.mkdir()
    b.mkdir()
    _gradient(320, 240, seed=50).save(a / "one.jpg", "JPEG", quality=92)
    _gradient(320, 240, seed=51).save(b / "two.jpg", "JPEG", quality=92)
    work = tmp_path / "work"
    pf(work, "scan", str(a), str(b))
    conn = open_db(work)
    conn.execute("UPDATE files SET meta_read=1, exif_date='STALE'")
    conn.commit()
    conn.close()

    pf(work, "scan", "--refresh-meta", "--kind", "video", str(a))  # kind AND prefix
    assert all(r["exif_date"] == "STALE" for r in q(work, "SELECT exif_date FROM files"))

    pf(work, "scan", "--refresh-meta", "--kind", "image", str(a))  # only tree a
    rows = {Path(r["source_path"]).name: r for r in q(work, "SELECT * FROM files")}
    assert rows["one.jpg"]["exif_date"] is None  # re-read: this JPEG has no EXIF date
    assert rows["one.jpg"]["meta_read"] == 1
    assert rows["two.jpg"]["exif_date"] == "STALE"  # outside the prefix


def test_scan_without_sources_or_refresh_meta_is_an_error(tmp_path: Path):
    import subprocess
    import sys

    proc = subprocess.run(
        [sys.executable, "-m", "photoflow", "--workdir", str(tmp_path / "wd"), "scan"],
        capture_output=True,
        text=True,
    )
    assert proc.returncode != 0
    assert "refresh-meta" in (proc.stdout + proc.stderr)


@pytest.mark.exiftool
def test_refresh_meta_prefix_matches_whole_path_components(tmp_path: Path):
    # "...\photos" must not also match a sibling directory like "...\photos_backup\...".
    photos = tmp_path / "photos"
    photos_backup = tmp_path / "photos_backup"
    photos.mkdir()
    photos_backup.mkdir()
    _gradient(320, 240, seed=60).save(photos / "one.jpg", "JPEG", quality=92)
    _gradient(320, 240, seed=61).save(photos_backup / "two.jpg", "JPEG", quality=92)
    work = tmp_path / "work"
    pf(work, "scan", str(photos), str(photos_backup))
    conn = open_db(work)
    conn.execute("UPDATE files SET meta_read=1, exif_date='STALE'")
    conn.commit()
    conn.close()

    pf(work, "scan", "--refresh-meta", str(photos))
    rows = {Path(r["source_path"]).name: r for r in q(work, "SELECT * FROM files")}
    assert rows["one.jpg"]["exif_date"] is None  # re-read: this fixture has no EXIF date
    assert rows["one.jpg"]["meta_read"] == 1
    assert rows["two.jpg"]["exif_date"] == "STALE"  # photos_backup is not under photos/


def test_refresh_meta_skips_error_and_skipped_manual_rows(tmp_path: Path, monkeypatch, capsys):
    import argparse

    import photoflow.scan as scan_mod
    from photoflow.config import Config

    conn = open_db(tmp_path / "work")
    conn.executemany(
        "INSERT INTO files(source_path, kind, status, content_hash, meta_read) VALUES (?,?,?,?,1)",
        [
            (str(tmp_path / "err.jpg"), "image", "error", "a" * 64),
            (str(tmp_path / "skip.jpg"), "image", "skipped_manual", "b" * 64),
            (str(tmp_path / "ok.jpg"), "image", "copied", "c" * 64),
            # never fingerprinted (interrupted scan): read_metadata_pending won't select it,
            # so --refresh-meta must not count it as "marked" either.
            (str(tmp_path / "nohash.jpg"), "image", "scanned", None),
        ],
    )
    conn.commit()

    monkeypatch.setattr(scan_mod, "exiftool_json", lambda paths, batch, **kw: {})

    scan_mod._refresh_meta(conn, argparse.Namespace(kind=None, sources=None), Config())

    state = {
        Path(r["source_path"]).name: r["meta_read"]
        for r in conn.execute("SELECT source_path, meta_read FROM files")
    }
    assert state["err.jpg"] == 1, "error rows are left alone"
    assert state["skip.jpg"] == 1, "skipped_manual rows are left alone"
    assert state["ok.jpg"] == 0, "copied row was reset for re-read"
    assert state["nohash.jpg"] == 1, "un-fingerprinted rows are not part of a metadata refresh"
    assert "1 manifest rows marked" in capsys.readouterr().out
    conn.close()
