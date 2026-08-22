"""Additive schema migrations on open_db: new columns reach pre-existing tables."""

import sqlite3

from photoflow.db import open_db


def test_open_db_adds_ignored_column_to_legacy_faces(tmp_path):
    # Simulate a DB created before faces.ignored existed: an old faces table (no `ignored`),
    # with a row. CREATE TABLE IF NOT EXISTS won't touch it, so open_db must ALTER it in.
    db = tmp_path / "photoflow.db"
    raw = sqlite3.connect(db)
    raw.executescript(
        "CREATE TABLE faces (id INTEGER PRIMARY KEY, file_id INTEGER, person_id INTEGER,"
        " cluster_id INTEGER, embedding BLOB);"
        "INSERT INTO faces(file_id) VALUES (1);"
    )
    raw.commit()
    raw.close()

    conn = open_db(tmp_path)
    cols = {r["name"] for r in conn.execute("PRAGMA table_info(faces)")}
    assert "ignored" in cols
    # existing rows default to not-ignored
    assert conn.execute("SELECT ignored FROM faces").fetchone()["ignored"] == 0

    # idempotent: opening again must not error or duplicate the column
    conn.close()
    open_db(tmp_path)


def test_open_db_adds_meta_read_column_to_legacy_files(tmp_path):
    # A DB created before files.meta_read existed. CREATE TABLE IF NOT EXISTS won't add the
    # column, so open_db must ALTER it in. A legacy row that already has a content_hash went
    # through the old inline exiftool pass, so the migration marks it read (1); a row without
    # one is an interrupted scan and stays 0 for the resumable passes to finish.
    db = tmp_path / "photoflow.db"
    raw = sqlite3.connect(db)
    raw.executescript(
        "CREATE TABLE files (id INTEGER PRIMARY KEY, source_path TEXT UNIQUE NOT NULL,"
        " content_hash TEXT, status TEXT DEFAULT 'scanned');"
        "INSERT INTO files(source_path, content_hash) VALUES ('C:/x/hashed.jpg', 'abc123');"
        "INSERT INTO files(source_path) VALUES ('C:/x/unhashed.jpg');"
    )
    raw.commit()
    raw.close()

    conn = open_db(tmp_path)
    cols = {r["name"] for r in conn.execute("PRAGMA table_info(files)")}
    assert "meta_read" in cols
    got = {
        r["source_path"]: r["meta_read"]
        for r in conn.execute("SELECT source_path, meta_read FROM files")
    }
    assert got == {"C:/x/hashed.jpg": 1, "C:/x/unhashed.jpg": 0}

    conn.close()
    open_db(tmp_path)  # idempotent


def test_open_db_adds_applied_sig_to_legacy_enrich_state(tmp_path):
    # enrich_state predates the incremental-apply signature; CREATE TABLE IF NOT EXISTS
    # can't add a column to an existing table, so open_db must ALTER it in.
    db = tmp_path / "photoflow.db"
    raw = sqlite3.connect(db)
    raw.executescript(
        "CREATE TABLE enrich_state (file_id INTEGER PRIMARY KEY, faces_done INTEGER,"
        " tags_done INTEGER, applied INTEGER, ts TEXT);"
        "INSERT INTO enrich_state(file_id, applied) VALUES (1, 1);"
    )
    raw.commit()
    raw.close()

    conn = open_db(tmp_path)
    cols = {r["name"] for r in conn.execute("PRAGMA table_info(enrich_state)")}
    assert "applied_sig" in cols
    assert conn.execute("SELECT applied_sig FROM enrich_state").fetchone()["applied_sig"] is None
    assert "errors" in cols
    assert conn.execute("SELECT errors FROM enrich_state").fetchone()["errors"] == 0
    conn.close()
    open_db(tmp_path)  # idempotent
