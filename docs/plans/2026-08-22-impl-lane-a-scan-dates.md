# Lane A — scan / metadata / refile Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans (or subagent-driven-development) to implement this plan task-by-task.

**Goal:** Fix the four defects that are actively corrupting the photo library at ingest time and give the owner a repair path for the 45k files already copied: (1) junk source trees are walked and ingested, and 12 RAW extensions are silently dropped; (2) an interrupted `scan` permanently loses files; (3) video capture dates are lost entirely (`-fast2` stops before the trailing `moov` atom) or 12 h wrong (QuickTime dates are UTC); (4) there is no way to re-read metadata for rows already in the manifest, and no way to move an already-copied library file to the destination its corrected date implies.

**Architecture:** `photoflow` is a five-command pipeline (`scan → plan → review → apply → status`) over a SQLite manifest in `--workdir`. Every command module exposes `cmd_x(conn, workdir, run_id, log_fh, args, cfg)` and is dispatched from `cli.py`. Pure logic (`dates.py`, `naming.py`, `hashing.py`, `bktree.py`) has no I/O and is unit-tested without exiftool; infra (`db.py`, `exiftool.py`, `xmp.py`, `audit.py`) wraps the outside world. This lane adds two config knobs, one additive DB column (`files.meta_read`), one extracted function (`scan.read_metadata_pending`), one CLI flag (`scan --refresh-meta`), and one new command module (`refile.py`). It changes no naming rules and no durable-status semantics.

**Tech Stack:** Python 3.11+, stdlib `sqlite3`/`argparse`/`os.walk`/`struct`, exiftool 13.x on PATH, Pillow + ImageHash (optional at runtime, present in the dev group), pytest, ruff (line-length 100, target py311, `select = ["E","F","I","UP","B"]`), `uv` as the runner.

---

## Constraints — read before the first edit

1. **Work in a git worktree branched from `feature/enrich`.** Do not commit on `feature/enrich` itself.
   ```
   git -C C:\dev_projects\photo_org worktree add ../photo_org-lane-a -b feature/lane-a feature/enrich
   ```
   All commands below run from the worktree root.
