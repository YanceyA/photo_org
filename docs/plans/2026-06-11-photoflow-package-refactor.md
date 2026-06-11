# photoflow Package Refactor Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Decompose the single-file `photoflow.py` prototype into a `src/photoflow/` package with full test coverage, without changing observable behavior.

**Architecture:** Phased extraction per HANDOFF.md §4 — scaffold first with the untouched reference file as the live implementation, then extract pure logic with tests that lock in observed behavior, then infrastructure, then the five commands, then a config file. The reference file (`_reference.py`) is the spec and is deleted only after the integration test passes against the new package and a parity check confirms identical output trees.

**Tech Stack:** Python 3.11+, uv, pytest, ruff, sqlite3 (stdlib), exiftool (external binary), Pillow/ImageHash/pillow-heif (optional runtime deps, required dev deps for tests).

**Key references:**
- `HANDOFF.md` §2 — non-negotiable invariants. Re-read before every phase.
- `HANDOFF.md` §5 — test strategy this plan implements.
- `src/photoflow/_reference.py` (after Task 1) — the behavioral spec. When this plan and that code disagree, the code wins.

**Decisions made in this plan (update HANDOFF.md §3 when executed):**
- `slugify`/`dest_for` go in a new pure module `naming.py` (not `apply.py`) so `test_naming.py` runs without importing copy/exiftool machinery. `apply.py` imports from it.
- Optional-dependency detection (`HAVE_PIL`, `HAVE_IMAGEHASH`, `HAVE_HEIF`) lives in `hashing.py`; other modules import the flags from there.

---

## Phase 0 — Scaffold

### Task 1: Initial commit of the prototype

The repo has no commits yet. Commit the inputs as-is first so every later step has a clean diff.

**Files:**
- Commit: `HANDOFF.md`, `README.md`, `photoflow.py`, `CLAUDE.md`, `docs/plans/2026-06-11-photoflow-package-refactor.md`

**Step 1: Create `.gitignore`**

```gitignore
__pycache__/
*.pyc
.venv/
photoflow_work/
.pytest_cache/
.ruff_cache/
dist/
```

**Step 2: Commit**

```bash
git add .gitignore HANDOFF.md README.md CLAUDE.md photoflow.py docs/
git commit -m "chore: import photoflow prototype, handoff doc, and refactor plan"
```

### Task 2: uv scaffold with reference implementation

**Files:**
- Create: `pyproject.toml`
- Create: `src/photoflow/__init__.py`
- Create: `src/photoflow/__main__.py`
- Move: `photoflow.py` → `src/photoflow/_reference.py` (byte-identical, `git mv`)

**Step 1: Move the reference file**

```bash
git mv photoflow.py src/photoflow/_reference.py
```

Do NOT edit `_reference.py` in any way for the rest of this plan (until its deletion in Task 17).

**Step 2: Write `pyproject.toml`**

```toml
[project]
name = "photoflow"
version = "0.1.0"
description = "Incremental, non-destructive photo library organizer"
requires-python = ">=3.11"
dependencies = []

[project.optional-dependencies]
images = ["Pillow", "ImageHash", "pillow-heif"]

[dependency-groups]
dev = ["pytest", "ruff", "Pillow", "ImageHash"]

[project.scripts]
photoflow = "photoflow.cli:main"

[build-system]
requires = ["hatchling"]
build-backend = "hatchling.build"

[tool.hatch.build.targets.wheel]
packages = ["src/photoflow"]

[tool.pytest.ini_options]
testpaths = ["tests"]
markers = [
    "exiftool: requires exiftool on PATH (skipped when absent)",
]

[tool.ruff]
line-length = 100
target-version = "py311"

[tool.ruff.lint]
select = ["E", "F", "I", "UP", "B"]
```

Note: `[project.scripts]` points at `photoflow.cli:main`, which doesn't exist until Phase 3. That's fine — don't use the `photoflow` console script until then; use `python -m photoflow`.

**Step 3: Write `src/photoflow/__init__.py`**

```python
__version__ = "0.1.0"
```

**Step 4: Write `src/photoflow/__main__.py`**

```python
from photoflow._reference import main

main()
```

**Step 5: Sync and smoke-test**

```bash
uv sync
uv run python -m photoflow --workdir %TEMP%\pf_smoke status
```

Expected: prints `by status:` / `by role:` / `by date source:` headers (empty manifest) and exits 0. On failure, debug before proceeding — this proves the entry point wiring.

**Step 6: Commit**

```bash
git add pyproject.toml uv.lock src/
git commit -m "feat: scaffold uv package; reference impl runs via python -m photoflow"
```

---

## Phase 1 — Pure logic + tests first

