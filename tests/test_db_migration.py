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
    # column, so open_db must ALTER it in - defaulting existing rows to "metadata not read".
    db = tmp_path / "photoflow.db"
    raw = sqlite3.connect(db)
    raw.executescript(
        "CREATE TABLE files (id INTEGER PRIMARY KEY, source_path TEXT UNIQUE NOT NULL,"
        " content_hash TEXT, status TEXT DEFAULT 'scanned');"
        "INSERT INTO files(source_path) VALUES ('C:/x/y.jpg');"
    )
    raw.commit()
    raw.close()

    conn = open_db(tmp_path)
    cols = {r["name"] for r in conn.execute("PRAGMA table_info(files)")}
    assert "meta_read" in cols
    assert conn.execute("SELECT meta_read FROM files").fetchone()["meta_read"] == 0

    conn.close()
    open_db(tmp_path)  # idempotent