2. **Never run `photoflow` against the repo's `photoflow_work/`.** It is the owner's live 153k-row manifest and an `enrich scan` is running against it. Every test and every manual smoke run uses a `tmp_path` workdir (`--workdir <tmp>`). Do not read, write, copy or delete anything under `photoflow_work/`.
3. **Sources are read-only** (HANDOFF §2.1). Nothing in this lane may write to, move, or delete a file under a scan source root. `refile` moves *library* files only.
4. **One commit per task**, conventional-commit message, after `uv run ruff check src tests && uv run ruff format src tests` and a **full green `uv run pytest`** (not just the task's file). exiftool is on PATH on this machine, so the `@pytest.mark.exiftool` tests do run — a skip there means something is wrong with the environment, not with your change.
5. Baseline at branch point (`0b9ad1c`): `ruff check` clean, `pytest` 156 passed / 1 skipped, ~2m40s.
6. Tests that must observe a **non-zero exit** cannot use the `pf()` helper in `tests/conftest.py:12` — it raises `AssertionError` on any non-zero return code. Call `subprocess.run([sys.executable, "-m", "photoflow", ...])` directly, as `tests/test_enrich_cli.py:24` does.
7. No binary assets in the repo. Image fixtures come from `conftest._gradient` (Pillow); the video fixture is built with `struct` (Task A3).

Task order is **A1 → A2 → A3 → A4 → A5**. A3 depends on the function A2 extracts; A4 depends on A3's function signature; A5 depends on A4 existing as the "how do I fix the data" front half.

---

### Task A1: Source hygiene — exclude dirs, min size, robust walk, more RAW extensions

Fixes review findings H4 (744 `.crw` silently skipped), H6 (no directory exclusion), C1 (`scan` aborts on one unreadable entry; `sorted(rglob("*"))` materialises the whole tree), and the `scan.py`/`planner.py` half of C10 (doc drift: "Next: python photoflow.py plan" — that file was deleted).

**Recommended agent:** sonnet — fully specified mechanical change to two files plus a config dataclass; no schema, no data mutation.

**Depends on:** nothing.

**Files:**
- Modify `src/photoflow/config.py` — line 14 (`_EXT_FIELDS`), lines 27–29 (`raw_ext`), after line 33 (new fields), line 96 (frozenset conversion).
- Modify `src/photoflow/scan.py` — lines 25–61 (the walk), line 104 (the "Next:" hint).
- Modify `src/photoflow/planner.py` — lines 174 and 176 (the "Next:" hints).
- Test: `tests/test_config.py` (new cases), `tests/test_scan.py` (new cases; the module currently claims "no exiftool required" in its docstring — widen it).

#### Steps

1. **Write the failing config test.** Append to `tests/test_config.py`:

```python
def test_source_hygiene_defaults():
    c = Config()
    # H4: RAW extensions that were silently dropped (744 Canon .crw in the owner's sources)
    for ext in (".crw", ".iiq", ".eip", ".erf", ".mrw", ".sr2", ".srf", ".nrw", ".rwl",
                ".mef", ".kdc", ".dcr", ".3fr"):
        assert ext in c.raw_ext, ext
    assert ".cr2" in c.raw_ext  # pre-existing entries survive
    # H6: junk trees that must never be descended into
    for d in ("CaptureOne", "Cache", "Proxies", "Thumbnails", "Trash", "$RECYCLE.BIN",
              "System Volume Information", "@eaDir", ".thumbnails", ".Trash", ".Trashes",
              "Previews.lrdata", "Smart Previews.lrdata", "Lightroom Settings", "__MACOSX"):
        assert d in c.exclude_dirs, d
    assert isinstance(c.exclude_dirs, frozenset)
    assert c.min_size_bytes == 0  # opt-in; 20000 is the documented value for thumbnail-laden sources


def test_exclude_dirs_and_min_size_from_toml(tmp_path: Path):
    (tmp_path / "photoflow.toml").write_text(
        'exclude_dirs = ["Output", "Selects"]\nmin_size_bytes = 20000\n', encoding="utf-8"
    )
    c = load_config(tmp_path)
    assert c.exclude_dirs == frozenset({"Output", "Selects"})  # list -> frozenset like the ext sets
    assert c.min_size_bytes == 20000
```

2. **Run it, expect failure.**
   ```
   uv run pytest -q tests/test_config.py
   ```
   Expected: `AttributeError: 'Config' object has no attribute 'exclude_dirs'` in both new tests; the six pre-existing tests still pass.

3. **Implement the config change.** In `src/photoflow/config.py`:

Replace line 14 with:
```python
_EXT_FIELDS = frozenset({"image_ext", "raw_ext", "video_ext", "sidecar_ext"})
# every field whose TOML value is a list but whose dataclass type is frozenset[str]
_FROZENSET_FIELDS = _EXT_FIELDS | {"exclude_dirs"}
```

Replace the `raw_ext` default (lines 27–29) with:
```python
    raw_ext: frozenset[str] = frozenset(
        {
            ".cr2", ".cr3", ".crw", ".nef", ".nrw", ".arw", ".sr2", ".srf", ".dng",
            ".orf", ".rw2", ".raf", ".pef", ".srw", ".x3f", ".iiq", ".3fr", ".eip",
            ".erf", ".mrw", ".rwl", ".mef", ".kdc", ".dcr",
        }
    )
```

Insert after the `sidecar_ext` field (line 33):
```python
    # Directory names never descended into, matched case-insensitively against each path
    # component BELOW the source root (the root itself is always scanned, even if its own
    # name is listed). Caches, proxies, previews and recycle bins - ingesting them fills the
    # library with derivatives of files it already has.
    exclude_dirs: frozenset[str] = frozenset(
        {
            "CaptureOne", "Cache", "Proxies", "Thumbnails", "Trash", "$RECYCLE.BIN",
            "System Volume Information", "@eaDir", ".thumbnails", ".Trash", ".Trashes",
            "Previews.lrdata", "Smart Previews.lrdata", "Lightroom Settings", "__MACOSX",
        }
    )
    # Files smaller than this are skipped at walk time. 0 = off (default). 20000 is a
    # sensible value for sources littered with camera/app thumbnails.
    min_size_bytes: int = 0
```

Replace line 96 (`for k in _EXT_FIELDS & data.keys():`) with:
```python
    for k in _FROZENSET_FIELDS & data.keys():
```

4. **Run the config tests, expect PASS.**
   ```
   uv run pytest -q tests/test_config.py
   ```
   Expected: `8 passed`.

5. **Write the failing scan tests.** Replace the docstring at the top of `tests/test_scan.py` and add imports + three tests:

```python
"""Scan-phase regression tests (pure ones need no exiftool; walk tests are marked)."""

import os
from pathlib import Path

import pytest
from conftest import _gradient, pf, q
from PIL import Image

from photoflow.db import open_db
from photoflow.hashing import HAVE_IMAGEHASH
from photoflow.scan import cmd_scan, phash_pending_images
```

```python
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
```

6. **Run them, expect failure.**
   ```
   uv run pytest -q tests/test_scan.py
   ```
   Expected: `test_scan_prunes_excluded_directories` fails on `names == {...}` (all 5 files ingested), `test_scan_skips_files_below_min_size` fails the same way, `test_scan_counts_unreadable_entries_instead_of_crashing` fails with `AttributeError: module 'photoflow.scan' has no attribute 'os'`, `test_next_hints_use_the_installed_command_name` fails on `scan.py`.

7. **Implement the walk.** In `src/photoflow/scan.py`, add `import os` under `import sys` (line 5), then replace lines 25–61 (from `new_paths = []` through the `print(f"{len(new_paths)} new/changed files to fingerprint")`) with:

```python
    new_paths = []
    pruned = unreadable = too_small = 0
    excluded = {d.lower() for d in cfg.exclude_dirs}

    def _walk_error(err: OSError) -> None:
        nonlocal unreadable
        unreadable += 1
        print(f"  unreadable: {err}")

    for root in args.sources:
        root = Path(root).expanduser().resolve()
        if not root.exists():
            print(f"skipping missing source: {root}")
            continue
        print(f"scanning {root} ...")
        for dirpath, dirnames, filenames in os.walk(root, onerror=_walk_error):
            keep = []
            for d in sorted(dirnames):
                if d.lower() in excluded:
                    pruned += 1
                    continue
                keep.append(d)
            dirnames[:] = keep  # in-place: this is what prunes the walk
            for fn in sorted(filenames):
                if fn.startswith("."):
                    continue
                ext = Path(fn).suffix.lower()
                kind = classify(ext, cfg)
                if kind == "other":
                    continue
                p = Path(dirpath) / fn
                try:
                    st = p.stat()
                except OSError as e:
                    unreadable += 1
                    print(f"  unreadable: {e}")
                    continue
                if st.st_size < cfg.min_size_bytes:
                    too_small += 1
                    continue
                sp = str(p)
                existing = conn.execute(
                    "SELECT size, mtime FROM files WHERE source_path=?", (sp,)
                ).fetchone()
                if (
                    existing
                    and existing["size"] == st.st_size
                    and abs(existing["mtime"] - st.st_mtime) < 1
                ):
                    continue  # already scanned, unchanged
                conn.execute(
                    """INSERT INTO files(source_path, source_root, rel_path, size,
                                         mtime, ext, kind)
                       VALUES (?,?,?,?,?,?,?)
                       ON CONFLICT(source_path) DO UPDATE SET
                         size=excluded.size, mtime=excluded.mtime, status='scanned',
                         content_hash=NULL, phash=NULL""",
                    (
                        sp,
                        str(root),
                        str(p.relative_to(root)),
                        st.st_size,
                        st.st_mtime,
                        ext,
                        kind,
                    ),
                )
                new_paths.append(sp)
    conn.commit()
    print(
        f"walk: pruned {pruned} dirs, skipped {unreadable} unreadable, "
        f"{too_small} below min size"
    )
    print(f"{len(new_paths)} new/changed files to fingerprint")
```

Note the walk order changed from one global `sorted(rglob("*"))` to per-directory sorting — determinism is preserved within a directory, which is all the pipeline relies on (dedupe keeper choice is by `status`/`mtime`, `planner.py:46`, not by walk order).

8. **Fix the "Next:" hints.** In `src/photoflow/scan.py` line 104: `print("scan complete. Next: photoflow plan")`. In `src/photoflow/planner.py` line 174: `print("Next: photoflow review")`; line 176: `print("Next: photoflow apply --out <DIR>")`.

9. **Run the whole suite, expect PASS.**
   ```
   uv run pytest -q
   ```
   Expected: `160 passed, 1 skipped` (156 + 2 config + 4 scan − 2 replaced-none... count is informational; the requirement is **0 failed**).

10. **Lint and format.**
    ```
    uv run ruff check src tests && uv run ruff format src tests
    ```
    Expected: `All checks passed!` then `N files left unchanged` (or a reformat, which you then re-run `pytest -q` after).

11. **Commit.**
    ```
    git add src/photoflow/config.py src/photoflow/scan.py src/photoflow/planner.py tests/test_config.py tests/test_scan.py
    git commit -m "feat(scan): exclude junk dirs, min-size filter, robust os.walk, 13 more RAW extensions"
    ```

---

### Task A2: Scan resume correctness — `files.meta_read` and a manifest-driven metadata pass

Fixes review finding H8: rows are inserted with `content_hash=NULL` and hashes are committed every 500, so an interrupted `scan` leaves NULL-hash rows that the size+mtime skip rule (`scan.py:44-49`) treats as done and `plan` filters out (`planner.py:31`) — those files are never copied and never reported. Latent today (0 NULL-hash rows in the live manifest) but it is a silent data-loss path. The same change makes the exiftool pass resumable and gives A4 the column it needs.

**Recommended agent:** opus — additive schema migration plus a change to the durable resume semantics of the manifest; getting the `meta_read=1` bookkeeping wrong makes exiftool re-read 153k files forever or skip them forever.

**Depends on:** A1.

**Files:**
- Modify `src/photoflow/db.py` — line 22 area (SCHEMA `files` table), lines 111–117 (`_migrate`).
- Modify `src/photoflow/scan.py` — the skip rule and the `ON CONFLICT` clause inside the walk (written in A1 step 7), and lines 77–94 (the inline exif pass → extracted function).
- Test: `tests/test_db_migration.py`, `tests/test_scan.py`.

#### Steps

1. **Write the failing migration test.** Append to `tests/test_db_migration.py`:

```python
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
```

2. **Run it, expect failure.**
   ```
   uv run pytest -q tests/test_db_migration.py
   ```
   Expected: `AssertionError: assert 'meta_read' in {...}`.

3. **Implement the migration.** In `src/photoflow/db.py`, add to the `files` CREATE TABLE (after `status TEXT DEFAULT 'scanned',` on line 34):
```sql
    meta_read INTEGER DEFAULT 0,
```
and extend `_migrate` (lines 111–117):
```python
def _migrate(conn: sqlite3.Connection) -> None:
    """Additive column migrations for DBs created before a column existed (CREATE TABLE
    IF NOT EXISTS can't add columns to a table that already exists)."""
    cols = {r["name"] for r in conn.execute("PRAGMA table_info(faces)")}
    if "ignored" not in cols:
        conn.execute("ALTER TABLE faces ADD COLUMN ignored INTEGER DEFAULT 0")
        conn.commit()
    cols = {r["name"] for r in conn.execute("PRAGMA table_info(files)")}
    if "meta_read" not in cols:
        # 0 = "exiftool has not read this row yet". Pre-existing rows were read by the old
        # inline pass, but re-reading them is harmless (and scan --refresh-meta wants exactly
        # this flag), so defaulting to 0 is the safe direction.
        conn.execute("ALTER TABLE files ADD COLUMN meta_read INTEGER DEFAULT 0")
        conn.commit()
```

4. **Run the migration tests, expect PASS.**
   ```
   uv run pytest -q tests/test_db_migration.py
   ```
   Expected: `2 passed`.

5. **Write the failing scan-resume tests.** Append to `tests/test_scan.py`:

```python
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
        "INSERT INTO files(source_path, kind, status, content_hash, meta_read)"
        " VALUES (?,?,?,?,0)",
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
    assert conn.execute(
        "SELECT exif_date FROM files WHERE source_path=?", (str(img),)
    ).fetchone()["exif_date"] == "TOUCHED"
```

6. **Run them, expect failure.**
   ```
   uv run pytest -q tests/test_scan.py
   ```
   Expected: `ImportError: cannot import name 'read_metadata_pending' from 'photoflow.scan'` in the second test; the first fails on `assert row["content_hash"]` (the unchanged file is skipped).

7. **Implement.** In `src/photoflow/scan.py`, inside the walk written in A1 step 7:

- change the existing-row lookup to fetch the hash:
```python
                existing = conn.execute(
                    "SELECT size, mtime, content_hash FROM files WHERE source_path=?", (sp,)
                ).fetchone()
                if (
                    existing
                    and existing["content_hash"] is not None
                    and existing["size"] == st.st_size
                    and abs(existing["mtime"] - st.st_mtime) < 1
                ):
                    continue  # already scanned AND fingerprinted, unchanged
```
- add `meta_read=0` to the upsert's DO UPDATE SET clause:
```sql
                       ON CONFLICT(source_path) DO UPDATE SET
                         size=excluded.size, mtime=excluded.mtime, status='scanned',
                         content_hash=NULL, phash=NULL, meta_read=0
```

Then replace the inline exif block (old lines 77–94, `print("reading metadata (exiftool)...")` through the following `conn.commit()`) with:
```python
    read_metadata_pending(conn, cfg)
```

and add the new function below `cmd_scan` (above `phash_pending_images`):

```python
def read_metadata_pending(conn, cfg) -> int:
    """Read exiftool metadata for every manifest row still flagged meta_read=0.

    Manifest-driven for the same reasons as phash_pending_images: an IN(<paths>) clause
    overflows SQLite's 32766-variable cap on large imports, and an interrupted scan resumes
    here instead of losing the pass. meta_read is set to 1 per batch even when exiftool
    returned nothing for a path - otherwise a tag-less file is retried on every future run.
    """
    rows = conn.execute(
        "SELECT id, source_path, kind FROM files "
        "WHERE status='scanned' AND content_hash IS NOT NULL AND meta_read=0"
    ).fetchall()
    if not rows:
        return 0
    print(f"reading metadata (exiftool) for {len(rows)} files...")
    done = 0
    for i in range(0, len(rows), cfg.exiftool_batch):
        batch = rows[i : i + cfg.exiftool_batch]
        meta = exiftool_json([r["source_path"] for r in batch], cfg.exiftool_batch)
        for r in batch:
            rec = meta.get(r["source_path"], {})
            raw_date = (
                rec.get("DateTimeOriginal") or rec.get("CreateDate") or rec.get("MediaCreateDate")
            )
            conn.execute(
                "UPDATE files SET exif_date=?, camera=?, width=?, height=?, meta_read=1"
                " WHERE id=?",
                (
                    str(raw_date) if raw_date else None,
                    rec.get("Model"),
                    rec.get("ImageWidth"),
                    rec.get("ImageHeight"),
                    r["id"],
                ),
            )
        conn.commit()
        done += len(batch)
        print(f"  metadata {done}/{len(rows)}")
    return done
```

8. **Run the whole suite, expect PASS.**
   ```
   uv run pytest -q
   ```
   Expected: 0 failed. If `test_full_pipeline` fails on `date_sources.get("exif") == 5`, the metadata pass is not reaching rows — check that `meta_read` defaults to 0 in the SCHEMA and that the walk resets it.

9. **Lint and format.**
   ```
   uv run ruff check src tests && uv run ruff format src tests
   ```

10. **Commit.**
    ```
    git add src/photoflow/db.py src/photoflow/scan.py tests/test_db_migration.py tests/test_scan.py
    git commit -m "fix(scan): resumable fingerprint + metadata passes via files.meta_read"
    ```

---

### Task A3: Correct video metadata — no `-fast2` for video, `QuickTimeUTC`, prefer `CreationDate`

Fixes review findings H1 and H2. Verified against exiftool 13.59 on this machine with the exact 160-byte fixture below:

| exiftool flags | result |
|---|---|
| `-fast2` | `{}` — nothing but `SourceFile` |
| (none) | `"CreateDate": "2010:09:03 16:03:31"` |
| `-api QuickTimeUTC=1` | `"CreateDate": "2010:09:04 04:03:31+12:00"` |
| `-fast2 -api QuickTimeUTC=1` | `{}` |

`-fast2` stops before the trailing `moov` atom that MP4/MOV files put at the end, which is why 3,100 of 4,580 videos in the live manifest have `exif_date IS NULL`. `QuickTimeUTC=1` tells exiftool the stored time is UTC and converts it to the machine's local zone, appending the offset. `dates.parse_exif_date` (`dates.py:28-32`) regex-matches only the leading `YYYY:MM:DD HH:MM:SS` via `EXIF_DATE_RE` (`dates.py:13`), so the trailing `+12:00` is discarded harmlessly — step 1 pins that down with a test.

**Recommended agent:** opus — changes how every capture date in the library is read; the tz semantics and the tag-preference order are easy to get subtly wrong and the blast radius is the whole library.

**Depends on:** A2 (`read_metadata_pending` is where the split call lives).

**Files:**
- Modify `src/photoflow/exiftool.py` — lines 13–20 (`EXIF_TAGS`), lines 27–38 (`exiftool_json` signature + argfile header).
- Modify `src/photoflow/scan.py` — `read_metadata_pending` (written in A2 step 7).
- Test: `tests/conftest.py` (new `make_minimal_mp4` helper), `tests/test_dates.py`, `tests/test_scan.py`.

#### Steps

1. **Write the failing date test.** Append to `tests/test_dates.py`:

```python
def test_parse_exif_date_ignores_a_trailing_timezone_offset():
    # exiftool -api QuickTimeUTC=1 returns tz-aware strings for QuickTime dates. The library
    # stores wall-clock local time, so the offset is deliberately discarded.
    assert parse_exif_date("2010:09:04 04:03:31+12:00") == datetime(2010, 9, 4, 4, 3, 31)
    assert parse_exif_date("2010:09:03 16:03:31-05:00") == datetime(2010, 9, 3, 16, 3, 31)
```

`tests/test_dates.py:1-9` already imports `datetime` and `parse_exif_date`, so no import changes are needed. The file groups tests in classes; a module-level function appended at the end is collected normally.

2. **Run it.**
   ```
   uv run pytest -q tests/test_dates.py
   ```
   Expected: **PASS immediately** — this is a characterisation test proving the assumption A3 relies on. If it fails, stop and re-read `dates.py:13`; the rest of this task is unsafe.

3. **Add the MP4 fixture builder.** Append to `tests/conftest.py`:

```python
_QT_EPOCH = datetime(1904, 1, 1, tzinfo=timezone.utc)  # QuickTime/MP4 time origin


def _box(kind: bytes, payload: bytes) -> bytes:
    return struct.pack(">I", len(payload) + 8) + kind + payload


def make_minimal_mp4(path: Path, creation_dt: datetime) -> int:
    """Write a valid 160-byte MP4 whose mvhd creation_time is `creation_dt` (must be tz-aware).

    ftyp + a stub mdat + a TRAILING moov/mvhd - the layout real cameras and phones use, and
    the reason -fast2 (which stops before the trailing moov) returns nothing for them.
    No binary asset needed; exiftool reads this as CreateDate.
    """
    secs = int((creation_dt - _QT_EPOCH).total_seconds())
    ftyp = _box(b"ftyp", b"isom" + struct.pack(">I", 512) + b"isomiso2mp41")
    mdat = _box(b"mdat", b"\x00" * 8)
    unity_matrix = struct.pack(">9i", 0x10000, 0, 0, 0, 0x10000, 0, 0, 0, 0x40000000)
    mvhd = _box(
        b"mvhd",
        struct.pack(">I", 0)  # version 0 + 3 flag bytes
        + struct.pack(">I", secs)  # creation_time
        + struct.pack(">I", secs)  # modification_time
        + struct.pack(">I", 1000)  # timescale
        + struct.pack(">I", 0)  # duration
        + struct.pack(">I", 0x00010000)  # rate 1.0
        + struct.pack(">H", 0x0100)  # volume 1.0
        + b"\x00" * 10  # reserved
        + unity_matrix
        + b"\x00" * 24  # pre_defined
        + struct.pack(">I", 2),  # next_track_id
    )
    data = ftyp + mdat + _box(b"moov", mvhd)
    path.write_bytes(data)
    return len(data)
```

Add to the imports at the top of `tests/conftest.py`:
```python
import struct
from datetime import datetime, timezone
```

4. **Write the failing exiftool tests.** Create `tests/test_exiftool_video.py`:

```python
"""Video metadata reads: -fast2 must not be used for QuickTime, and dates are UTC."""

import types
from datetime import datetime, timezone
from pathlib import Path

import pytest
from conftest import make_minimal_mp4

from photoflow.exiftool import EXIF_TAGS, exiftool_json

UTC_CREATION = datetime(2010, 9, 3, 16, 3, 31, tzinfo=timezone.utc)


def test_creation_date_tag_is_requested():
    # QuickTime Keys:CreationDate is what iPhones write, and it carries a real tz offset.
    assert "-CreationDate" in EXIF_TAGS


@pytest.mark.exiftool
def test_fast_mode_misses_the_trailing_moov_and_slow_mode_finds_it(tmp_path: Path):
    clip = tmp_path / "clip.mp4"
    assert make_minimal_mp4(clip, UTC_CREATION) == 160

    fast = exiftool_json([str(clip)], 200, fast=True)
    assert "CreateDate" not in fast[str(clip)], "-fast2 must not be used for video (H1)"

    slow = exiftool_json([str(clip)], 200, fast=False)
    # QuickTimeUTC converts the stored UTC time to THIS machine's local zone, so compute
    # the expectation rather than hard-coding an offset.
    expect = UTC_CREATION.astimezone().strftime("%Y:%m:%d %H:%M:%S")
    assert str(slow[str(clip)]["CreateDate"]).startswith(expect)


def test_argfile_omits_fast2_when_fast_is_false_and_always_sets_quicktimeutc(monkeypatch):
    seen: list[str] = []

    def fake_run(argv, **kw):
        seen.append(Path(argv[argv.index("-@") + 1]).read_text(encoding="utf-8"))
        return types.SimpleNamespace(stdout="[]", stderr="", returncode=0)

    import photoflow.exiftool as et

    monkeypatch.setattr(et.subprocess, "run", fake_run)
    et.exiftool_json(["a.mp4"], 200, fast=False)
    et.exiftool_json(["b.jpg"], 200, fast=True)

    video_args, image_args = seen
    assert "-fast2\n" not in video_args
    assert "-fast2\n" in image_args
    for args in (video_args, image_args):
        assert "-api\nQuickTimeUTC=1\n" in args
        assert "-CreationDate\n" in args
```

Also append to `tests/test_scan.py` a pure test of the tag-preference order:

```python
def test_video_metadata_prefers_creation_date_over_create_date(tmp_path, monkeypatch):
    """CreationDate (QuickTime Keys, tz-aware, what iPhones write) wins over CreateDate."""
    from photoflow.config import Config
    from photoflow.scan import read_metadata_pending

    conn = open_db(tmp_path / "work")
    conn.execute(
        "INSERT INTO files(source_path, kind, status, content_hash, meta_read)"
        " VALUES (?,?,?,?,0)",
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
```

5. **Run them, expect failure.**
   ```
   uv run pytest -q tests/test_exiftool_video.py tests/test_scan.py
   ```
   Expected: `test_creation_date_tag_is_requested` fails (`-CreationDate` not in `EXIF_TAGS`); the two `exiftool_json(..., fast=...)` tests fail with `TypeError: exiftool_json() got an unexpected keyword argument 'fast'`; the preference test fails (`exif_date == "2010:09:03 16:03:31"`).

6. **Implement the exiftool change.** In `src/photoflow/exiftool.py`, replace lines 13–20:
```python
EXIF_TAGS = [
    # QuickTime Keys:CreationDate - tz-aware, written by iPhones, preferred for video
    "-CreationDate",
    "-DateTimeOriginal",
    "-CreateDate",
    "-MediaCreateDate",
    "-Model",
    "-ImageWidth",
    "-ImageHeight",
]
```
and the signature + argfile header (lines 27 and 33):
```python
def exiftool_json(paths: list[str], batch_size: int = 200, *, fast: bool = True) -> dict[str, dict]:
    """Run exiftool on a batch of paths, return {path: tags}.

    fast=True adds -fast2, which stops reading before trailing metadata - a big speedup for
    JPEG/RAW. It MUST be False for QuickTime (MP4/MOV): those keep their moov atom at the END
    of the file, so -fast2 returns nothing at all (verified: exiftool 13.59 returns {} for a
    trailing-moov MP4 with -fast2 and CreateDate without it).

    -api QuickTimeUTC=1 is always on: QuickTime dates are UTC by spec, and without this the
    library files a midnight clip under the wrong day (12-13 h off in NZ).
    """
```
```python
            af.write("-j\n-n\n")
            if fast:
                af.write("-fast2\n")
            af.write("-api\nQuickTimeUTC=1\n-charset\nfilename=utf8\n")
```

7. **Implement the split call.** In `read_metadata_pending` (`src/photoflow/scan.py`), replace the single `meta = exiftool_json(...)` line with:
```python
        # -fast2 is a large win for JPEG/RAW but returns NOTHING for trailing-moov QuickTime,
        # so video is read in a second, slower call.
        video = [r["source_path"] for r in batch if r["kind"] == "video"]
        other = [r["source_path"] for r in batch if r["kind"] != "video"]
        meta = exiftool_json(other, cfg.exiftool_batch, fast=True)
        meta.update(exiftool_json(video, cfg.exiftool_batch, fast=False))
```
(`exiftool_json([])` returns `{}` without spawning a process, so the empty case costs nothing.)

and the date preference:
```python
            raw_date = (
                rec.get("CreationDate")
                or rec.get("DateTimeOriginal")
                or rec.get("CreateDate")
                or rec.get("MediaCreateDate")
            )
```

8. **Run the whole suite, expect PASS.**
   ```
   uv run pytest -q
   ```
   Expected: 0 failed. Watch `test_full_pipeline`'s `date_sources.get("exif") == 5` — the fixture has no video, so it must be unaffected.

9. **Lint and format.**
   ```
   uv run ruff check src tests && uv run ruff format src tests
   ```

10. **Commit.**
    ```
    git add src/photoflow/exiftool.py src/photoflow/scan.py tests/conftest.py tests/test_dates.py tests/test_exiftool_video.py tests/test_scan.py
    git commit -m "fix(scan): read video dates correctly (no -fast2, QuickTimeUTC, prefer CreationDate)"
    ```

---

### Task A4: `scan --refresh-meta [PREFIX …] [--kind K]`

Fixes review finding H3: with A3 in place, nothing re-reads metadata for the 45,194 rows already marked `copied` — `scan`'s size+mtime rule skips them and `status='copied'` is durable. `--refresh-meta` resets `meta_read=0` for a selected subset and re-runs only the metadata pass. No re-hashing, no status changes, sources untouched (exiftool reads them, never writes).

**Confirmed by reading `planner.py`:** `plan` re-resolves the date for copied rows too — the row query at `planner.py:30-32` is `WHERE status IN ('scanned','copied') AND content_hash IS NOT NULL`, the cascade at `planner.py:132-135` runs over every row in `by_id`, and the write-back at `planner.py:142-156` sets `date_taken`/`date_source`/`date_confidence` for every row while preserving `status='copied'` (`planner.py:139-141`). So `scan --refresh-meta` → `plan` is sufficient to move a copied row's `date_taken`; `refile` (A5) then moves the file to match.

**Recommended agent:** sonnet — argparse plumbing plus one parameterised SQL builder; the semantics are fully pinned down above.

**Depends on:** A3.

**Files:**
- Modify `src/photoflow/cli.py` — lines 41–42 (the `scan` parser).
- Modify `src/photoflow/scan.py` — top of `cmd_scan`, `read_metadata_pending` signature, new `_refresh_meta` + `_like_prefix` helpers.
- Test: `tests/test_scan.py`.

#### Steps

1. **Write the failing tests.** Append to `tests/test_scan.py`:

```python
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
```

2. **Run them, expect failure.**
   ```
   uv run pytest -q tests/test_scan.py
   ```
   Expected: `ImportError: cannot import name '_like_prefix'`; the three CLI tests fail at `pf(...)` with `error: unrecognized arguments: --refresh-meta`; the last one fails because argparse's `nargs="+"` already errors — but with a message that does not mention `refresh-meta`.

3. **Wire the CLI.** In `src/photoflow/cli.py`, replace lines 41–42:
```python
    p = sub.add_parser("scan", help="fingerprint source folders into the manifest")
    p.add_argument(
        "sources",
        nargs="*",
        help="source folders to scan (with --refresh-meta: path prefixes to limit the refresh)",
    )
    p.add_argument(
        "--refresh-meta",
        action="store_true",
        help="re-read exiftool metadata for rows already in the manifest (no re-hash, no copy)",
    )
    p.add_argument(
        "--kind",
        action="append",
        choices=["image", "raw", "video", "sidecar"],
        help="with --refresh-meta: limit to this kind (repeatable)",
    )
```

4. **Implement the refresh path.** In `src/photoflow/scan.py`, give `read_metadata_pending` a `statuses` parameter:
```python
def read_metadata_pending(conn, cfg, statuses: tuple[str, ...] | None = ("scanned",)) -> int:
```
and build its query from it (replacing the hard-coded `status='scanned'`):
```python
    sql = "SELECT id, source_path, kind FROM files WHERE content_hash IS NOT NULL AND meta_read=0"
    params: list = []
    if statuses:
        sql += " AND status IN (%s)" % ",".join("?" * len(statuses))
        params += list(statuses)
    rows = conn.execute(sql, params).fetchall()
```

Add the helpers above `cmd_scan`:
```python
def _like_prefix(prefix: str) -> str:
    """SQL LIKE pattern matching everything under `prefix`, with wildcards escaped.

    Windows source roots are full of '_' (H:\\_photos_backup) and '_' is a LIKE wildcard,
    so the pattern is escaped and used with ESCAPE '~'.
    """
    esc = prefix.replace("~", "~~").replace("%", "~%").replace("_", "~_")
    return esc + "%"


def _refresh_meta(conn, args, cfg) -> None:
    """Re-run only the exiftool pass over rows already in the manifest.

    Repairs metadata for files whose bytes never changed but whose read was wrong (e.g. the
    video dates fixed in this lane). Never re-hashes, never changes `status`, and applies to
    `copied` rows too - `plan` then recomputes date_taken for them (planner.py:30,132,142)
    and `refile` moves the library file to match.
    """
    where, params = [], []
    kinds = args.kind or []
    if kinds:
        where.append("kind IN (%s)" % ",".join("?" * len(kinds)))
        params += kinds
    prefixes = [str(Path(s).expanduser().resolve()) for s in (args.sources or [])]
    if prefixes:
        where.append(" OR ".join(["source_path LIKE ? ESCAPE '~'"] * len(prefixes)))
        params += [_like_prefix(p) for p in prefixes]
    sql = "UPDATE files SET meta_read=0"
    if where:
        sql += " WHERE " + " AND ".join(f"({w})" for w in where)
    n = conn.execute(sql, params).rowcount
    conn.commit()
    print(f"{n} manifest rows marked for metadata refresh")
    read_metadata_pending(conn, cfg, statuses=None)
    print("refresh complete. Next: photoflow plan")
```

At the top of `cmd_scan`, after the `exiftool_available()` guard (`scan.py:15-16`) and before the ImageHash notes:
```python
    if getattr(args, "refresh_meta", False):
        _refresh_meta(conn, args, cfg)
        return
    if not args.sources:
        sys.exit("scan: give at least one source folder, or use --refresh-meta [PREFIX ...]")
```

5. **Run the scan tests, expect PASS.**
   ```
   uv run pytest -q tests/test_scan.py
   ```
   Expected: all pass. If `test_refresh_meta_kind_and_prefix_filters` fails on `two.jpg`, the prefix `OR` group is not parenthesised — check the `f"({w})"` wrapping.

6. **Run the whole suite, expect PASS.**
   ```
   uv run pytest -q
   ```
   Expected: 0 failed.

7. **Lint and format.**
   ```
   uv run ruff check src tests && uv run ruff format src tests
   ```

8. **Commit.**
   ```
   git add src/photoflow/cli.py src/photoflow/scan.py tests/test_scan.py
   git commit -m "feat(scan): --refresh-meta re-reads metadata for existing manifest rows"
   ```

---

### Task A5: `refile --out DIR [--dry-run]`

The repair half of H3. After `scan --refresh-meta` → `plan` has corrected `date_taken` for copied rows, their `dest_path` still points at the old (wrong) library location; `apply` will never touch them because `status='copied'` is durable and it only processes `planned`/`review` (`apply.py:27`). `refile` recomputes `dest_for(row)` for every copied row and moves the library file — and its `.xmp` sidecar — to match, updating `dest_path` and writing an audit row.

**Immich note (put it in the module docstring and the command help):** a moved file looks like *delete + add* to Immich, digiKam and any mtime-based backup. Rescan the external library after a refile run.

**Recommended agent:** opus — this is the only task in the lane that moves real user data (45k library files). The collision/missing-file pre-flight is the safety property; get it wrong and files are overwritten.

**Depends on:** A4.

**Files:**
- Create `src/photoflow/refile.py`.
- Modify `src/photoflow/cli.py` — the imports block, the `sub.add_parser` block (after `apply`, ~line 51), the dispatch dict (lines 82–88).
- Test: create `tests/test_refile.py`.

#### Steps

1. **Write the failing tests.** Create `tests/test_refile.py`:

```python
"""refile: move already-copied library files to the dest their corrected date implies."""

import io
from pathlib import Path
from types import SimpleNamespace

import pytest
from conftest import pf, q

from photoflow.config import Config
from photoflow.db import new_run, open_db
from photoflow.refile import cmd_refile

H = "deadbeef" + "0" * 56  # content_hash -> hash8 'deadbeef'
OLD_REL = Path("2018") / "08" / "20180813_IMG-0735_deadbeef.mov"
NEW_REL = Path("2010") / "09" / "20100904_040331_IMG-0735_deadbeef.mov"


def make_lib(tmp_path: Path, *, sidecar=True, date="2010-09-04T04:03:31"):
    """A workdir + library holding one copied row filed under the WRONG (import-year) folder."""
    work, lib = tmp_path / "work", tmp_path / "lib"
    conn = open_db(work)
    old = lib / OLD_REL
    old.parent.mkdir(parents=True)
    old.write_bytes(b"MOVIEBYTES")
    if sidecar:
        Path(str(old) + ".xmp").write_text("<x/>", encoding="utf-8")
    conn.execute(
        "INSERT INTO files(source_path, rel_path, ext, kind, content_hash, date_taken,"
        " date_source, date_confidence, status, dest_path)"
        " VALUES (?,?,?,?,?,?,?,?,?,?)",
        (
            r"H:\backup\2018\IMG_0735.MOV",
            r"2018\IMG_0735.MOV",
            ".mov",
            "video",
            H,
            date,
            "exif",
            "high",
            "copied",
            str(old),
        ),
    )
    conn.commit()
    return work, lib, conn


def run_refile(work: Path, lib: Path, conn, dry_run: bool):
    run_id = new_run(conn, "refile", {})
    with open(work / "refile.jsonl", "w", encoding="utf-8") as fh:
        cmd_refile(
            conn, work, run_id, fh, SimpleNamespace(out=str(lib), dry_run=dry_run), Config()
        )


def test_dry_run_moves_nothing(tmp_path: Path, capsys):
    work, lib, conn = make_lib(tmp_path)
    run_refile(work, lib, conn, dry_run=True)
    out = capsys.readouterr().out
    assert "MOVE" in out and str(NEW_REL) in out
    assert (lib / OLD_REL).exists()
    assert not (lib / NEW_REL).exists()
    assert conn.execute("SELECT dest_path FROM files").fetchone()[0] == str(lib / OLD_REL)


def test_refile_moves_file_and_sidecar_and_updates_the_manifest(tmp_path: Path):
    work, lib, conn = make_lib(tmp_path)
    run_refile(work, lib, conn, dry_run=False)

    assert not (lib / OLD_REL).exists()
    assert (lib / NEW_REL).read_bytes() == b"MOVIEBYTES"
    assert Path(str(lib / NEW_REL) + ".xmp").read_text(encoding="utf-8") == "<x/>"
    assert conn.execute("SELECT dest_path FROM files").fetchone()[0] == str(lib / NEW_REL)
    act = conn.execute("SELECT action, detail FROM actions WHERE action='refiled'").fetchone()
    assert act is not None
    assert str(OLD_REL) in act["detail"] and str(NEW_REL) in act["detail"]


def test_refile_is_idempotent(tmp_path: Path, capsys):
    work, lib, conn = make_lib(tmp_path)
    run_refile(work, lib, conn, dry_run=False)
    capsys.readouterr()
    run_refile(work, lib, conn, dry_run=False)
    assert "0 moved" in capsys.readouterr().out


def test_occupied_target_aborts_the_whole_run(tmp_path: Path):
    work, lib, conn = make_lib(tmp_path)
    (lib / NEW_REL).parent.mkdir(parents=True)
    (lib / NEW_REL).write_bytes(b"SOMEONE ELSE")
    with pytest.raises(SystemExit) as e:
        run_refile(work, lib, conn, dry_run=False)
    assert e.value.code != 0
    assert (lib / OLD_REL).exists()  # nothing moved
    assert (lib / NEW_REL).read_bytes() == b"SOMEONE ELSE"
    assert conn.execute("SELECT dest_path FROM files").fetchone()[0] == str(lib / OLD_REL)


def test_missing_library_file_is_reported_not_fatal(tmp_path: Path, capsys):
    work, lib, conn = make_lib(tmp_path, sidecar=False)
    (lib / OLD_REL).unlink()
    run_refile(work, lib, conn, dry_run=False)
    out = capsys.readouterr().out
    assert "missing" in out.lower()
    assert conn.execute("SELECT dest_path FROM files").fetchone()[0] == str(lib / OLD_REL)


def test_refile_is_wired_into_the_cli(tmp_path: Path):
    work, lib = tmp_path / "work", tmp_path / "lib"
    lib.mkdir()
    out = pf(work, "refile", "--out", str(lib), "--dry-run").stdout
    assert "refile" in out.lower()
    assert q(work, "SELECT COUNT(*) c FROM files")[0]["c"] == 0
```

2. **Run them, expect failure.**
   ```
   uv run pytest -q tests/test_refile.py
   ```
   Expected: `ModuleNotFoundError: No module named 'photoflow.refile'` (collection error for the whole file).

3. **Implement `src/photoflow/refile.py`.**

```python
"""Refile command: move already-copied library files to their current dest_for() path.

Why this exists: `status='copied'` is durable and `dest_path` is never recomputed, so a
metadata fix (e.g. the video dates repaired by `scan --refresh-meta`) followed by `plan`
changes `date_taken` but leaves the file sitting in the folder its OLD date implied. `apply`
will not touch it - it only processes `planned`/`review` rows (apply.py:27). `refile` closes
that loop.

Sources are never touched: this moves files inside the library root only.

Immich / digiKam / backup note: a moved file looks like delete + add to any external indexer.
Rescan the external library after a refile run.
"""

from __future__ import annotations

import errno
import os
import shutil
import sys
from collections import Counter
from pathlib import Path

from photoflow.audit import log_action
from photoflow.naming import dest_for


def _move(src: Path, dst: Path) -> None:
    dst.parent.mkdir(parents=True, exist_ok=True)
    try:
        os.replace(src, dst)  # atomic within a volume, which is the expected case
    except OSError as e:
        if e.errno != errno.EXDEV:
            raise
        shutil.move(str(src), str(dst))


def cmd_refile(conn, workdir, run_id, log_fh, args, cfg):
    out_root = Path(args.out).expanduser().resolve()
    rows = conn.execute(
        "SELECT * FROM files WHERE status='copied' AND dest_path IS NOT NULL"
    ).fetchall()

    # ---- pass 1: collect every candidate move (nothing touches the disk yet)
    moves: list[tuple[int, Path, Path, str]] = []
    missing: list[str] = []
    for r in rows:
        old = Path(r["dest_path"])
        new = dest_for(r, out_root, cfg.slug_max)
        if old == new:
            continue
        if not old.exists():
            missing.append(str(old))
            continue
        reason = "folder changed" if old.parent != new.parent else "name changed"
        moves.append((r["id"], old, new, reason))

    # ---- pass 2: pre-flight. Refuse the WHOLE run on any collision; a partial refile with an
    # overwritten library file is unrecoverable, an aborted one costs nothing.
    vacated = {str(old).casefold() for _, old, _, _ in moves}
    claimed: dict[str, Path] = {}
    collisions: list[str] = []
    for _fid, old, new, _reason in moves:
        for a, b in ((old, new), (Path(str(old) + ".xmp"), Path(str(new) + ".xmp"))):
            if not a.exists():
                continue  # sidecar simply isn't there
            key = str(b).casefold()
            if key in claimed:
                collisions.append(f"{b}  <- both {claimed[key]} and {a}")
            elif b.exists() and key not in vacated:
                collisions.append(f"{b}  already exists (target of {a})")
            else:
                claimed[key] = a
    if collisions:
        print(f"refile aborted: {len(collisions)} destination collision(s), nothing moved:")
        for c in collisions[:50]:
            print(f"  {c}")
        if len(collisions) > 50:
            print(f"  ... and {len(collisions) - 50} more")
        sys.exit(1)

    by_reason = Counter(reason for _, _, _, reason in moves)
    summary = ", ".join(f"{k}: {v}" for k, v in sorted(by_reason.items())) or "none"
    if missing:
        print(f"refile: {len(missing)} copied file(s) missing from the library, skipped:")
        for m in missing[:10]:
            print(f"  missing: {m}")
        if len(missing) > 10:
            print(f"  ... and {len(missing) - 10} more")

    if args.dry_run:
        for _fid, old, new, _reason in moves:
            print(f"MOVE {old}  ->  {new}")
        print(f"refile dry-run: {len(moves)} would move ({summary}).")
        return

    # ---- pass 3: execute
    moved = sidecars = 0
    for fid, old, new, _reason in moves:
        _move(old, new)
        old_sc, new_sc = Path(str(old) + ".xmp"), Path(str(new) + ".xmp")
        if old_sc.exists():
            _move(old_sc, new_sc)
            sidecars += 1
        conn.execute("UPDATE files SET dest_path=? WHERE id=?", (str(new), fid))
        log_action(conn, log_fh, run_id, fid, "refiled", f"{old} -> {new}")
        moved += 1
        if moved % 500 == 0:
            conn.commit()
            print(f"  refiled {moved}/{len(moves)}...")
    conn.commit()
    print(
        f"refile complete: {moved} moved ({summary}), {sidecars} sidecars, "
        f"{len(missing)} missing. Rescan your external library (Immich/digiKam) afterwards."
    )
```

4. **Wire it into the CLI.** In `src/photoflow/cli.py`: add `from photoflow.refile import cmd_refile` to the import block (alphabetically after `photoflow.review`); add the parser after the `apply` block (~line 51):
```python
    p = sub.add_parser(
        "refile",
        help="move already-copied library files to the dest their current date implies",
    )
    p.add_argument("--out", required=True, help="output library root (same as apply --out)")
    p.add_argument("--dry-run", action="store_true")
```
and add `"refile": cmd_refile,` to the dispatch dict (lines 82–88).

5. **Run the refile tests, expect PASS.**
   ```
   uv run pytest -q tests/test_refile.py
   ```
   Expected: `7 passed`. If `test_dry_run_moves_nothing` fails on the expected name, print `dest_for(row, lib, 40)` for the fixture row and reconcile against `naming.py:15-33` — `date_source='exif'` with a non-midnight time yields `%Y%m%d_%H%M%S_<slug>_<hash8><ext>`.

6. **Run the whole suite, expect PASS.**
   ```
   uv run pytest -q
   ```
   Expected: 0 failed.

7. **Lint and format.**
   ```
   uv run ruff check src tests && uv run ruff format src tests
   ```

8. **Commit.**
   ```
   git add src/photoflow/refile.py src/photoflow/cli.py tests/test_refile.py
   git commit -m "feat(refile): move copied library files to their corrected dest, dry-run first"
   ```

---

## Owner runbook (after this lane lands — the owner runs it, not the implementer)

Not part of the implementation. Included so the reviewer can see the lane is complete end-to-end.

```
photoflow scan --refresh-meta --kind video          # re-read 4,580 video dates (H1/H2)
photoflow plan                                      # recomputes date_taken for copied rows too
photoflow refile --out J:\photos_org --dry-run      # inspect the move list
photoflow refile --out J:\photos_org                # execute; rescan Immich afterwards
photoflow scan H:\_photos_backup                    # picks up the 744 .crw + .iiq/.eip (A1)
photoflow plan && photoflow apply --out J:\photos_org
```

## Out of scope for Lane A

`apply` hardening (T5), sidecar copy policy (T6/T6b), README + HANDOFF edits (Lane B), the review-page keeper lock (T21), and everything under the `enrich/` subsystem. Do not touch those files; a conflicting edit there will collide with the parallel lane.