Order within each task: write the test file (asserting the reference's observed behavior), watch it fail on import, create the module by copying code **verbatim** from `_reference.py`, watch it pass. Never rewrite logic while moving it.

### Task 3: `dates.py`

**Files:**
- Create: `tests/test_dates.py`
- Create: `src/photoflow/dates.py`

**Step 1: Write the failing tests**

`tests/test_dates.py`:

```python
from datetime import datetime, timedelta

from photoflow.dates import (
    date_from_filename,
    parse_exif_date,
    resolve_date,
    year_from_folder,
)


class TestParseExifDate:
    def test_standard_exif(self):
        assert parse_exif_date("2015:07:14 10:30:00") == datetime(2015, 7, 14, 10, 30, 0)

    def test_t_separator(self):
        assert parse_exif_date("2015:07:14T10:30:00") == datetime(2015, 7, 14, 10, 30, 0)

    def test_timezone_suffix_ignored(self):
        # wild EXIF carries tz suffixes; regex grabs the leading match
        assert parse_exif_date("2015:07:14 10:30:00+02:00") == datetime(2015, 7, 14, 10, 30)

    def test_none_input(self):
        assert parse_exif_date(None) is None

    def test_garbage(self):
        assert parse_exif_date("not a date") is None

    def test_all_zeros(self):
        assert parse_exif_date("0000:00:00 00:00:00") is None

    def test_year_below_window(self):
        assert parse_exif_date("1989:01:01 00:00:00") is None

    def test_year_above_window(self):
        future = datetime.now().year + 2
        assert parse_exif_date(f"{future}:01:01 00:00:00") is None

    def test_invalid_calendar_date(self):
        assert parse_exif_date("2015:02:30 10:00:00") is None


class TestDateFromFilename:
    def test_compact_datetime(self):
        assert date_from_filename("IMG_20190304_101112.jpg") == datetime(2019, 3, 4, 10, 11, 12)

    def test_whatsapp(self):
        assert date_from_filename("IMG-20190304-WA0001.jpg") == datetime(2019, 3, 4)

    def test_dashed_date(self):
        assert date_from_filename("2019-03-04 party.jpg") == datetime(2019, 3, 4)

    def test_no_date(self):
        assert date_from_filename("beach.jpg") is None

    def test_bogus_year_rejected(self):
        assert date_from_filename("IMG_30190304_101112.jpg") is None

    def test_invalid_compact_falls_through(self):
        # compact match with invalid date must not raise
        assert date_from_filename("99999999_999999.jpg") is None


class TestYearFromFolder:
    def test_year_in_parent(self):
        assert year_from_folder("Old Laptop/Holiday 2015/beach.jpg") == 2015

    def test_nearest_folder_wins(self):
        assert year_from_folder("2010/Trip 2015/x.jpg") == 2015

    def test_filename_year_ignored(self):
        assert year_from_folder("stuff/IMG_2015.jpg") is None

    def test_out_of_window(self):
        assert year_from_folder("Archive 1980/x.jpg") is None


def _row(exif_date=None, source_path="C:/src/x.jpg", rel_path="", mtime=None):
    return {
        "exif_date": exif_date,
        "source_path": source_path,
        "rel_path": rel_path,
        "mtime": mtime,
    }


class TestResolveDate:
    def test_exif_wins(self):
        iso, src, conf = resolve_date(
            _row(exif_date="2015:07:14 10:30:00", source_path="C:/s/IMG_20190304_101112.jpg")
        )
        assert (iso, src, conf) == ("2015-07-14T10:30:00", "exif", "high")

    def test_filename_second(self):
        iso, src, conf = resolve_date(_row(source_path="C:/s/IMG_20190304_101112.jpg"))
        assert (iso, src, conf) == ("2019-03-04T10:11:12", "filename", "medium")

    def test_folder_third(self):
        iso, src, conf = resolve_date(_row(rel_path="Holiday 2015/no_meta.png"))
        assert (iso, src, conf) == ("2015-01-01T00:00:00", "folder", "low")

    def test_mtime_last(self):
        ts = datetime(2018, 6, 1, 12, 0, 0).timestamp()
        iso, src, conf = resolve_date(_row(mtime=ts))
        assert iso.startswith("2018-06-01T12:00:00")
        assert (src, conf) == ("mtime", "low")

    def test_nothing(self):
        assert resolve_date(_row()) == (None, "none", "none")

    def test_bogus_mtime_rejected(self):
        ts = (datetime.now() + timedelta(days=365 * 3)).timestamp()
        assert resolve_date(_row(mtime=ts)) == (None, "none", "none")
```

**Step 2: Run, verify failure**

Run: `uv run pytest tests/test_dates.py -v`
Expected: collection error, `ModuleNotFoundError: No module named 'photoflow.dates'`

**Step 3: Create `src/photoflow/dates.py`**

Copy verbatim from `_reference.py`: the five regexes (`EXIF_DATE_RE`, `FNAME_FULL_RE`, `FNAME_WA_RE`, `FNAME_DATE_RE`, `FOLDER_YEAR_RE`), `_valid`, `parse_exif_date`, `date_from_filename`, `year_from_folder`, `resolve_date` (reference lines ~272–334), plus the `MIN_YEAR, MAX_YEAR` constants (line ~69) and the needed imports (`re`, `datetime`, `Path`). Module docstring: one line. No behavior changes.

**Step 4: Run, verify pass**

Run: `uv run pytest tests/test_dates.py -v`
Expected: all PASS. If any test fails, check the test's expectation against `_reference.py` behavior by running the function in the reference interactively — fix the **test**, not the logic (the code is the spec).

**Step 5: Commit**

```bash
git add tests/test_dates.py src/photoflow/dates.py
git commit -m "feat: extract date resolution cascade into dates.py with unit tests"
```

### Task 4: `hashing.py` and `bktree.py`

**Files:**
- Create: `tests/test_hashing.py`
- Create: `src/photoflow/hashing.py`
- Create: `src/photoflow/bktree.py`

**Step 1: Write the failing tests**

`tests/test_hashing.py`:

```python
from pathlib import Path

from photoflow.bktree import BKTree
from photoflow.hashing import content_hash, hamming


class TestHamming:
    def test_identical(self):
        assert hamming(0xABCD, 0xABCD) == 0

    def test_one_bit(self):
        assert hamming(0b1000, 0b0000) == 1

    def test_many_bits(self):
        assert hamming(0xFFFF, 0x0000) == 16


class TestContentHash:
    def test_stable_and_distinct(self, tmp_path: Path):
        a = tmp_path / "a.bin"
        b = tmp_path / "b.bin"
        a.write_bytes(b"hello world" * 1000)
        b.write_bytes(b"hello world" * 1000 + b"!")
        ha = content_hash(a)
        assert ha == content_hash(a)          # deterministic
        assert ha != content_hash(b)          # content-sensitive
        assert len(ha) == 40                  # blake2b digest_size=20 -> 40 hex chars


class TestBKTree:
    def test_empty_query(self):
        assert BKTree().query(0, 5) == []

    def test_exact_member_found(self):
        t = BKTree()
        t.add(0b1010)
        assert t.query(0b1010, 0) == [0b1010]

    def test_radius_inclusion_and_exclusion(self):
        t = BKTree()
        for h in (0b0000, 0b0001, 0b0111, 0b1111111):
            t.add(h)
        hits = set(t.query(0b0000, 2))
        assert 0b0000 in hits and 0b0001 in hits
        assert 0b0111 not in hits          # distance 3 > radius 2
        assert 0b1111111 not in hits

    def test_duplicate_add_is_noop(self):
        t = BKTree()
        t.add(42)
        t.add(42)
        assert t.query(42, 0) == [42]
```

**Step 2: Run, verify failure**

Run: `uv run pytest tests/test_hashing.py -v`
Expected: `ModuleNotFoundError`

**Step 3: Create the modules**

`src/photoflow/bktree.py` — copy `hamming` + `BKTree` verbatim (reference ~227–268). `BKTree.query` calls `hamming`; keep them together here.

`src/photoflow/hashing.py` — copy the optional-dependency try/except block (`HAVE_PIL`, `HAVE_IMAGEHASH`, `HAVE_HEIF`, reference ~39–56) and `content_hash`, `perceptual_hash` (~209–224). Re-export `hamming` from bktree for convenience:

```python
from photoflow.bktree import hamming  # noqa: F401  (public re-export)
```

**Step 4: Run, verify pass**

Run: `uv run pytest tests/test_hashing.py -v`
Expected: all PASS

**Step 5: Commit**

```bash
git add tests/test_hashing.py src/photoflow/hashing.py src/photoflow/bktree.py
git commit -m "feat: extract hashing and BK-tree into modules with unit tests"
```

### Task 5: `naming.py`

**Files:**
- Create: `tests/test_naming.py`
- Create: `src/photoflow/naming.py`
- Modify: `HANDOFF.md` §3 (record the naming.py decision)

**Step 1: Write the failing tests**

`tests/test_naming.py`:

```python
from pathlib import Path

from photoflow.naming import dest_for, slugify


class TestSlugify:
    def test_spaces_and_punct(self):
        assert slugify("My Photo (1)!") == "My-Photo-1"

    def test_truncated_to_slug_max(self):
        assert len(slugify("a" * 100)) == 40

    def test_empty_falls_back(self):
        assert slugify("???") == "img"


def _row(content_hash="deadbeefcafe", source_path="C:/src/Beach Day.jpg",
         ext=".jpg", date_taken=None, date_source="none"):
    return {
        "content_hash": content_hash,
        "source_path": source_path,
        "ext": ext,
        "date_taken": date_taken,
        "date_source": date_source,
    }


class TestDestFor:
    OUT = Path("C:/lib")

    def test_exif_with_time(self):
        d = dest_for(_row(date_taken="2015-07-14T10:30:00", date_source="exif"), self.OUT)
        assert d == self.OUT / "2015" / "07" / "20150714_103000_Beach-Day_deadbeef.jpg"

    def test_filename_source_with_time(self):
        d = dest_for(_row(date_taken="2019-03-04T10:11:12", date_source="filename"), self.OUT)
        assert d.name == "20190304_101112_Beach-Day_deadbeef.jpg"

    def test_exif_midnight_gets_date_only_name(self):
        d = dest_for(_row(date_taken="2015-07-14T00:00:00", date_source="exif"), self.OUT)
        assert d.name == "20150714_Beach-Day_deadbeef.jpg"

    def test_low_confidence_source_gets_date_only_name(self):
        # folder/mtime sources never get an HHMMSS component
        d = dest_for(_row(date_taken="2018-06-01T12:00:00", date_source="mtime"), self.OUT)
        assert d.name == "20180601_Beach-Day_deadbeef.jpg"

    def test_dateless_goes_to_unknown(self):
        d = dest_for(_row(), self.OUT)
        assert d == self.OUT / "unknown-date" / "Beach-Day_deadbeef.jpg"

    def test_ext_lowercased(self):
        d = dest_for(_row(ext=".JPG"), self.OUT)
        assert d.suffix == ".jpg"
```

**Step 2: Run, verify failure**

Run: `uv run pytest tests/test_naming.py -v`
Expected: `ModuleNotFoundError`

**Step 3: Create `src/photoflow/naming.py`**

Copy `slugify` and `dest_for` verbatim (reference ~655–675) plus `SLUG_MAX = 40`. Imports: `re`, `datetime`, `Path`.

**Step 4: Run, verify pass**

Run: `uv run pytest tests/test_naming.py -v`
Expected: all PASS

**Step 5: Update HANDOFF.md §3**

Add `│   ├── naming.py             # slugify + dest_for (pure)` to the layout tree and remove `dest_for` from the `apply.py` comment.

**Step 6: Commit**

```bash
git add tests/test_naming.py src/photoflow/naming.py HANDOFF.md
git commit -m "feat: extract slugify/dest_for into naming.py with unit tests"
```

### Task 6: Phase 1 cleanup gate

**Step 1: Lint everything written so far**

Run: `uv run ruff check src tests && uv run ruff format src tests`
Expected: clean (fix anything it flags; `_reference.py` may be excluded — add to `pyproject.toml`:

```toml
[tool.ruff]
extend-exclude = ["src/photoflow/_reference.py"]
```

The reference file is frozen; never reformat it.)

**Step 2: Full test run**

Run: `uv run pytest -v`
Expected: all PASS

**Step 3: Commit**

```bash
git add -A
git commit -m "chore: ruff config excludes frozen reference; lint clean"
```

---

## Phase 2 — Infrastructure

### Task 7: `db.py` with schema_version

**Files:**
- Create: `tests/test_db.py`
- Create: `src/photoflow/db.py`

**Step 1: Write the failing tests**

`tests/test_db.py`:

```python
from pathlib import Path

from photoflow.db import SCHEMA_VERSION, new_run, open_db


def test_open_db_creates_tables(tmp_path: Path):
    conn = open_db(tmp_path)
    names = {r["name"] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'")}
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
```

**Step 2: Run, verify failure**

Run: `uv run pytest tests/test_db.py -v`
Expected: `ModuleNotFoundError`

**Step 3: Create `src/photoflow/db.py`**

Copy `SCHEMA`, `open_db`, `new_run` verbatim (reference ~90–150). Then add — this is the one sanctioned addition (HANDOFF §4 Phase 2):

```python
SCHEMA_VERSION = 1

SCHEMA += """
CREATE TABLE IF NOT EXISTS schema_version (version INTEGER NOT NULL);
"""
```

(Concatenate into the SCHEMA string directly rather than `+=` — shown here only to highlight the delta.) In `open_db`, after `executescript`, seed it once:

```python
    if conn.execute("SELECT COUNT(*) FROM schema_version").fetchone()[0] == 0:
        conn.execute("INSERT INTO schema_version(version) VALUES (?)", (SCHEMA_VERSION,))
        conn.commit()
```

**Step 4: Run, verify pass**

Run: `uv run pytest tests/test_db.py -v`
Expected: all PASS

**Step 5: Commit**

```bash
git add tests/test_db.py src/photoflow/db.py
git commit -m "feat: extract db module; add schema_version table for future migrations"
```

### Task 8: `audit.py`

**Files:**
- Create: `tests/test_audit.py`
- Create: `src/photoflow/audit.py`

**Step 1: Write the failing tests**

`tests/test_audit.py`:

```python
import io
import json
from pathlib import Path

from photoflow.audit import log_action
from photoflow.db import new_run, open_db


def test_log_action_writes_table_and_jsonl(tmp_path: Path):
    conn = open_db(tmp_path)
    run_id = new_run(conn, "scan", {})
    fh = io.StringIO()
    log_action(conn, fh, run_id, 7, "copied", "a -> b")
    conn.commit()

    row = conn.execute("SELECT * FROM actions").fetchone()
    assert (row["run_id"], row["file_id"], row["action"], row["detail"]) == \
        (run_id, 7, "copied", "a -> b")

    rec = json.loads(fh.getvalue())
    assert rec["action"] == "copied" and rec["file_id"] == 7 and rec["run"] == run_id
```

**Step 2: Run, verify failure**

Run: `uv run pytest tests/test_audit.py -v`
Expected: `ModuleNotFoundError`

**Step 3: Create `src/photoflow/audit.py`**

Copy `log_action` verbatim (reference ~153–158). Imports: `json`, `datetime`.

**Step 4: Run, verify pass; Step 5: Commit**

```bash
git add tests/test_audit.py src/photoflow/audit.py
git commit -m "feat: extract audit logging (actions table + JSONL)"
```

### Task 9: `exiftool.py` and `xmp.py`

**Files:**
- Create: `tests/test_xmp.py`
- Create: `src/photoflow/exiftool.py`
- Create: `src/photoflow/xmp.py`
- Create: `tests/conftest.py` (exiftool marker plumbing only, for now)

**Step 1: Write the failing tests**

`tests/conftest.py` (initial version — fixture builder comes in Task 15):

```python
import shutil

import pytest


def pytest_collection_modifyitems(config, items):
    if shutil.which("exiftool"):
        return
    skip = pytest.mark.skip(reason="exiftool not on PATH")
    for item in items:
        if "exiftool" in item.keywords:
            item.add_marker(skip)
```

`tests/test_xmp.py`:

```python
import shutil
import subprocess
from pathlib import Path

import pytest

from photoflow.xmp import xmp_sidecar


def test_sidecar_path_and_content(tmp_path: Path):
    dest = tmp_path / "photo.dng"
    dest.touch()
    xmp_sidecar(dest, "photoflow src: A/b.jpg | C/d.jpg", ["Holiday 2015", "A&B"])
    sc = tmp_path / "photo.dng.xmp"
    assert sc.exists()
    text = sc.read_text(encoding="utf-8")
    assert "photoflow src: A/b.jpg | C/d.jpg" in text
    assert "<rdf:li>Holiday 2015</rdf:li>" in text
    assert "A&amp;B" in text  # html-escaped


@pytest.mark.exiftool
def test_sidecar_parses_with_exiftool(tmp_path: Path):
    dest = tmp_path / "photo.dng"
    dest.touch()
    xmp_sidecar(dest, "desc here", ["kw1"])
    out = subprocess.run(
        ["exiftool", "-j", "-XMP-dc:Description", "-XMP-dc:Subject",
         str(tmp_path / "photo.dng.xmp")],
        capture_output=True, text=True, check=True)
    assert "desc here" in out.stdout and "kw1" in out.stdout
```

**Step 2: Run, verify failure**

Run: `uv run pytest tests/test_xmp.py -v`
Expected: `ModuleNotFoundError`

**Step 3: Create the modules**

`src/photoflow/xmp.py` — copy `xmp_sidecar` verbatim (reference ~678–690). Also move the embed-args construction here as a pure builder so apply.py stays thin (this is the "embed-arg builder" from HANDOFF §3):

```python
EMBED_EXT = {".jpg", ".jpeg", ".tif", ".tiff", ".png", ".heic", ".heif"}


def embed_args(dest: str, description: str, keywords: list[str]) -> list[str]:
    """exiftool argfile lines to embed Dublin Core XMP into one file."""
    lines = ["-overwrite_original", f"-XMP-dc:Description={description}"]
    lines += [f"-XMP-dc:Subject={k}" for k in keywords]
    lines += [dest, "-execute"]
    return lines
```

(These exact strings come from reference ~751–754; assemble, don't invent.)

`src/photoflow/exiftool.py` — copy `exiftool_available`, `exiftool_json`, `exiftool_apply_argfile` verbatim (reference ~162–205) plus `EXIFTOOL_BATCH = 200` and `EXIF_TAGS` (reference ~71–74). Add the merge helper extracted from `cmd_apply` (reference ~780–782):

```python
def merge_metadata(donor_path: str, keeper_path: str) -> None:
    """Fill keeper's missing tags from donor. -wm cg = create-only, never overwrite."""
    subprocess.run(["exiftool", "-overwrite_original", "-wm", "cg",
                    "-tagsfromfile", donor_path, "-all:all", keeper_path],
                   capture_output=True)
```

**Step 4: Run, verify pass**

Run: `uv run pytest tests/test_xmp.py -v`
Expected: PASS (second test skips if exiftool absent)

**Step 5: Commit**

```bash
git add tests/conftest.py tests/test_xmp.py src/photoflow/exiftool.py src/photoflow/xmp.py
git commit -m "feat: extract exiftool wrapper and XMP builders"
```

### Task 10: `models.py` and `config.py` (constants only)

**Files:**
- Create: `src/photoflow/models.py`
- Create: `src/photoflow/config.py`
- Test: covered by existing suites (classify via planner tests later); add a micro-test here

**Step 1: Write the failing test** (append to `tests/test_naming.py` or new `tests/test_models.py` — use new file)

`tests/test_models.py`:

```python
from photoflow.models import classify


def test_classify():
    assert classify(".jpg") == "image"
    assert classify(".cr2") == "raw"
    assert classify(".mp4") == "video"
    assert classify(".xmp") == "sidecar"
    assert classify(".txt") == "other"
```

**Step 2: Run, verify failure** — `uv run pytest tests/test_models.py -v` → `ModuleNotFoundError`

**Step 3: Create the modules**

`src/photoflow/config.py` — copy the constant block verbatim (reference ~59–74): `IMAGE_EXT`, `RAW_EXT`, `VIDEO_EXT`, `SIDECAR_EXT`, `NEAR_DUPE_THRESHOLD`, `BURST_WINDOW_S`, `MIN_YEAR`, `MAX_YEAR`, `SLUG_MAX`, `EXIFTOOL_BATCH`. The TOML loader comes in Phase 4 — constants only for now. Then point the earlier extractions at it: `dates.py`, `naming.py`, `exiftool.py` import their constants from `config` instead of holding copies (delete the local copies).

`src/photoflow/models.py` — copy `classify` verbatim (reference ~77–86), importing the ext sets from `config`. Define the vocabulary as plain frozensets of strings (the DB stores strings; full enums are YAGNI until something needs them):

```python
ROLES = frozenset({"keep", "exact_dupe", "raw_jpeg_pair", "live_pair", "burst", "review"})
DURABLE_STATUSES = frozenset({"copied", "error", "skipped_manual"})  # HANDOFF §2.4
```

**Step 4: Run the FULL suite** (config imports touched three modules)

Run: `uv run pytest -v`
Expected: all PASS

**Step 5: Commit**

```bash
git add -A
git commit -m "feat: centralize constants in config.py; add models.classify"
```

---

## Phase 3 — Split the commands

The five `cmd_*` functions move into their own modules. These are copy-extractions with import rewiring only — resist any urge to refactor logic while moving. Each command module exposes the same signature `(conn, workdir, run_id, log_fh, args)`.

### Task 11: `scan.py`

**Files:**
- Create: `src/photoflow/scan.py` — copy `cmd_scan` verbatim (reference ~338–427); imports from `config`, `models`, `hashing`, `exiftool`, `audit`.

**Step 1: Create the module. Step 2: Verify imports**

Run: `uv run python -c "from photoflow.scan import cmd_scan"`
Expected: no output, exit 0

**Step 3: Commit**

```bash
git add src/photoflow/scan.py
git commit -m "feat: extract scan command"
```

(Real verification is the integration test in Task 15 — these modules are exercised end-to-end there. Unit-testing `cmd_scan` in isolation would just mock everything it does.)

### Task 12: `planner.py`, `review.py`, `apply.py`, `status`

**Files:**
- Create: `src/photoflow/planner.py` — copy `cmd_plan` verbatim (reference ~431–572); imports `bktree.BKTree`, `dates.parse_exif_date/resolve_date`, `config.NEAR_DUPE_THRESHOLD/BURST_WINDOW_S`, `hashing.HAVE_IMAGEHASH`, `audit.log_action`.
- Create: `src/photoflow/review.py` — copy `cmd_review` verbatim (reference ~576–651); imports `hashing.HAVE_PIL`, `audit`.
- Create: `src/photoflow/apply.py` — copy `cmd_apply` verbatim (reference ~693–789), replacing the inline `embed_kinds_ext` set with `xmp.EMBED_EXT`, the inline xmp_args construction with `xmp.embed_args(...)`, the inline merge subprocess with `exiftool.merge_metadata(...)`, and `dest_for` with the `naming` import. Behavior must be byte-identical; only call sites change shape.
- Create: `src/photoflow/status.py` — copy `cmd_status` verbatim (reference ~793–804).

**Step 1: Create all four modules. Step 2: Verify imports**

Run: `uv run python -c "from photoflow import planner, review, apply, status"`
Expected: exit 0

**Step 3: Run full suite** — `uv run pytest -v` → all PASS

**Step 4: Commit**

```bash
git add src/photoflow/planner.py src/photoflow/review.py src/photoflow/apply.py src/photoflow/status.py
git commit -m "feat: extract plan/review/apply/status commands"
```

### Task 13: `cli.py` dispatcher

**Files:**
- Create: `src/photoflow/cli.py` — copy `main` verbatim (reference ~808–837), dispatching to the new modules:

```python
from photoflow.apply import cmd_apply
from photoflow.planner import cmd_plan
from photoflow.review import cmd_review
from photoflow.scan import cmd_scan
from photoflow.status import cmd_status
```

Same argparse tree: `--workdir` global; `scan sources+`; `plan`; `review`; `apply --out (required) --decisions --dry-run`; `status`. Run/log setup stays in `main` (it is the composition root).

- Modify: `src/photoflow/__main__.py`:

```python
from photoflow.cli import main

main()
```

**Step 1: Make the changes. Step 2: Smoke test**

```bash
uv run python -m photoflow --workdir %TEMP%\pf_smoke2 status
```

Expected: same three-section summary as the Phase 0 smoke test.

**Step 3: Commit**

```bash
git add src/photoflow/cli.py src/photoflow/__main__.py
git commit -m "feat: cli.py thin dispatcher; python -m photoflow now runs the package"
```

### Task 14: Synthetic fixture builder

**Files:**
- Modify: `tests/conftest.py`

**Step 1: Add the fixture builder** (HANDOFF §5 — every item matters; comments say why)

Append to `tests/conftest.py`:

```python
import os
import shutil as _shutil
import subprocess
from pathlib import Path

from PIL import Image


def _gradient(w: int, h: int, seed: int) -> Image.Image:
    """Low-frequency image so pHash is stable across resizes; seed varies the pattern."""
    img = Image.new("RGB", (w, h))
    px = img.load()
    for x in range(w):
        for y in range(h):
            px[x, y] = ((x * (seed + 3)) % 256, (y * (seed + 7)) % 256,
                        ((x + y) * (seed + 11)) % 256)
    return img


def _set_exif(path: Path, **tags: str) -> None:
    args = ["exiftool", "-overwrite_original"]
    args += [f"-{k}={v}" for k, v in tags.items()]
    subprocess.run(args + [str(path)], capture_output=True, check=True)


@pytest.fixture
def photo_fixture(tmp_path: Path) -> Path:
    """Synthetic source tree per HANDOFF §5. Requires exiftool (mark tests exiftool)."""
    src = tmp_path / "sources"
    old = src / "Old Laptop" / "Holiday 2015"
    rnd = src / "Random"
    phone = src / "Phone Backup" / "Camera"
    for d in (old, rnd, phone):
        d.mkdir(parents=True)

    # beach.jpg: EXIF date + camera model
    beach = old / "beach.jpg"
    _gradient(640, 480, seed=1).save(beach, "JPEG", quality=92)
    _set_exif(beach, DateTimeOriginal="2015:07:14 10:30:00", Model="Canon EOS 70D")

    # exact dupe of beach, different folder
    _shutil.copy2(beach, rnd / "beach copy.jpg")

    # filename-date path (no EXIF)
    _gradient(640, 480, seed=2).save(phone / "IMG_20190304_101112.jpg", "JPEG", quality=92)

    # near-dupe pair: same scene, downscaled re-encode -> review queue
    big = _gradient(1000, 750, seed=3)
    big.save(rnd / "sunset_big.jpg", "JPEG", quality=92)
    big.resize((400, 300)).save(rnd / "sunset_small.jpg", "JPEG", quality=70)

    # RAW+JPEG pair: same stem; .dng must NOT be byte-identical (HANDOFF §7)
    mountain = old / "mountain.jpg"
    _gradient(640, 480, seed=4).save(mountain, "JPEG", quality=92)
    _shutil.copy2(mountain, old / "mountain.dng")
    with open(old / "mountain.dng", "ab") as f:
        f.write(b"\x00RAWPAYLOAD")

    # folder-year date path
    _gradient(320, 240, seed=5).save(old / "no_meta.png", "PNG")

    # burst trio: same Model, DateTimeOriginal 2s apart, near-identical pixels
    for i in range(3):
        p = old / f"burst_{i}.jpg"
        img = _gradient(640, 480, seed=6)
        px = img.load()
        px[i, 0] = (255, 0, 0)  # not byte-identical, pHash-identical
        img.save(p, "JPEG", quality=92)
        _set_exif(p, DateTimeOriginal=f"2015:07:14 12:00:{i * 2:02d}",
                  Model="Canon EOS 70D")

    return src
```

**Step 2: Sanity-check the fixture builds**

Run: `uv run pytest --co -q` (collects; no fixture errors)
Then a quick throwaway check that the near-dupe pair actually lands within pHash distance 5:

```bash
uv run python -c "import imagehash, sys; from PIL import Image; print('write a tiny script using the fixture gradient code to confirm phash(sunset_big) - phash(sunset_small) <= 5')"
```

Do this properly: write a 10-line scratch script reproducing `_gradient(1000,750,3)` vs its 400x300 resize and assert the imagehash distance ≤ 5 and that seeds 1–6 are mutually > 5 apart. If a seed pair collides, change the seed. **Delete the scratch script after.** This pre-check avoids debugging fixture physics through integration-test failures.

**Step 3: Commit**

```bash
git add tests/conftest.py
git commit -m "test: synthetic photo fixture builder per HANDOFF §5"
```

### Task 15: Integration test

**Files:**
- Create: `tests/test_pipeline.py`

**Step 1: Write the integration test** (this is the §5 spec, including the §7 regression round)

```python
import csv
import shutil
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

pytestmark = pytest.mark.exiftool


def pf(workdir: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, "-m", "photoflow", "--workdir", str(workdir), *args],
        capture_output=True, text=True, check=True)


def q(workdir: Path, sql: str, *params):
    conn = sqlite3.connect(workdir / "photoflow.db")
    conn.row_factory = sqlite3.Row
    try:
        return conn.execute(sql, params).fetchall()
    finally:
        conn.close()


def tree(root: Path) -> set[str]:
    return {str(p.relative_to(root)) for p in root.rglob("*") if p.is_file()}


def test_full_pipeline(photo_fixture: Path, tmp_path: Path):
    work = tmp_path / "work"
    lib = tmp_path / "library"

    # ---- 1. scan + plan
    pf(work, "scan", str(photo_fixture))
    pf(work, "plan")

    roles = {r["role"]: r["c"] for r in q(
        work, "SELECT role, COUNT(*) c FROM files GROUP BY role")}
    assert roles.get("exact_dupe") == 1          # beach copy.jpg
    assert roles.get("raw_jpeg_pair") == 2       # mountain.jpg + mountain.dng
    assert roles.get("burst") == 3               # burst trio kept silently
    assert roles.get("review") == 2              # sunset big + small

    date_sources = {r["s"]: r["c"] for r in q(
        work, "SELECT date_source s, COUNT(*) c FROM files GROUP BY date_source")}
    assert date_sources.get("exif") >= 4         # beach + dupe + 3 bursts... (dupe also exif)
    assert date_sources.get("filename") == 1     # IMG_20190304_101112.jpg
    assert date_sources.get("folder") >= 1       # no_meta.png

    # ---- 2. review -> fill decisions -> apply
    pf(work, "review")
    dec = work / "decisions.csv"
    rows = list(csv.DictReader(open(dec, newline="", encoding="utf-8")))
    big = next(r for r in rows if "sunset_big" in r["source_path"])
    small = next(r for r in rows if "sunset_small" in r["source_path"])
    for r in rows:
        if r is big:
            r["decision"], r["merge_from_file_id"] = "keep", small["file_id"]
        elif r is small:
            r["decision"] = "skip"
    with open(dec, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=rows[0].keys())
        w.writeheader()
        w.writerows(rows)

    pf(work, "apply", "--out", str(lib))

    files = tree(lib)
    beach_dest = [f for f in files if "beach" in f]
    assert len(beach_dest) == 1
    assert beach_dest[0].startswith("2015\\07\\20150714_103000_")  # exif date+time naming
    assert any("unknown-date" not in f and "no-meta" in f.replace("_", "-")
               or "no_meta" in f for f in files)  # folder-dated PNG landed in 2015/
    assert any(f.endswith(".dng.xmp") for f in files)              # RAW sidecar
    assert not any("sunset_small" in f for f in files)

    # XMP description on beach keeper carries BOTH source rel-paths
    beach_path = lib / beach_dest[0]
    out = subprocess.run(["exiftool", "-j", "-XMP-dc:Description", str(beach_path)],
                         capture_output=True, text=True, check=True).stdout
    assert "Holiday 2015" in out or "beach.jpg" in out
    assert "beach copy.jpg" in out

    statuses = {r["status"]: r["c"] for r in q(
        work, "SELECT status, COUNT(*) c FROM files GROUP BY status")}
    assert statuses.get("skipped_manual") == 1
    assert statuses.get("skipped_dupe") == 1

    # ---- 3. incremental regression round (HANDOFF §7 bug)
    inc = tmp_path / "usb_stick"
    inc.mkdir()
    shutil.copy2(photo_fixture / "Old Laptop" / "Holiday 2015" / "beach.jpg",
                 inc / "beach again.jpg")
    from conftest import _gradient
    _gradient(640, 480, seed=9).save(inc / "brand_new.jpg", "JPEG", quality=92)

    before = tree(lib)
    pf(work, "scan", str(inc))
    pf(work, "plan")
    pf(work, "apply", "--out", str(lib))
    after = tree(lib)

    new_files = after - before
    assert len(new_files) == 1                                # exactly ONE new copy
    assert "brand_new" in next(iter(new_files))
    small_status = q(work, "SELECT status FROM files WHERE source_path LIKE ?",
                     "%sunset_small%")[0]["status"]
    assert small_status == "skipped_manual"                   # decision survived re-plan
    big_rows = q(work, "SELECT status, role FROM files WHERE source_path LIKE ?",
                 "%sunset_big%")
    assert big_rows[0]["status"] == "copied"                  # not re-copied, not re-queued

    # ---- 4. review regeneration keeps decisions
    pf(work, "review")
    rows2 = list(csv.DictReader(open(dec, newline="", encoding="utf-8")))
    kept = {r["file_id"]: r["decision"] for r in rows2 if r["decision"]}
    assert kept.get(big["file_id"]) == "keep"
    assert kept.get(small["file_id"]) == "skip"

    # ---- 5. dry-run changes nothing
    snapshot = tree(lib)
    pf(work, "apply", "--out", str(lib), "--dry-run")
    assert tree(lib) == snapshot
```

Note on the `roles.get("review") == 2` assertion: it runs *before* apply, immediately after the first plan. Note on step 3's status checks: re-plan recomputes roles for ALL files including copied ones (invariant §2.4) — sunset_big's role may legitimately be `review` again, but its status must remain `copied` and it must not be re-copied. The assertions above check exactly that and nothing stricter.

**Step 2: Run it**

Run: `uv run pytest tests/test_pipeline.py -v`
Expected: PASS. This will likely take a few iterations — fixture pHash distances and Windows path separators (`2015\\07\\...`) are the usual suspects. When an assertion fails, FIRST verify the expectation against the reference implementation's actual behavior (run the same fixture through `python src/photoflow/_reference.py` in a scratch dir) before touching package code. If reference and package disagree → package extraction bug. If reference matches package but not the test → test expectation bug.

**Step 3: Commit**

```bash
git add tests/test_pipeline.py
git commit -m "test: end-to-end integration test incl. incremental regression round"
```

### Task 16: `test_planner.py` and `test_review.py` unit-level coverage

**Files:**
- Create: `tests/test_planner.py`
- Create: `tests/test_review.py`

**Step 1: Write the tests.** These reuse `photo_fixture` but assert at DB level after `scan`+`plan` only (no apply):

`tests/test_planner.py` (all `@pytest.mark.exiftool`):
- `test_keeper_prefers_already_copied`: scan+plan+apply the fixture (decisions: keep big/skip small as in Task 15), then scan a new dir containing a byte-copy of beach.jpg, re-plan, and assert the keeper of the beach group is the row with `status='copied'` and the new copy has `role='exact_dupe'` with `dupe_of` pointing at the copied keeper's id.
- `test_keeper_prefers_earliest_mtime`: two byte-identical files, neither copied, different mtimes (use `os.utime` to set them) → keeper is the earlier one.
- `test_plan_is_idempotent`: run plan twice; assert identical `(role, group_id IS NULL, dupe_of)` multisets and identical status counts both times.
- `test_live_pair_video_inherits_date`: add `clip.jpg` (with EXIF date) + `clip.mp4` (no date) same stem to a scratch source; after plan, the mp4 row has `role='live_pair'` and `date_source='exif'`.

`tests/test_review.py` (`@pytest.mark.exiftool`):
- `test_carry_forward`: scan+plan+review, write a decision into one row of decisions.csv, run review again, assert the decision is still there (this duplicates integration step 4 at a smaller scale — keep it; it pins the unit).
- `test_blank_decisions_stay_blank`: untouched rows remain empty after regeneration.

Write each as straightforward subprocess-driven tests using the `pf`/`q` helpers — move those two helpers from `test_pipeline.py` into `conftest.py` so all three files share them.

**Step 2: Run** — `uv run pytest tests/test_planner.py tests/test_review.py -v` → PASS

**Step 3: Commit**

```bash
git add tests/test_planner.py tests/test_review.py tests/conftest.py tests/test_pipeline.py
git commit -m "test: planner keeper-preference/idempotence and review carry-forward units"
```

### Task 17: Parity check, then delete the reference

**Files:**
- Delete: `src/photoflow/_reference.py`

**Step 1: Parity run (HANDOFF §9 acceptance criterion)**

Write a throwaway script `scratch_parity.py` (repo root, never committed):

```python
"""Run reference vs package against the same fixture; diff trees + hashes."""
import hashlib, subprocess, sys, tempfile
from pathlib import Path
sys.path.insert(0, "tests")
from conftest import _gradient, _set_exif  # reuse fixture builder pieces
# ... build the same fixture twice (two tmp dirs), then:
#   ref:  python -c "import photoflow._reference as r; ..."  via subprocess with --workdir w1
#   pkg:  python -m photoflow ... --workdir w2
# scan/plan/apply (no review groups decided -> held files excluded from both)
# assert: identical relative dest trees, identical file content hashes,
#         identical role/status GROUP BY counts in both DBs.
```

Flesh it out at execution time (≈60 lines). Reference is invoked as `uv run python src/photoflow/_reference.py <cmd> ...`. Expected: prints `PARITY OK`.

**Step 2: Delete reference + scratch**

```bash
git rm src/photoflow/_reference.py
del scratch_parity.py
```

Remove the `extend-exclude` for `_reference.py` from `pyproject.toml`.

**Step 3: Full suite + lint**

Run: `uv run pytest -v && uv run ruff check src tests && uv run ruff format --check src tests`
Expected: all PASS, lint clean

**Step 4: Commit**

```bash
git add -A
git commit -m "feat: remove reference implementation after parity check passes"
```

---

## Phase 4 — Config file

### Task 18: `photoflow.toml` loader

**Files:**
- Create: `tests/test_config.py`
- Modify: `src/photoflow/config.py`
- Modify: `src/photoflow/cli.py` (load config from workdir, thread through)
- Modify: consumers (`dates`, `naming`, `planner`, `exiftool`, `scan`) to read from a `Config` instance

**Step 1: Write the failing tests**

`tests/test_config.py`:

```python
from pathlib import Path

from photoflow.config import Config, load_config


def test_defaults_match_legacy_constants():
    c = Config()
    assert c.near_dupe_threshold == 5
    assert c.burst_window_s == 10
    assert c.min_year == 1990
    assert c.slug_max == 40
    assert c.exiftool_batch == 200
    assert ".jpg" in c.image_ext and ".cr2" in c.raw_ext and ".mp4" in c.video_ext


def test_load_missing_file_gives_defaults(tmp_path: Path):
    assert load_config(tmp_path) == Config()


def test_toml_overrides(tmp_path: Path):
    (tmp_path / "photoflow.toml").write_text(
        "near_dupe_threshold = 8\nburst_window_s = 4\n", encoding="utf-8")
    c = load_config(tmp_path)
    assert c.near_dupe_threshold == 8
    assert c.burst_window_s == 4
    assert c.min_year == 1990  # untouched keys keep defaults


def test_unknown_key_rejected(tmp_path: Path):
    (tmp_path / "photoflow.toml").write_text("near_dup_threshold = 8\n", encoding="utf-8")
    import pytest
    with pytest.raises(SystemExit):
        load_config(tmp_path)  # typo'd key must not be silently ignored
```

**Step 2: Run, verify failure. Step 3: Implement**

`config.py` becomes a frozen dataclass + `tomllib` loader; module-level constants remain as the dataclass field defaults (single source of truth). `load_config(workdir)` reads `workdir/photoflow.toml` if present, errors on unknown keys with a `sys.exit` naming the bad key. `cli.py` calls `load_config` once and passes the instance into each command (extend the command signature to `(conn, workdir, run_id, log_fh, args, cfg)`). Consumers swap module-constant reads for `cfg.<field>`. `MAX_YEAR` stays derived (`datetime.now().year + 1`), not configurable.

Keep this surface exactly per HANDOFF §4 Phase 4: `near_dupe_threshold`, `burst_window_s`, `min_year`, `slug_max`, `exiftool_batch`, extension sets. CLI flags (when added later) override file values — for now no new CLI flags exist, so nothing to wire.

**Step 4: Full suite** — `uv run pytest -v` → all PASS (integration test confirms behavior unchanged with defaults)

**Step 5: Commit**

```bash
git add -A
git commit -m "feat: photoflow.toml config with defaults equal to legacy constants"
```

---

## Phase 5 — Tooling polish

### Task 19: justfile + pre-commit

**Files:**
- Create: `justfile`

```just
test:
    uv run pytest

lint:
    uv run ruff check src tests

fmt:
    uv run ruff format src tests

run *ARGS:
    uv run python -m photoflow {{ARGS}}
```

- Create: `.pre-commit-config.yaml`

```yaml
repos:
  - repo: https://github.com/astral-sh/ruff-pre-commit
    rev: v0.8.4
    hooks:
      - id: ruff
        args: [--fix]
      - id: ruff-format
```

(Check for the current rev at execution time: `pre-commit autoupdate` after install.)

**Step 1: Create files. Step 2: Verify** — `uv run pre-commit run --all-files` (add `pre-commit` to dev group) → clean. **Step 3: Commit.**

```bash
git add justfile .pre-commit-config.yaml pyproject.toml uv.lock
git commit -m "chore: justfile + pre-commit (ruff)"
```

### Task 20: CI (optional per HANDOFF)

**Files:**
- Create: `.github/workflows/ci.yml`

```yaml
name: ci
on: [push, pull_request]
jobs:
  test:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: astral-sh/setup-uv@v5
      - run: sudo apt-get update && sudo apt-get install -y libimage-exiftool-perl
      - run: uv sync
      - run: uv run ruff check src tests
      - run: uv run ruff format --check src tests
      - run: uv run pytest -v
```

**Commit:** `git add .github && git commit -m "ci: uv + ruff + pytest on push"`

---

## Acceptance checklist (HANDOFF §9 — verify before calling this done)

- [ ] `python -m photoflow <cmd>` exposes scan/plan/review/apply/status with the same flags and same workdir artifacts (db, logs/, thumbs/, review.html, decisions.csv)
- [ ] Integration test passes, including the incremental regression round (Task 15 step 3)
- [ ] Parity check passed before `_reference.py` was deleted (Task 17)
- [ ] `ruff check` and `ruff format --check` clean
- [ ] Pure-logic test files (`test_dates`, `test_hashing`, `test_naming`, `test_models`, `test_config`) pass with exiftool absent from PATH (verify: temporarily rename exiftool or check the marker skips)
- [ ] HANDOFF.md updated with decisions made (naming.py, anything else that came up)
- [ ] Backlog (HANDOFF §8) untouched — explicitly out of scope for this plan
