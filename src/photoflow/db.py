"""SQLite manifest: schema, connection, and run bookkeeping."""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime
from pathlib import Path

SCHEMA_VERSION = 1

SCHEMA = """
CREATE TABLE IF NOT EXISTS files (
    id INTEGER PRIMARY KEY,
    source_path TEXT UNIQUE NOT NULL,
    source_root TEXT,
    rel_path TEXT,
    size INTEGER,
    mtime REAL,
    ext TEXT,
    kind TEXT,
    content_hash TEXT,
    phash TEXT,
    width INTEGER,
    height INTEGER,
    exif_date TEXT,
    camera TEXT,
    date_taken TEXT,
    date_source TEXT,
    date_confidence TEXT,
    group_id INTEGER,
    dupe_of INTEGER,
    role TEXT,
    status TEXT DEFAULT 'scanned',
    dest_path TEXT,
    error TEXT
);
CREATE INDEX IF NOT EXISTS idx_hash ON files(content_hash);
CREATE INDEX IF NOT EXISTS idx_status ON files(status);

CREATE TABLE IF NOT EXISTS runs (
    id INTEGER PRIMARY KEY,
    started TEXT,
    command TEXT,
    args TEXT
);

CREATE TABLE IF NOT EXISTS actions (
    id INTEGER PRIMARY KEY,
    run_id INTEGER,
    file_id INTEGER,
    action TEXT,
    detail TEXT,
    ts TEXT
);

CREATE TABLE IF NOT EXISTS schema_version (version INTEGER NOT NULL);
"""


def open_db(workdir: Path) -> sqlite3.Connection:
    workdir.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(workdir / "photoflow.db")
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA)
    if conn.execute("SELECT COUNT(*) FROM schema_version").fetchone()[0] == 0:
        conn.execute("INSERT INTO schema_version(version) VALUES (?)", (SCHEMA_VERSION,))
        conn.commit()
    return conn


def new_run(conn, command, args) -> int:
    cur = conn.execute("INSERT INTO runs(started, command, args) VALUES (?,?,?)",
                       (datetime.now().isoformat(timespec="seconds"), command,
                        json.dumps(args)))
    conn.commit()
    return cur.lastrowid
