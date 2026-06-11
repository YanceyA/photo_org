from pathlib import Path

from photoflow.db import SCHEMA_VERSION, new_run, open_db


def test_open_db_creates_tables(tmp_path: Path):
    conn = open_db(tmp_path)
    names = {r["name"] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert {"files", "runs", "actions", "schema_version"} <= names


def test_schema_version_recorded(tmp_path: Path):
    conn = open_db(tmp_path)
    v = conn.execute("SELECT version FROM schema_version").fetchone()["version"]
    assert v == SCHEMA_VERSION == 1


def test_reopen_is_idempotent(tmp_path: Path):
    open_db(tmp_path).close()
    conn = open_db(tmp_path)
    assert conn.execute("SELECT COUNT(*) c FROM schema_version").fetchone()["c"] == 1


def test_new_run_increments(tmp_path: Path):
    conn = open_db(tmp_path)
    r1 = new_run(conn, "scan", {"sources": ["x"]})
    r2 = new_run(conn, "plan", {})
    assert (r1, r2) == (1, 2)
