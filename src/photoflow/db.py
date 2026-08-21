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

-- enrich subsystem (additive; absent in pre-enrich DBs, created on next open).
-- Durable human-confirmed state: persons + faces.person_id + confirmed tags.
-- Ephemeral recomputed state: faces.cluster_id / cluster_prob (each enrich cluster).
CREATE TABLE IF NOT EXISTS persons (
    id INTEGER PRIMARY KEY,
    name TEXT UNIQUE NOT NULL,
    created TEXT
);

CREATE TABLE IF NOT EXISTS faces (
    id INTEGER PRIMARY KEY,
    file_id INTEGER NOT NULL,
    bbox TEXT,                 -- json [x1,y1,x2,y2] pixel coords in the library image
    det_score REAL,
    embedding BLOB,            -- float32 512-d, L2-normalized, ndarray.tobytes()
    img_w INTEGER,
    img_h INTEGER,
    person_id INTEGER,         -- DURABLE assignment (NULL = unassigned)
    cluster_id INTEGER,        -- EPHEMERAL: last cluster run (NULL = noise/assigned)
    cluster_prob REAL,         -- HDBSCAN membership prob; low = edge case for review
    ignored INTEGER DEFAULT 0, -- DURABLE "not interested": excluded from re-cluster/review
    thumb TEXT,                -- relative path to face-crop thumbnail
    FOREIGN KEY(file_id) REFERENCES files(id),
    FOREIGN KEY(person_id) REFERENCES persons(id)
);
CREATE INDEX IF NOT EXISTS idx_faces_file ON faces(file_id);
CREATE INDEX IF NOT EXISTS idx_faces_cluster ON faces(cluster_id);
CREATE INDEX IF NOT EXISTS idx_faces_person ON faces(person_id);

CREATE TABLE IF NOT EXISTS tags (
    id INTEGER PRIMARY KEY,
    file_id INTEGER NOT NULL,
    tag TEXT NOT NULL,
    source TEXT,               -- 'ram' | 'clip'
    score REAL,                -- per-tag confidence (NULL when the model gives none)
    status TEXT DEFAULT 'auto',-- auto (high-conf accept) | review (edge band) | rejected
    UNIQUE(file_id, tag),
    FOREIGN KEY(file_id) REFERENCES files(id)
);
CREATE INDEX IF NOT EXISTS idx_tags_file ON tags(file_id);
CREATE INDEX IF NOT EXISTS idx_tags_tag ON tags(tag);

CREATE TABLE IF NOT EXISTS enrich_state (   -- incremental skip, like scan's size+mtime rule
    file_id INTEGER PRIMARY KEY,
    faces_done INTEGER DEFAULT 0,
    tags_done INTEGER DEFAULT 0,
    applied INTEGER DEFAULT 0,
    applied_sig TEXT,          -- hash of what apply last wrote; equal => skip the rewrite
    ts TEXT
);
"""


def _migrate(conn: sqlite3.Connection) -> None:
    """Additive column migrations for DBs created before a column existed (CREATE TABLE
    IF NOT EXISTS can't add columns to a table that already exists)."""
    cols = {r["name"] for r in conn.execute("PRAGMA table_info(faces)")}
    if "ignored" not in cols:
        conn.execute("ALTER TABLE faces ADD COLUMN ignored INTEGER DEFAULT 0")
        conn.commit()

    cols = {r["name"] for r in conn.execute("PRAGMA table_info(enrich_state)")}
    if "applied_sig" not in cols:
        conn.execute("ALTER TABLE enrich_state ADD COLUMN applied_sig TEXT")
        conn.commit()


def open_db(workdir: Path) -> sqlite3.Connection:
    workdir.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(workdir / "photoflow.db")
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA)
    _migrate(conn)
    if conn.execute("SELECT COUNT(*) FROM schema_version").fetchone()[0] == 0:
        conn.execute("INSERT INTO schema_version(version) VALUES (?)", (SCHEMA_VERSION,))
        conn.commit()
    return conn


def new_run(conn, command, args) -> int:
    cur = conn.execute(
        "INSERT INTO runs(started, command, args) VALUES (?,?,?)",
        (datetime.now().isoformat(timespec="seconds"), command, json.dumps(args)),
    )
    conn.commit()
    return cur.lastrowid
