"""Scan-phase regression tests (no exiftool required)."""

import pytest
from PIL import Image

from photoflow.db import open_db
from photoflow.hashing import HAVE_IMAGEHASH
from photoflow.scan import phash_pending_images


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
