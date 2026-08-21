# Lane C — enrich correctness Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans (or subagent-driven-development) to implement this plan task-by-task.

**Goal:** Make the optional `enrich` subsystem correct and safe to re-run: core commands stop
hard-requiring numpy; `enrich apply` becomes incremental, mtime-preserving, failure-aware and
genuinely read-only in `--dry-run`; foreign `HierarchicalSubject` / `PersonInImage` values written by
digiKam/Lightroom survive an apply; `enrich merge` actually removes the stale name from the library
files; `enrich scan` survives a per-file model crash and reports progress; the tag blacklist becomes
durable DB state instead of a localStorage-only decision that silently reverts.

**Architecture:** photoflow is a package under `src/photoflow/`. Every command module shares the
signature `(conn, workdir, run_id, log_fh, args, cfg)` and is dispatched from `cli.py`. Enrich lives
in `src/photoflow/enrich/` with the same convention: pure/CI-testable modules (`regions.py`,
`page.py`, `clustering.py`) hold all logic that can be tested without models or exiftool; the command
modules (`scan/cluster/assign/merge/review/apply/status.py`) do the DB + subprocess work.
State is SQLite (`photoflow_work/photoflow.db`); schema lives in `db.py` (`SCHEMA` for fresh DBs,
`_migrate()` for additive `ALTER TABLE` on existing ones). All library writes go through exiftool
argfiles built by pure functions (`regions.py`, `xmp.py`) and executed by `exiftool.py`.

**Tech Stack:** Python 3.11+ (`target-version = "py311"`), sqlite3, argparse, exiftool (external
binary, hard requirement for the write paths), pytest, ruff (line-length 100; `src/photoflow/xmp.py`
and `src/photoflow/enrich/page.py` carry an `E501` per-file ignore), uv for running everything.
Optional model stack (`insightface`, `torch`, `open_clip`, `scikit-learn`, `numpy`) behind
`pip install -e .[enrich]`.

---

## Constraints — read before touching anything

1. **Work from a git worktree off `feature/enrich`.** Do not commit to `master`.
2. **NEVER run photoflow against the repo's `photoflow_work/` directory, and never read/write files
   under it.** It holds the owner's live 153k-row manifest and an `enrich scan` is currently running
   against it. A new `ALTER TABLE` there would also risk `database is locked`. Every test uses a
   `tmp_path` workdir. There is no step in this plan that needs the real DB.
3. **One commit per task.** Full `uv run pytest` must be green *before* each commit — not just the
   file you touched. On this machine that is ~156 tests / ~2m40s with exiftool on PATH.
4. `uv run ruff check src tests && uv run ruff format src tests` before every commit.
5. Tests needing the exiftool binary get `@pytest.mark.exiftool`; tests needing the model stack get
   `@pytest.mark.enrich` (both auto-skip via `tests/conftest.py:36-47`). CI has neither — every new
   test that is *not* marked must pass with only numpy + scikit-learn + Pillow present.
6. **No binary assets.** Fixtures are generated with Pillow (`tests/conftest.py:50-62`).
7. Preserve the HANDOFF.md §2 invariants. Nothing in this lane touches source files or the
   scan/plan/apply core pipeline; enrich only ever writes into the *already-copied* library.

### Shared-file note (lane coordination)

The coordinator merges lanes **A → B → C**.

* `src/photoflow/db.py` `_migrate()` is also edited by Lane A (adds `files.meta_read`). Keep your
  edit a separate, self-contained `if "<col>" not in cols:` block appended to the function — never
  reflow or reorder the existing blocks.
* `src/photoflow/exiftool.py` is edited by Lanes A and B. Lane B changes
  `exiftool_apply_argfile()` to return a result object. **This plan assumes that return type is
  `ExiftoolResult(returncode: int, stdout: str, stderr: str)`.** In Task C2, step 3, check whether
  Lane B has already landed it (`grep -n "ExiftoolResult" src/photoflow/exiftool.py`); if it has,
  reuse it verbatim and skip creating it; if it has not, create it exactly as written here so the
  merge is a no-op. Do not change `exiftool_json()` or `merge_metadata()`.
* `src/photoflow/enrich/page.py` has the `E501` exemption — long HTML/JS template lines are
  intentional; do not reformat the template.

---

### Task C1: enrich imports must not break core commands (T22 / R4)

**Files:**
* `src/photoflow/cli.py:1-31` (module imports + `ENRICH_COMMANDS`), `:77-79` (dispatch)
* `src/photoflow/enrich/clustering.py:12-19` (top-level `import numpy as np`), `:38-48`, `:87-88`
* `tests/test_enrich_deps.py:1-5` (imports), append two tests

**Recommended agent:** sonnet — mechanical import move, fully specified, no design judgement.

**Depends on:** nothing.

**Why:** `pyproject.toml:6-8` declares only `pillow-heif` as a core dependency, but `cli.py:11-17`
imports all seven enrich command modules at load time, and `enrich/cluster.py:11` imports
`enrich/clustering.py`, which does `import numpy as np` at module level. So `pip install photoflow`
without `[enrich]` makes **every** command — `scan`, `plan`, `status` — die with
`ModuleNotFoundError: numpy` before argparse even runs. Masked in dev because the dev dependency
group pulls scikit-learn (and therefore numpy).

#### Steps

1. **Write the failing tests.** Append to `tests/test_enrich_deps.py` (and extend its import block
   at the top of the file from nothing to the three stdlib imports shown):

   ```python
   # --- at the very top of tests/test_enrich_deps.py, above the existing photoflow imports ---
   import subprocess
   import sys

   from conftest import pf
   ```

   ```python
   # --- appended at the end of tests/test_enrich_deps.py ---


   def test_core_cli_imports_without_numpy_or_sklearn():
       """Core commands must not hard-require the [enrich] stack.

       `pip install photoflow` pulls only pillow-heif, but cli.py used to import all seven
       enrich command modules at load time and enrich/clustering.py imported numpy at module
       level, so `photoflow status` died with ModuleNotFoundError before argparse ran (R4).
       Blocking the modules in a subprocess reproduces a bare install without uninstalling
       anything.
       """
       code = (
           "import sys;"
           " sys.modules['numpy'] = None;"
           " sys.modules['sklearn'] = None;"
           " import photoflow.cli;"
           " print('ok')"
       )
       proc = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True)
       assert proc.returncode == 0, proc.stderr
       assert "ok" in proc.stdout


   def test_status_still_dispatches(tmp_path):
       # The lazy-import refactor must not break the enrich dispatch or the core dispatch.
       assert "manifest" in pf(tmp_path / "wd", "status").stdout.lower()
       assert "faces" in pf(tmp_path / "wd", "enrich", "status").stdout.lower()
   ```

2. **Run it, expect failure.**

   ```
   uv run pytest -q tests/test_enrich_deps.py
   ```

   Expect `test_core_cli_imports_without_numpy_or_sklearn` to FAIL with the assertion on
   `proc.returncode == 0`, and the captured stderr showing
   `ImportError: import of numpy halted; None in sys.modules` raised from
   `photoflow/enrich/clustering.py`. (`test_status_still_dispatches` should pass already.)

3. **Implement — `cli.py`.** Delete lines 11-17 (the seven `from photoflow.enrich...` imports) and
   the `ENRICH_COMMANDS` dict at lines 23-31. In their place, add this function immediately after the
   remaining imports:

   ```python
   def enrich_command(step: str):
       """Import an enrich command module lazily.

       Core commands must run on a bare install (`dependencies = ["pillow-heif"]`), but every
       enrich module pulls numpy/scikit-learn directly or through enrich.clustering. Importing
       them at photoflow.cli load time made `photoflow status` fail with ModuleNotFoundError
       before argparse ran, so the import happens only once an `enrich` sub-step is dispatched.
       """
       from photoflow.enrich.apply import cmd_enrich_apply
       from photoflow.enrich.assign import cmd_enrich_assign
       from photoflow.enrich.cluster import cmd_enrich_cluster
       from photoflow.enrich.merge import cmd_enrich_merge
       from photoflow.enrich.review import cmd_enrich_review
       from photoflow.enrich.scan import cmd_enrich_scan
       from photoflow.enrich.status import cmd_enrich_status

       return {
           "scan": cmd_enrich_scan,
           "cluster": cmd_enrich_cluster,
           "assign": cmd_enrich_assign,
           "merge": cmd_enrich_merge,
           "review": cmd_enrich_review,
           "apply": cmd_enrich_apply,
           "status": cmd_enrich_status,
       }[step]
   ```

   And change the dispatch branch (was lines 77-79) to:

   ```python
       if args.cmd == "enrich":
           label = f"enrich-{args.enrich_step}"
           command_fn = enrich_command(args.enrich_step)
   ```

4. **Implement — `enrich/clustering.py`.** Delete the module-level `import numpy as np` (line 14) and
   add a function-scope import as the first statement of each of the three functions, matching how
   `enrich/assign.py:29` and `enrich/review.py:46` already do it:

   ```python
   def _l2_normalize(x):
       import numpy as np

       norms = np.linalg.norm(x, axis=1, keepdims=True)
       return x / np.clip(norms, 1e-12, None)
   ```

   ```python
   def cluster_embeddings(
       embeddings,
       *,
       min_cluster_size: int = 5,
       min_samples: int | None = None,
       cluster_selection_epsilon: float = 0.0,
       cluster_selection_method: str = "eom",
   ):
       """..."""  # keep the existing docstring verbatim
       import numpy as np

       embeddings = np.asarray(embeddings, dtype=np.float32)
       ...  # rest of the body unchanged
   ```

   ```python
   def nearest_person(embedding, person_centroids, threshold: float) -> tuple[int | None, float]:
       """..."""  # keep the existing docstring verbatim
       import numpy as np

       if not person_centroids:
           return None, 0.0
       ...  # rest of the body unchanged
   ```

   Drop the `np.ndarray` annotations from the three signatures (shown above) and from the local
   variable annotation in `cluster_embeddings`; `from __future__ import annotations` is present so
   *return* annotations are already lazy strings, but the module must not evaluate `np` anywhere at
   import time. Add a one-line note under the module docstring:

   ```python
   # numpy is imported INSIDE each function, not at module scope: photoflow.cli reaches this
   # module through the enrich dispatch, and core commands must import on a bare install (R4).
   ```

5. **Run the tests, expect PASS.**

   ```
   uv run pytest -q tests/test_enrich_deps.py tests/test_enrich_cli.py tests/test_enrich_clustering.py
   uv run pytest -q
   ```

   Expect all green (156+ passed, 1 skipped baseline).

6. **Lint + format.**

   ```
   uv run ruff check src tests && uv run ruff format src tests
   ```

7. **Commit.**

   ```
   git add src/photoflow/cli.py src/photoflow/enrich/clustering.py tests/test_enrich_deps.py
   git commit -m "fix(cli): import enrich commands lazily so core commands don't require numpy"
   ```

---

### Task C2: `enrich apply` — incremental, mtime-safe, failure-aware, dry-run-clean (T9 / R1 / R2 / R12)

**Files:**
* `src/photoflow/db.py:101-108` (`enrich_state` in `SCHEMA`), `:111-118` (`_migrate`)
* `src/photoflow/exiftool.py:103-115` (`exiftool_apply_argfile`)
* `src/photoflow/enrich/apply.py:45-165` (whole command body)
* `src/photoflow/cli.py:68-69` (`enrich apply` parser: add `--all`)
* `tests/test_enrich_commands.py:277-511` (existing apply tests need monkeypatch updates), append new
  tests after line 511
* `tests/test_db_migration.py` (append one migration test)

**Recommended agent:** opus — the largest task; transaction semantics (`--dry-run` must not mutate),
batch failure accounting and the R1 "missing from the keyword read" branch all need judgement, and it
reshapes a command other tasks build on.

**Depends on:** C1.

**Why (five bugs in one command):**
* **H10** — `apply.py:87-92` selects *every* file with a person or tag and rewrites it; `enrich_state.applied`
  is written but never read. Nine apply runs so far = nine full rewrites of the library.
* **H9** — `apply.py:148` writes `-overwrite_original` with no `-P`, so every rewrite resets the
  library file's mtime to now (full re-index for Immich/digiKam, full re-upload for mtime-based backup).
* **R1** — `apply.py:134` falls back to `existing=set()` when a file is missing from `existing_map`
  (one corrupt XMP makes `read_keywords` return `{}` for its whole 200-file batch,
  `exiftool.py:96` swallows it), and the clear-then-rewrite `-XMP-dc:Subject=` then wipes every
  pre-existing keyword on those files, silently.
* **R2** — `--dry-run` still runs the step 1/1b UPDATEs; the guarded commit at `apply.py:71` is
  skipped but the unconditional `conn.commit()` at `apply.py:164` commits them anyway.
* **E2 / R12** — `exiftool_apply_argfile` ignores the return code and `applied=1` is written
  regardless; there is no `exiftool_available()` check and only one `file_id=0` audit row per run.

#### Steps

1. **Write the failing tests.** First, fix the four existing monkeypatched apply tests so they keep
   working with the new contract. In `tests/test_enrich_commands.py`, add this helper right after
   `_write_csv` (line 275) and use it in place of the inline lambdas:

   ```python
   def _fake_exiftool(captured):
       """Stand in for exiftool_apply_argfile: collect every batch's lines, report success."""

       def run(lines):
           captured.setdefault("lines", []).extend(lines)
           return ExiftoolResult(0, "", "")

       return run
   ```

   Add `from photoflow.exiftool import ExiftoolResult` to the test module's imports (after the
   `from photoflow.db import ...` line). Then in each of
   `test_apply_builds_region_and_keyword_args`, `test_apply_writes_sidecar_target_for_raw`,
   `test_apply_respects_blacklist_and_rejects` and
   `test_apply_marks_fully_dismissed_cluster_ignored`, replace the two `monkeypatch.setattr(...)`
   lines with:

   ```python
       captured = {}
       monkeypatch.setattr(eapply, "exiftool_available", lambda: True)
       monkeypatch.setattr(eapply, "read_keywords", lambda paths: {p: set() for p in paths})
       monkeypatch.setattr(eapply, "exiftool_apply_argfile", _fake_exiftool(captured))
   ```

   (in `test_apply_marks_fully_dismissed_cluster_ignored` there is no `captured` today — add it;
   `test_apply_respects_blacklist_and_rejects` keeps its `captured.get("lines", [])` assertion, which
   still holds because nothing is written for that file.)

   Now append the new tests at the end of the apply section (after
   `test_apply_real_exiftool_roundtrip`, line 511):

   ```python
   def _one_face_file(tmp_path, person="Mum", n=1):
       """Seed n library files, one face each, plus a faces.csv naming them all `person`."""
       conn, workdir, lib, ids = _seed(tmp_path, n=n)
       rows = []
       for fid in ids:
           _insert_face(conn, fid, which=0)
           face_id = conn.execute("SELECT MAX(id) m FROM faces").fetchone()["m"]
           conn.execute("UPDATE faces SET bbox=? WHERE id=?", ("[20,20,80,100]", face_id))
           rows.append(_face_row(1, face_id, fid, person=person, decision="keep"))
       conn.commit()
       _write_csv(workdir / "faces.csv", FACE_COLS, rows)
       _write_csv(
           workdir / "tags.csv", ["file_id", "tag", "source", "score", "suggestion", "decision"], []
       )
       return conn, workdir, lib, ids


   def test_apply_is_incremental_on_the_second_run(tmp_path, monkeypatch, capsys):
       # H10: apply rewrote every enriched file on every run (9 runs = 9 full-library rewrites,
       # 9 mtime bumps). A per-file signature of what we'd write makes the second run a no-op.
       conn, workdir, lib, ids = _one_face_file(tmp_path)
       captured = {}
       monkeypatch.setattr(eapply, "exiftool_available", lambda: True)
       monkeypatch.setattr(eapply, "read_keywords", lambda paths: {p: set() for p in paths})
       monkeypatch.setattr(eapply, "exiftool_apply_argfile", _fake_exiftool(captured))

       _run(eapply.cmd_enrich_apply, conn, workdir, dry_run=False, all=False)
       assert captured["lines"], "first run must write"
       sig1 = conn.execute("SELECT applied_sig FROM enrich_state").fetchone()["applied_sig"]
       assert sig1

       captured.clear()
       _run(eapply.cmd_enrich_apply, conn, workdir, dry_run=False, all=False)
       out = capsys.readouterr().out
       assert captured.get("lines", []) == []  # nothing rewritten
       assert "written 0" in out and "unchanged 1" in out
       assert conn.execute("SELECT applied_sig FROM enrich_state").fetchone()["applied_sig"] == sig1

       # --all forces the rewrite back on
       _run(eapply.cmd_enrich_apply, conn, workdir, dry_run=False, all=True)
       assert captured["lines"]


   def test_apply_rewrites_only_the_file_whose_people_changed(tmp_path, monkeypatch):
       conn, workdir, lib, ids = _one_face_file(tmp_path, n=2)
       captured = {}
       monkeypatch.setattr(eapply, "exiftool_available", lambda: True)
       monkeypatch.setattr(eapply, "read_keywords", lambda paths: {p: set() for p in paths})
       monkeypatch.setattr(eapply, "exiftool_apply_argfile", _fake_exiftool(captured))
       _run(eapply.cmd_enrich_apply, conn, workdir, dry_run=False, all=False)

       # rename the person on ONE file only (a second person row + repoint one face)
       conn.execute("INSERT INTO persons(name, created) VALUES ('Mother','')")
       pid = conn.execute("SELECT id FROM persons WHERE name='Mother'").fetchone()["id"]
       target = conn.execute("SELECT id FROM faces WHERE file_id=?", (ids[0],)).fetchone()["id"]
       conn.execute("UPDATE faces SET person_id=? WHERE id=?", (pid, target))
       conn.commit()

       captured.clear()
       _run(eapply.cmd_enrich_apply, conn, workdir, dry_run=False, all=False)
       dests = {r["dest_path"] for r in conn.execute("SELECT id, dest_path FROM files")}
       written = [ln for ln in captured["lines"] if ln in dests]
       changed = conn.execute("SELECT dest_path FROM files WHERE id=?", (ids[0],)).fetchone()
       assert written == [changed["dest_path"]]  # only the changed file was rewritten


   def test_apply_dry_run_mutates_nothing(tmp_path, monkeypatch):
       # R2: the dry run used to durably commit the step-1 person upsert + faces.person_id,
       # hiding those clusters from the next `enrich review`.
       conn, workdir, lib, ids = _one_face_file(tmp_path)
       monkeypatch.setattr(eapply, "exiftool_available", lambda: True)
       monkeypatch.setattr(eapply, "read_keywords", lambda paths: {p: set() for p in paths})
       monkeypatch.setattr(eapply, "exiftool_apply_argfile", _fake_exiftool({}))

       _run(eapply.cmd_enrich_apply, conn, workdir, dry_run=True, all=False)

       assert conn.execute("SELECT COUNT(*) c FROM persons").fetchone()["c"] == 0
       assert (
           conn.execute("SELECT COUNT(*) c FROM faces WHERE person_id IS NOT NULL").fetchone()["c"]
           == 0
       )
       assert conn.execute("SELECT COUNT(*) c FROM enrich_state").fetchone()["c"] == 0


   def test_apply_skips_files_whose_keyword_read_failed(tmp_path, monkeypatch, capsys):
       # R1: one corrupt XMP makes read_keywords return {} for its whole 200-file batch. Falling
       # back to existing=set() and clearing dc:Subject would wipe every pre-existing keyword.
       conn, workdir, lib, ids = _one_face_file(tmp_path)
       captured = {}
       monkeypatch.setattr(eapply, "exiftool_available", lambda: True)
       monkeypatch.setattr(eapply, "read_keywords", lambda paths: {})
       monkeypatch.setattr(eapply, "exiftool_apply_argfile", _fake_exiftool(captured))

       _run(eapply.cmd_enrich_apply, conn, workdir, dry_run=False, all=False)

       out = capsys.readouterr().out
       assert captured.get("lines", []) == []  # nothing written rather than everything clobbered
       assert "skipped-unreadable 1" in out
       assert conn.execute("SELECT COUNT(*) c FROM enrich_state").fetchone()["c"] == 0


   def test_apply_failed_batch_keeps_the_old_signature(tmp_path, monkeypatch, capsys):
       # E2: a read-only/locked file made exiftool exit non-zero and apply reported success.
       conn, workdir, lib, ids = _one_face_file(tmp_path)
       monkeypatch.setattr(eapply, "exiftool_available", lambda: True)
       monkeypatch.setattr(eapply, "read_keywords", lambda paths: {p: set() for p in paths})
       monkeypatch.setattr(
           eapply,
           "exiftool_apply_argfile",
           lambda lines: ExiftoolResult(1, "", "Error: img0.jpg is not writable"),
       )

       _run(eapply.cmd_enrich_apply, conn, workdir, dry_run=False, all=False)

       out = capsys.readouterr().out
       assert "failed 1" in out and "not writable" in out
       row = conn.execute("SELECT applied_sig FROM enrich_state").fetchone()
       assert row is None or row["applied_sig"] is None  # never marked applied


   @pytest.mark.exiftool
   def test_apply_preserves_library_mtime(tmp_path):
       # H9: -overwrite_original without -P resets mtime to "now" on every apply, and HANDOFF
       # §2.1 promises the library mtime is the source mtime.
       import os

       conn, workdir, lib, ids = _one_face_file(tmp_path)
       dest = conn.execute("SELECT dest_path FROM files").fetchone()["dest_path"]
       os.utime(dest, (1_000_000_000, 1_000_000_000))
       before = os.stat(dest).st_mtime

       _run(eapply.cmd_enrich_apply, conn, workdir, dry_run=False, all=False)

       assert os.stat(dest).st_mtime == pytest.approx(before, abs=2)
   ```

   And in `tests/test_db_migration.py`, append:

   ```python
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
       conn.close()
       open_db(tmp_path)  # idempotent
   ```

2. **Run them, expect failure.**

   ```
   uv run pytest -q tests/test_enrich_commands.py tests/test_db_migration.py
   ```

   Expect `ImportError: cannot import name 'ExiftoolResult'` at collection (that is the first
   failure — fix it in step 3 and re-run to see the rest fail on `applied_sig` /
   `TypeError: cmd_enrich_apply() got an unexpected keyword argument` style errors).

3. **Implement — `exiftool.py`.** First check whether Lane B already landed this:

   ```
   grep -n "ExiftoolResult" src/photoflow/exiftool.py
   ```

   If it prints nothing, add `from dataclasses import dataclass` to the imports and replace
   `exiftool_apply_argfile` (lines 103-115) with:

   ```python
   @dataclass(frozen=True)
   class ExiftoolResult:
       """Outcome of one exiftool argfile run. Callers MUST check returncode: a read-only or
       locked file makes exiftool exit non-zero while writing nothing, and enrich apply used to
       record those files as successfully applied."""

       returncode: int
       stdout: str = ""
       stderr: str = ""


   def exiftool_apply_argfile(lines: list[str]) -> ExiftoolResult:
       """Run one exiftool process over a prepared -execute argfile (fast batching)."""
       if not lines:
           return ExiftoolResult(0)
       with tempfile.NamedTemporaryFile("w", suffix=".args", delete=False, encoding="utf-8") as af:
           af.write("\n".join(lines) + "\n")
           argfile = af.name
       try:
           res = subprocess.run(
               ["exiftool", "-@", argfile, "-charset", "filename=utf8"],
               capture_output=True,
               text=True,
               encoding="utf-8",
               errors="replace",
           )
           return ExiftoolResult(res.returncode, res.stdout or "", res.stderr or "")
       finally:
           os.unlink(argfile)
   ```

   `apply.py:93` (core) calls this and ignores the return value — that still works; leave it to Lane A/B.

4. **Implement — `db.py`.** In `SCHEMA`, add the column to the `enrich_state` definition
   (lines 101-107):

   ```sql
   CREATE TABLE IF NOT EXISTS enrich_state (   -- incremental skip, like scan's size+mtime rule
       file_id INTEGER PRIMARY KEY,
       faces_done INTEGER DEFAULT 0,
       tags_done INTEGER DEFAULT 0,
       applied INTEGER DEFAULT 0,
       applied_sig TEXT,          -- hash of what apply last wrote; equal => skip the rewrite
       ts TEXT
   );
   ```

   and append a block to `_migrate()` (after the existing `faces.ignored` block, keeping it
   self-contained so Lane A's `files.meta_read` block merges cleanly):

   ```python
       cols = {r["name"] for r in conn.execute("PRAGMA table_info(enrich_state)")}
       if "applied_sig" not in cols:
           conn.execute("ALTER TABLE enrich_state ADD COLUMN applied_sig TEXT")
           conn.commit()
   ```

   NULL on every pre-existing row is deliberate: the first apply after this upgrade rewrites the
   already-enriched library once (which is also what picks up the C3 hierarchy fix), then settles.

5. **Implement — `cli.py`.** Add the flag to the `enrich apply` parser (line 68-69):

   ```python
       ea = esub.add_parser("apply", help="write confirmed people + tags into the library files")
       ea.add_argument("--dry-run", action="store_true")
       ea.add_argument(
           "--all", action="store_true", help="rewrite every file, even unchanged ones"
       )
   ```

6. **Implement — `enrich/apply.py`.** Replace the imports and `cmd_enrich_apply` body. Keep
   `_read_csv` and `_upsert_person` as they are.

   ```python
   import csv
   import hashlib
   import json
   from collections import defaultdict
   from datetime import datetime
   from pathlib import Path

   from photoflow.audit import log_action
   from photoflow.enrich.page import face_is_applied, tag_is_applied
   from photoflow.enrich.regions import keyword_argfile_lines, region_argfile_lines
   from photoflow.exiftool import exiftool_apply_argfile, exiftool_available, read_keywords
   from photoflow.xmp import EMBED_EXT

   # -execute blocks per exiftool process. Failure is recorded per BATCH, not per file: exiftool
   # reports "N image files updated" for the whole run, so a batch that exits non-zero leaves
   # every one of its files with its previous applied_sig and gets retried next run.
   WRITE_BATCH = 100
   ```

   ```python
   def _signature(tags, people, regions, img_w, img_h) -> str:
       """Hash of everything this file's write depends on. Equal signature => skip the rewrite.

       Deliberately excludes the file's EXISTING keywords: a keyword added in digiKam must not
       trigger a rewrite (the write is a union, so nothing would change) - only OUR data does.
       """
       payload = {
           "tags": list(tags),
           "people": list(people),
           "regions": [[name, [float(v) for v in bbox]] for name, bbox in regions],
           "dims": [img_w, img_h],
       }
       return hashlib.sha1(json.dumps(payload, sort_keys=True).encode("utf-8")).hexdigest()
   ```

   ```python
   def cmd_enrich_apply(conn, workdir, run_id, log_fh, args, cfg):
       dry = getattr(args, "dry_run", False)
       rewrite_all = getattr(args, "all", False)
       if not exiftool_available():
           print("enrich apply: exiftool not found on PATH - nothing written.")
           return
       face_csv = _read_csv(workdir / "faces.csv")
       tag_csv = _read_csv(workdir / "tags.csv")
       now = datetime.now().isoformat(timespec="seconds")

       # 1. make person assignments durable (faces.person_id)
       for row in face_csv:
           if face_is_applied(row.get("person", ""), row.get("decision", "")):
               pid = _upsert_person(conn, row["person"].strip())
               conn.execute("UPDATE faces SET person_id=? WHERE id=?", (pid, int(row["face_id"])))

       # 1b. "not interested" clusters: a cluster whose every member is skipped (none named) was
       # dismissed wholesale in the page -> mark its faces ignored so re-cluster/review drop them
       # for good. A lone skip inside an otherwise-named cluster is just an eject, left eligible.
       by_cluster: dict[str, list[dict]] = defaultdict(list)
       for row in face_csv:
           cid = (row.get("cluster_id") or "").strip()
           if cid:
               by_cluster[cid].append(row)
       for members in by_cluster.values():
           if all((m.get("decision") or "") == "skip" for m in members):
               for m in members:
                   conn.execute(
                       "UPDATE faces SET ignored=1, cluster_id=NULL, cluster_prob=NULL WHERE id=?",
                       (int(m["face_id"]),),
                   )
       # R2: in dry mode NOTHING above is committed - the whole command runs inside one
       # transaction that is rolled back at the end, so a dry run can't hide clusters from the
       # next `enrich review`.
       if not dry:
           conn.commit()

       # 2. tag decision overlay: blacklist wildcards + per-(file,tag) review decisions
       blacklist = {
           r["tag"]
           for r in tag_csv
           if str(r.get("file_id")) == "*" and r.get("decision") == "reject"
       }
       review_dec = {
           (str(r["file_id"]), r["tag"]): (r.get("decision") or "")
           for r in tag_csv
           if str(r.get("file_id")) != "*"
       }

       # 3. candidate files: any assigned-person face or any tag. EXISTS subqueries (not an
       # IN(...) list) so this never trips SQLite's 32766-variable limit on a large library.
       targets: dict[int, tuple[str, str]] = {}  # file_id -> (write_target, dest_path)
       file_rows = conn.execute(
           """SELECT id, dest_path, ext FROM files f
              WHERE dest_path IS NOT NULL
                AND (EXISTS (SELECT 1 FROM faces WHERE file_id=f.id AND person_id IS NOT NULL)
                     OR EXISTS (SELECT 1 FROM tags WHERE file_id=f.id))"""
       ).fetchall()
       for fr in file_rows:
           is_embed = (fr["ext"] or "").lower() in EMBED_EXT
           dest = fr["dest_path"]
           targets[fr["id"]] = (dest if is_embed else dest + ".xmp", dest)
       if not targets:
           print("enrich apply: nothing to write (no assigned people or tags).")
           if dry:
               conn.rollback()
           return

       prior_sig = {
           r["file_id"]: r["applied_sig"]
           for r in conn.execute("SELECT file_id, applied_sig FROM enrich_state")
       }

       # 4. pass one: compute what each file WOULD get and skip the ones already carrying it.
       pending: list[dict] = []
       unchanged = 0
       for fid, (target, dest) in targets.items():
           tags_for_file = sorted(
               t["tag"]
               for t in conn.execute("SELECT tag, status FROM tags WHERE file_id=?", (fid,))
               if tag_is_applied(
                   t["status"], review_dec.get((str(fid), t["tag"]), ""), t["tag"] in blacklist
               )
           )
           people: list[str] = []
           regions: list[tuple[str, tuple]] = []
           img_w = img_h = None
           for fa in conn.execute(
               "SELECT fa.bbox, fa.img_w, fa.img_h, p.name FROM faces fa "
               "JOIN persons p ON p.id = fa.person_id WHERE fa.file_id=?",
               (fid,),
           ):
               people.append(fa["name"])
               try:
                   bbox = tuple(json.loads(fa["bbox"]))
               except (TypeError, ValueError, json.JSONDecodeError):
                   continue
               regions.append((fa["name"], bbox))
               img_w, img_h = fa["img_w"], fa["img_h"]
           people = sorted(set(people))

           if not tags_for_file and not people:
               continue
           sig = _signature(tags_for_file, people, regions, img_w, img_h)
           if not rewrite_all and prior_sig.get(fid) == sig:
               unchanged += 1
               continue
           pending.append(
               {
                   "fid": fid,
                   "target": target,
                   "dest": dest,
                   "tags": tags_for_file,
                   "people": people,
                   "regions": regions,
                   "w": img_w,
                   "h": img_h,
                   "sig": sig,
               }
           )

       # 5. pass two: read the CURRENT keywords of just the files we're about to rewrite.
       existing_map = read_keywords([p["target"] for p in pending]) if pending else {}
       owned_people = {r["name"] for r in conn.execute("SELECT name FROM persons")}

       blocks: list[tuple[int, str, list[str]]] = []  # (file_id, sig, argfile lines)
       skipped_unreadable = 0
       for p in pending:
           key = str(Path(p["target"]))
           if key not in existing_map:
               # R1: one corrupt XMP makes read_keywords return {} for its whole batch. Writing
               # with existing=set() would CLEAR every pre-existing keyword on these files.
               skipped_unreadable += 1
               print(f"  WARNING: could not read existing keywords, skipping {p['dest']}")
               continue
           lines = keyword_argfile_lines(
               existing_map[key],
               p["tags"],
               p["people"],
               prefix=cfg.people_keyword_prefix,
               iptc=cfg.write_iptc_keywords,
               owned_people=owned_people,
           )
           if cfg.write_mwg_regions and p["regions"] and p["w"] and p["h"]:
               lines += region_argfile_lines(p["w"], p["h"], p["regions"])
           if dry:
               print(f"DRY enrich {p['dest']}: +{len(p['tags'])} tags, {len(p['people'])} people")
               continue
           # -P preserves the file's mtime (H9); without it every apply bumps the whole library.
           blocks.append((p["fid"], p["sig"], ["-P", "-overwrite_original", *lines, p["target"], "-execute"]))

       # 6. write in batches; a batch that exits non-zero marks NONE of its files applied.
       written = failed = 0
       for i in range(0, len(blocks), WRITE_BATCH):
           chunk = blocks[i : i + WRITE_BATCH]
           lines: list[str] = []
           for _fid, _sig, block in chunk:
               lines += block
           print(f"  writing enrich XMP {i + 1}-{i + len(chunk)} of {len(blocks)} (exiftool)...")
           res = exiftool_apply_argfile(lines)
           if res.returncode != 0:
               failed += len(chunk)
               head = "\n    ".join((res.stderr or "").strip().splitlines()[:5])
               print(f"  exiftool batch FAILED (rc={res.returncode}); not marking applied:\n    {head}")
               continue
           for fid, sig, _block in chunk:
               conn.execute(
                   "INSERT INTO enrich_state(file_id, applied, applied_sig, ts) VALUES (?,1,?,?) "
                   "ON CONFLICT(file_id) DO UPDATE SET applied=1, applied_sig=excluded.applied_sig,"
                   " ts=excluded.ts",
                   (fid, sig, now),
               )
               log_action(conn, log_fh, run_id, fid, "enrich_applied", sig)
               written += 1

       if dry:
           conn.rollback()  # R2: discard step 1/1b - a dry run mutates nothing
       log_action(
           conn,
           log_fh,
           run_id,
           0,
           "enrich_apply",
           f"written={written} unchanged={unchanged} skipped={skipped_unreadable} "
           f"failed={failed} dry={dry}",
       )
       conn.commit()
       print(
           f"enrich apply: written {written} / unchanged {unchanged} / "
           f"skipped-unreadable {skipped_unreadable} / failed {failed}"
           f"{' (dry-run, nothing written)' if dry else ''}."
       )
   ```

   Note the `owned_people=` keyword on `keyword_argfile_lines` — that parameter does not exist yet.
   Add it to `regions.py` in **C3**; until then this call fails. Do C2 and C3 back-to-back, or
   temporarily drop that kwarg in C2 and add it in C3. **Preferred: drop the `owned_people=` kwarg in
   C2 and add it as part of C3's edit**, so each task's suite is green on its own commit.

7. **Run the tests, expect PASS.**

   ```
   uv run pytest -q tests/test_enrich_commands.py tests/test_db_migration.py tests/test_enrich_cli.py
   uv run pytest -q
   ```

8. **Lint + format.**

   ```
   uv run ruff check src tests && uv run ruff format src tests
   ```

9. **Commit.**

   ```
   git add src/photoflow/db.py src/photoflow/exiftool.py src/photoflow/enrich/apply.py src/photoflow/cli.py tests/test_enrich_commands.py tests/test_db_migration.py
   git commit -m "fix(enrich): apply only rewrites changed files, preserves mtime, and honours failures"
   ```

---

### Task C3: preserve foreign `HierarchicalSubject` / `PersonInImage` (T10 / H11 / R6)

**Files:**
* `src/photoflow/exiftool.py:62-100` (`read_keywords`)
* `src/photoflow/enrich/regions.py:71-105` (`keyword_argfile_lines`)
* `src/photoflow/enrich/apply.py` (pass `owned_people=`; see C2 step 6)
* `tests/test_enrich_regions.py` (append union-rule tests)
* `tests/test_enrich_commands.py` (append one `@pytest.mark.exiftool` round-trip)

**Recommended agent:** opus — the union rules have four cases each (keep foreign / replace ours /
drop renamed / emit nothing) and the "when to emit a bare clear line" rule is easy to get subtly
wrong in a way no cheap test catches.

**Depends on:** C2.

**Why:** `regions.py:103-104` clears `-XMP-lr:HierarchicalSubject=` and rewrites only `People|<name>`
entries, and `read_keywords` (`exiftool.py:73`) reads only `dc:Subject` + `IPTC:Keywords`. So a
`Places|Paris` hierarchy added in digiKam or Lightroom is **deleted** on the first `enrich apply`.
Same for `PersonInImage`: names tagged by another tool are wiped. `dc:Subject` is already unioned
correctly — only the two people-shaped lists are destructive.

#### Steps

1. **Write the failing tests.** In `tests/test_enrich_regions.py`, extend the import block to

   ```python
   from photoflow.exiftool import KeywordSets
   ```

   and append:

   ```python
   def test_keyword_lines_keep_foreign_hierarchy_and_replace_only_our_branch():
       # H11: a Places|Paris hierarchy added in digiKam must survive; only the People| branch
       # is ours to rewrite.
       existing = KeywordSets(
           subject={"Holiday"},
           hierarchical={"Places|Paris", "People|Stale"},
           persons=set(),
       )
       lines = keyword_argfile_lines(existing, tags=set(), people={"Mum"}, prefix="People")
       hier = [
           ln.split("=", 1)[1]
           for ln in lines
           if ln.startswith("-XMP-lr:HierarchicalSubject=") and ln != "-XMP-lr:HierarchicalSubject="
       ]
       assert "Places|Paris" in hier  # foreign hierarchy preserved
       assert "People|Mum" in hier  # our branch rewritten
       assert "People|Stale" not in hier  # our branch REPLACED, not unioned
       assert lines.count("-XMP-lr:HierarchicalSubject=") == 1  # cleared exactly once


   def test_keyword_lines_keep_foreign_person_and_drop_our_renamed_one():
       existing = KeywordSets(subject=set(), hierarchical=set(), persons={"Grandma", "Old Name"})
       lines = keyword_argfile_lines(
           existing, tags=set(), people={"Mum"}, owned_people={"Mum", "Old Name"}
       )
       persons = [
           ln.split("=", 1)[1]
           for ln in lines
           if ln.startswith("-XMP-iptcExt:PersonInImage=")
           and ln != "-XMP-iptcExt:PersonInImage="
       ]
       assert "Grandma" in persons  # foreign name preserved (photoflow doesn't know it)
       assert "Mum" in persons
       assert "Old Name" not in persons  # a name photoflow OWNS but no longer assigns is dropped


   def test_keyword_lines_leave_people_lists_alone_when_there_are_no_people():
       # A tags-only file must not touch PersonInImage/HierarchicalSubject at all.
       existing = KeywordSets(subject=set(), hierarchical={"Places|Paris"}, persons={"Grandma"})
       lines = keyword_argfile_lines(existing, tags={"beach"}, people=set(), owned_people={"Mum"})
       assert not any("HierarchicalSubject" in ln for ln in lines)
       assert not any("PersonInImage" in ln for ln in lines)


   def test_keyword_lines_clear_when_our_last_entry_goes_away():
       # Every entry was ours and none is assigned any more -> emit the bare clear line so the
       # stale value actually leaves the file.
       existing = KeywordSets(subject=set(), hierarchical={"People|Old"}, persons={"Old"})
       lines = keyword_argfile_lines(existing, tags=set(), people=set(), owned_people={"Old"})
       assert lines.count("-XMP-lr:HierarchicalSubject=") == 1
       assert not any(ln.startswith("-XMP-lr:HierarchicalSubject=People") for ln in lines)
       assert lines.count("-XMP-iptcExt:PersonInImage=") == 1


   def test_keyword_lines_still_accept_a_plain_set_of_subjects():
       # Back-compat: callers/tests that pass just the dc:Subject set keep working.
       lines = keyword_argfile_lines({"Holiday"}, tags={"beach"}, people={"Mum"})
       assert "-XMP-dc:Subject=Holiday" in lines and "-XMP-dc:Subject=beach" in lines
       assert "-XMP-iptcExt:PersonInImage=Mum" in lines
   ```

   And in `tests/test_enrich_commands.py`, append after the C2 tests:

   ```python
   @pytest.mark.exiftool
   def test_apply_preserves_foreign_hierarchy_and_person(tmp_path):
       # H11 end-to-end: values another tool wrote survive an apply.
       import json
       import subprocess

       conn, workdir, lib, ids = _one_face_file(tmp_path, person="Yancey")
       dest = conn.execute("SELECT dest_path FROM files").fetchone()["dest_path"]
       subprocess.run(
           [
               "exiftool",
               "-overwrite_original",
               "-XMP-lr:HierarchicalSubject=Places|Paris",
               "-XMP-iptcExt:PersonInImage=Grandma",
               dest,
           ],
           capture_output=True,
           check=True,
       )

       _run(eapply.cmd_enrich_apply, conn, workdir, dry_run=False, all=False)

       out = subprocess.run(
           ["exiftool", "-j", "-XMP-lr:HierarchicalSubject", "-XMP-iptcExt:PersonInImage", dest],
           capture_output=True,
           text=True,
       )
       rec = json.loads(out.stdout)[0]

       def as_list(v):
           return [v] if isinstance(v, str) else (v or [])

       hier = as_list(rec.get("HierarchicalSubject"))
       persons = as_list(rec.get("PersonInImage"))
       assert "Places|Paris" in hier and "People|Yancey" in hier
       assert "Grandma" in persons and "Yancey" in persons
   ```

2. **Run them, expect failure.**

   ```
   uv run pytest -q tests/test_enrich_regions.py
   ```

   Expect `ImportError: cannot import name 'KeywordSets' from 'photoflow.exiftool'` at collection.

3. **Implement — `exiftool.py`.** Add `from dataclasses import dataclass, field` to the imports
   (extend the `dataclass` import you added in C2), then replace `read_keywords` (lines 62-100):

   ```python
   @dataclass
   class KeywordSets:
       """What a library file already carries in the lists enrich apply rewrites.

       `subject` merges dc:Subject and IPTC:Keywords (photoflow always writes them in step);
       `hierarchical` and `persons` are kept separate because only OUR entries in them may be
       replaced - a Places|Paris hierarchy or a PersonInImage written in digiKam must survive.
       """

       subject: set[str] = field(default_factory=set)
       hierarchical: set[str] = field(default_factory=set)
       persons: set[str] = field(default_factory=set)


   def _tag_set(value) -> set[str]:
       """exiftool returns a scalar for a one-item list and omits absent tags entirely."""
       if isinstance(value, str):
           return {value}
       if isinstance(value, list):
           return {str(x) for x in value}
       return set()


   def read_keywords(paths: list[str], batch_size: int = 200) -> dict[str, KeywordSets]:
       """Read the existing keyword-ish lists for each path.

       Used by enrich apply to union new tags/people with what's already on the file (the
       provenance folder keywords apply wrote, plus any user edits) so the write is a superset
       and re-applying is idempotent. A path MISSING from the returned dict means the read
       failed - callers must skip it, never treat it as "no keywords" (R1).
       """
       out: dict[str, KeywordSets] = {}
       for i in range(0, len(paths), batch_size):
           batch = paths[i : i + batch_size]
           with tempfile.NamedTemporaryFile(
               "w", suffix=".args", delete=False, encoding="utf-8"
           ) as af:
               af.write(
                   "-j\n-charset\nfilename=utf8\n-XMP-dc:Subject\n-IPTC:Keywords\n"
                   "-XMP-lr:HierarchicalSubject\n-XMP-iptcExt:PersonInImage\n"
               )
               for p in batch:
                   af.write(p + "\n")
               argfile = af.name
           try:
               res = subprocess.run(
                   ["exiftool", "-@", argfile],
                   capture_output=True,
                   text=True,
                   encoding="utf-8",
                   errors="replace",
               )
               if res.stdout.strip():
                   for rec in json.loads(res.stdout):
                       key = str(Path(rec.get("SourceFile", "")))
                       out[key] = KeywordSets(
                           subject=_tag_set(rec.get("Subject")) | _tag_set(rec.get("Keywords")),
                           hierarchical=_tag_set(rec.get("HierarchicalSubject")),
                           persons=_tag_set(rec.get("PersonInImage")),
                       )
           except (json.JSONDecodeError, OSError) as e:
               print(f"  exiftool keyword read failed: {e}", file=sys.stderr)
           finally:
               os.unlink(argfile)
       return out
   ```

4. **Implement — `enrich/regions.py`.** Add `from photoflow.exiftool import KeywordSets` to the
   imports (core → enrich is the established direction; `exiftool.py` pulls only stdlib, so
   `regions.py` stays free of third-party imports and CI-testable), then replace
   `keyword_argfile_lines` (lines 71-105):

   ```python
   def keyword_argfile_lines(
       existing,
       tags: Iterable[str],
       people: Iterable[str],
       *,
       prefix: str = "People",
       iptc: bool = True,
       owned_people: Iterable[str] = (),
   ) -> list[str]:
       """Idempotent read-union-replace argfile lines for keywords + people.

       `existing` is what the file carries right now (a KeywordSets from read_keywords, or a
       plain set of dc:Subject values for callers that only have those). dc:Subject is a pure
       UNION so user keywords and photoflow's provenance folder keywords are never lost.

       The two people-shaped lists are trickier, because other tools write into them too:
         * lr:HierarchicalSubject - everything NOT under `<prefix>|` is foreign (Places|Paris
           from digiKam) and is preserved verbatim; only our `<prefix>|` branch is replaced.
         * Iptc4xmpExt:PersonInImage - names photoflow OWNS (`owned_people`, i.e. every row in
           the persons table) are ours to replace; any other name was written by another tool
           and survives.
       Each list is cleared (`-TAG=`) then rewritten so re-applying yields the same set instead
       of duplicating entries. A list is left completely untouched when the resulting set is
       empty AND was already empty, so a tags-only file never gets a stray clear.
       """
       if isinstance(existing, KeywordSets):
           ex_subject = set(existing.subject)
           ex_hier = set(existing.hierarchical)
           ex_persons = set(existing.persons)
       else:  # back-compat: a bare iterable of dc:Subject values
           ex_subject, ex_hier, ex_persons = set(existing or ()), set(), set()

       people = sorted(set(people))
       owned = set(owned_people)
       subjects = sorted(ex_subject | set(tags) | set(people))

       lines: list[str] = ["-XMP-dc:Subject="]
       lines += [f"-XMP-dc:Subject={s}" for s in subjects]

       if iptc:
           lines.append("-IPTC:Keywords=")
           lines += [f"-IPTC:Keywords={s}" for s in subjects]

       if prefix:
           new_hier = {h for h in ex_hier if not h.startswith(f"{prefix}|")}
           new_hier |= {f"{prefix}|{p}" for p in people}
       else:
           new_hier = set(ex_hier)
       lines += _replace_list_lines("-XMP-lr:HierarchicalSubject", ex_hier, new_hier)

       new_persons = (ex_persons - owned) | set(people)
       lines += _replace_list_lines("-XMP-iptcExt:PersonInImage", ex_persons, new_persons)
       return lines


   def _replace_list_lines(tag: str, before: set[str], after: set[str]) -> list[str]:
       """Clear-then-rewrite lines for one list tag; nothing at all when there's no change to make."""
       if after:
           return [f"{tag}="] + [f"{tag}={v}" for v in sorted(after)]
       if after != before:  # everything in it was ours and is gone -> clear it for real
           return [f"{tag}="]
       return []
   ```

5. **Implement — `enrich/apply.py`.** Add the `owned_people=owned_people` keyword to the
   `keyword_argfile_lines(...)` call (see C2 step 6); `owned_people` is already computed there,
   *after* step 1's `_upsert_person` calls, so freshly named people are included.

6. **Run the tests, expect PASS.**

   ```
   uv run pytest -q tests/test_enrich_regions.py tests/test_enrich_commands.py
   uv run pytest -q
   ```

7. **Lint + format.**

   ```
   uv run ruff check src tests && uv run ruff format src tests
   ```

8. **Commit.**

   ```
   git add src/photoflow/exiftool.py src/photoflow/enrich/regions.py src/photoflow/enrich/apply.py tests/test_enrich_regions.py tests/test_enrich_commands.py
   git commit -m "fix(enrich): preserve foreign HierarchicalSubject and PersonInImage on apply"
   ```

---

### Task C4: `enrich merge` strips the stale name from the files (T11 / H12 / R7 / R8)

**Files:**
* `src/photoflow/enrich/merge.py:38-63` (the merge loop + reporting)
* `src/photoflow/enrich/apply.py` (step 1: the `person_id IS NULL` guard)
* `tests/test_enrich_commands.py:600-650` (merge section — append four tests)

**Recommended agent:** sonnet — the CSV rule is settled below (see "Rename policy"), so this is
well-specified. Escalate to opus only if the rename policy turns out not to hold.

**Depends on:** C2 (needs `applied_sig` and `ExiftoolResult`), C3 (owned-name semantics).

**Why:** `keyword_remove_argfile_lines` (`regions.py:108`) is written and unit-tested but has **zero
callers**. `merge.py` repoints faces and prints "re-run enrich apply", but apply only ever *unions*
keywords, so the misspelled name stays in `dc:Subject` / `IPTC:Keywords` / `PersonInImage` /
`HierarchicalSubject` forever. MWG regions self-heal (the region list is a struct overwrite);
keywords do not. And (R8) `apply` step 1 re-applies every `keep` row of a stale `faces.csv`, so the
very "re-run apply" the merge message instructs **re-creates the alias person row and repoints its
faces back**.

**Rename policy (the R8 judgement call, resolved):** `enrich/review.py:75-79` only ever emits faces
with `fa.person_id IS NULL` into `faces.csv` and the page. A named face therefore never appears in
the CSV, so a CSV row can never legitimately *rename* an already-named face — the CSV only ever
grants a first name. The supported rename path is `enrich merge`. So the correct guard is the strict
one: apply a CSV keep row **only when the face is currently unassigned**, and do not even upsert the
person row otherwise (the upsert is what resurrects a deleted alias). Document this in the code.

#### Steps

1. **Write the failing tests.** Append to the merge section of `tests/test_enrich_commands.py`:

   ```python
   def _named_library_file(tmp_path, name):
       """One library file whose single face is assigned to `name`, already applied."""
       conn, workdir, lib, ids = _seed(tmp_path, n=1)
       fid = ids[0]
       conn.execute("INSERT INTO persons(name, created) VALUES (?, '')", (name,))
       pid = conn.execute("SELECT id FROM persons WHERE name=?", (name,)).fetchone()["id"]
       _insert_face(conn, fid, which=0, person_id=pid)
       conn.execute(
           "INSERT INTO enrich_state(file_id, applied, applied_sig, ts) VALUES (?,1,'deadbeef','')",
           (fid,),
       )
       conn.commit()
       return conn, workdir, lib, fid


   def test_merge_invalidates_applied_sig_for_touched_files(tmp_path, monkeypatch):
       conn, workdir, lib, fid = _named_library_file(tmp_path, "Deidre Hough")
       captured = {}
       monkeypatch.setattr(emerge, "exiftool_available", lambda: True)
       monkeypatch.setattr(emerge, "exiftool_apply_argfile", _fake_exiftool(captured))

       _run(
           emerge.cmd_enrich_merge,
           conn,
           workdir,
           canonical="Deirdre Hough",
           aliases=["Deidre Hough"],
       )

       # the stale name is stripped from the file...
       assert "-XMP-dc:Subject-=Deidre Hough" in captured["lines"]
       assert "-XMP-iptcExt:PersonInImage-=Deidre Hough" in captured["lines"]
       assert "-P" in captured["lines"] and "-overwrite_original" in captured["lines"]
       # ...and the file is queued for a rewrite so regions/PersonInImage get the new name
       row = conn.execute("SELECT applied_sig FROM enrich_state WHERE file_id=?", (fid,)).fetchone()
       assert row["applied_sig"] is None


   def test_merge_rewrites_faces_csv_in_place(tmp_path, monkeypatch):
       conn, workdir, lib, fid = _named_library_file(tmp_path, "Deidre Hough")
       face_id = conn.execute("SELECT id FROM faces").fetchone()["id"]
       _write_csv(
           workdir / "faces.csv",
           FACE_COLS,
           [_face_row(1, face_id, fid, person="Deidre Hough", decision="keep")],
       )
       monkeypatch.setattr(emerge, "exiftool_available", lambda: True)
       monkeypatch.setattr(emerge, "exiftool_apply_argfile", _fake_exiftool({}))

       _run(
           emerge.cmd_enrich_merge,
           conn,
           workdir,
           canonical="Deirdre Hough",
           aliases=["Deidre Hough"],
       )

       rows = list(csv.DictReader((workdir / "faces.csv").open(encoding="utf-8")))
       assert [r["person"] for r in rows] == ["Deirdre Hough"]


   def test_apply_with_a_stale_csv_does_not_resurrect_a_merged_alias(tmp_path, monkeypatch):
       # R8: merge deletes the alias person row, then apply replayed the old faces.csv keep row
       # and re-created it, repointing the faces back.
       conn, workdir, lib, fid = _named_library_file(tmp_path, "Deidre Hough")
       face_id = conn.execute("SELECT id FROM faces").fetchone()["id"]
       monkeypatch.setattr(emerge, "exiftool_available", lambda: True)
       monkeypatch.setattr(emerge, "exiftool_apply_argfile", _fake_exiftool({}))
       _run(
           emerge.cmd_enrich_merge,
           conn,
           workdir,
           canonical="Deirdre Hough",
           aliases=["Deidre Hough"],
       )

       # simulate a stale CSV coming back (restored from the page's localStorage, or a backup)
       _write_csv(
           workdir / "faces.csv",
           FACE_COLS,
           [_face_row(1, face_id, fid, person="Deidre Hough", decision="keep")],
       )
       _write_csv(
           workdir / "tags.csv", ["file_id", "tag", "source", "score", "suggestion", "decision"], []
       )
       monkeypatch.setattr(eapply, "exiftool_available", lambda: True)
       monkeypatch.setattr(eapply, "read_keywords", lambda paths: {p: set() for p in paths})
       monkeypatch.setattr(eapply, "exiftool_apply_argfile", _fake_exiftool({}))
       _run(eapply.cmd_enrich_apply, conn, workdir, dry_run=False, all=False)

       assert {r["name"] for r in conn.execute("SELECT name FROM persons")} == {"Deirdre Hough"}
       pid = conn.execute("SELECT person_id FROM faces WHERE id=?", (face_id,)).fetchone()
       canonical = conn.execute("SELECT id FROM persons").fetchone()["id"]
       assert pid["person_id"] == canonical


   @pytest.mark.exiftool
   def test_merge_removes_the_alias_from_the_real_file(tmp_path):
       import json
       import subprocess

       conn, workdir, lib, fid = _named_library_file(tmp_path, "Deidre Hough")
       dest = conn.execute("SELECT dest_path FROM files WHERE id=?", (fid,)).fetchone()["dest_path"]
       subprocess.run(
           [
               "exiftool",
               "-overwrite_original",
               "-XMP-dc:Subject=Deidre Hough",
               "-XMP-dc:Subject=beach",
               "-XMP-iptcExt:PersonInImage=Deidre Hough",
               dest,
           ],
           capture_output=True,
           check=True,
       )

       _run(
           emerge.cmd_enrich_merge,
           conn,
           workdir,
           canonical="Deirdre Hough",
           aliases=["Deidre Hough"],
       )

       rec = json.loads(
           subprocess.run(
               ["exiftool", "-j", "-XMP-dc:Subject", "-XMP-iptcExt:PersonInImage", dest],
               capture_output=True,
               text=True,
           ).stdout
       )[0]

       def as_list(v):
           return [v] if isinstance(v, str) else (v or [])

       assert "Deidre Hough" not in as_list(rec.get("Subject"))
       assert "beach" in as_list(rec.get("Subject"))  # other keywords untouched
       assert "Deidre Hough" not in as_list(rec.get("PersonInImage"))
   ```

2. **Run them, expect failure.**

   ```
   uv run pytest -q tests/test_enrich_commands.py -k merge
   ```

   Expect `AttributeError: <module 'photoflow.enrich.merge'> has no attribute
   'exiftool_available'` from the first monkeypatch.

3. **Implement — `enrich/merge.py`.** Extend the imports and the merge loop:

   ```python
   import csv
   from datetime import datetime

   from photoflow.audit import log_action
   from photoflow.enrich.regions import keyword_remove_argfile_lines
   from photoflow.exiftool import exiftool_apply_argfile, exiftool_available
   from photoflow.xmp import EMBED_EXT
   ```

   Replace the loop body (lines 38-46) so the touched files are collected **before** the faces are
   repointed (afterwards there is no way to tell which files carried the alias):

   ```python
       moved: list[tuple[str, int]] = []
       touched: dict[int, tuple[str, str]] = {}  # file_id -> (dest_path, ext)
       for alias in aliases:
           aid = _person_id(conn, alias)
           if aid is None or aid == cid:  # unknown name, or it's the canonical row itself
               continue
           # collect BEFORE repointing: once person_id is canonical the alias is untraceable
           for row in conn.execute(
               """SELECT DISTINCT f.id, f.dest_path, f.ext FROM faces fa
                  JOIN files f ON f.id = fa.file_id
                  WHERE fa.person_id=? AND f.dest_path IS NOT NULL""",
               (aid,),
           ):
               touched[row["id"]] = (row["dest_path"], row["ext"] or "")
           n = conn.execute("UPDATE faces SET person_id=? WHERE person_id=?", (cid, aid)).rowcount
           conn.execute("DELETE FROM persons WHERE id=?", (aid,))
           moved.append((alias, n))
       conn.commit()

       stripped = 0
       if moved and touched:
           stripped = _strip_alias_keywords(conn, touched, [a for a, _n in moved], cfg)
           # queue a rewrite so the next apply puts the canonical name into regions/PersonInImage
           for file_id in touched:  # one statement per id: an IN(...) list would trip the
               conn.execute(  # 32766-variable limit on a large merge
                   "UPDATE enrich_state SET applied_sig=NULL WHERE file_id=?", (file_id,)
               )
           conn.commit()
       csv_fixed = _rewrite_faces_csv(workdir / "faces.csv", {a: canonical for a, _n in moved})
   ```

   Add the two helpers below `_person_id`:

   ```python
   def _strip_alias_keywords(conn, touched: dict[int, tuple[str, str]], aliases: list[str], cfg) -> int:
       """Delete the merged-away names from the library files' keyword lists.

       enrich apply only ever UNIONS keywords, so a renamed person lingers in dc:Subject /
       IPTC:Keywords / PersonInImage / HierarchicalSubject on every already-applied file (H12).
       exiftool's '-=' removes that exact list value and is a no-op when it's absent, so this
       strips only the named values and never disturbs other keywords. -P keeps the mtime.
       """
       if not exiftool_available():
           print("enrich merge: exiftool not on PATH - the old name is still in the files.")
           return 0
       remove = keyword_remove_argfile_lines(
           aliases, iptc=cfg.write_iptc_keywords, people_prefix=cfg.people_keyword_prefix
       )
       lines: list[str] = []
       for dest, ext in touched.values():
           target = dest if ext.lower() in EMBED_EXT else dest + ".xmp"
           lines += ["-P", "-overwrite_original", *remove, target, "-execute"]
       res = exiftool_apply_argfile(lines)
       if res.returncode != 0:
           head = "\n    ".join((res.stderr or "").strip().splitlines()[:5])
           print(f"  exiftool reported errors while stripping old names:\n    {head}")
       return len(touched)


   def _rewrite_faces_csv(path, mapping: dict[str, str]) -> int:
       """Point alias names in the workdir faces.csv at the canonical name.

       faces.csv is a decision overlay that apply replays; leaving the alias in it means the
       page and the DB disagree about who this face is (and, before the apply guard, meant the
       next apply re-created the alias - R8).
       """
       if not path.exists() or not mapping:
           return 0
       with open(path, newline="", encoding="utf-8") as f:
           reader = csv.DictReader(f)
           columns = list(reader.fieldnames or [])
           rows = list(reader)
       n = 0
       for r in rows:
           name = (r.get("person") or "").strip()
           if name in mapping:
               r["person"] = mapping[name]
               n += 1
       if n:
           with open(path, "w", newline="", encoding="utf-8") as f:
               w = csv.DictWriter(f, fieldnames=columns)
               w.writeheader()
               w.writerows(rows)
       return n
   ```

   Update the reporting tail (lines 48-63) to mention the new work and drop the now-misleading
   "re-run apply to rewrite" wording:

   ```python
       total = sum(n for _a, n in moved)
       log_action(
           conn,
           log_fh,
           run_id,
           0,
           "enrich_merge",
           f"canonical={canonical} aliases={len(moved)} faces_moved={total} "
           f"files_stripped={stripped} csv_rows={csv_fixed}",
       )
       conn.commit()
       print(f"enrich merge: folded {len(moved)} alias(es) into '{canonical}', moved {total} face(s).")
       for alias, n in moved:
           print(f"  {alias} -> {canonical}: {n}")
       if stripped:
           print(f"  stripped the old name(s) from {stripped} library file(s).")
       if csv_fixed:
           print(f"  updated {csv_fixed} row(s) in faces.csv.")
       if total:
           print("Next: photoflow enrich apply (writes the canonical name into regions/people).")
   ```

4. **Implement — `enrich/apply.py` step 1 (R8).** Replace the step-1 loop with:

   ```python
       # 1. make person assignments durable (faces.person_id).
       # A CSV keep row only ever grants a FIRST name: enrich review emits faces with
       # person_id IS NULL only, so a named face never appears in faces.csv and a CSV row can
       # never legitimately rename one. Renames go through `enrich merge`. Guarding on
       # person_id IS NULL (and skipping the person upsert entirely) stops a stale CSV from
       # re-creating a merged-away alias and repointing its faces back (R8).
       for row in face_csv:
           if not face_is_applied(row.get("person", ""), row.get("decision", "")):
               continue
           face_id = int(row["face_id"])
           cur = conn.execute("SELECT person_id FROM faces WHERE id=?", (face_id,)).fetchone()
           if cur is None or cur["person_id"] is not None:
               continue
           pid = _upsert_person(conn, row["person"].strip())
           conn.execute("UPDATE faces SET person_id=? WHERE id=?", (pid, face_id))
   ```

5. **Run the tests, expect PASS.**

   ```
   uv run pytest -q tests/test_enrich_commands.py
   uv run pytest -q
   ```

6. **Lint + format.**

   ```
   uv run ruff check src tests && uv run ruff format src tests
   ```

7. **Commit.**

   ```
   git add src/photoflow/enrich/merge.py src/photoflow/enrich/apply.py tests/test_enrich_commands.py
   git commit -m "fix(enrich): merge strips the old name from library files and faces.csv"
   ```

---

### Task C5: `enrich scan` crash-resilience + progress (T12 / E1)

**Files:**
* `src/photoflow/db.py` (`SCHEMA` `enrich_state`, `_migrate`)
* `src/photoflow/enrich/scan.py:22-119` (whole command)
* `tests/test_enrich_commands.py:85-102` (scan section — append two tests)
* `tests/test_db_migration.py` (extend the C2 migration test to also assert `errors`)

**Recommended agent:** sonnet — mechanical error handling + counters, fully specified.

**Depends on:** C2 (shares the `enrich_state` migration block).

**Why:** `enrich/scan.py:107` commits every 200 files (~10 minutes of CPU inference lost per crash on
this rig), and `detector.detect` / `tagger.tag` are unguarded — one truncated JPEG or a CUDA OOM
aborts the whole run *and* rolls the batch back. No total is printed up front, so a multi-hour run
gives no sense of progress.

**Design note:** a failure deliberately does **not** mark that side done, so a transient OOM is
retried on the next run; the `errors` counter is what stops an unreadable file from being retried
forever (3 strikes, then it's excluded and reported).

#### Steps

1. **Write the failing tests.** Append to the scan section of `tests/test_enrich_commands.py`:

   ```python
   class FlakyDetector(FakeDetector):
       """Raises on its first call only - a truncated JPEG / transient CUDA OOM."""

       def __init__(self, which=0):
           super().__init__(which)
           self.calls = 0

       def detect(self, rgb):
           self.calls += 1
           if self.calls == 1:
               raise RuntimeError("boom: model blew up on this file")
           return super().detect(rgb)


   def test_scan_survives_a_detector_failure_and_retries_next_run(tmp_path, monkeypatch):
       # E1: one bad file aborted the whole run and rolled back the batch.
       conn, workdir, lib, ids = _seed(tmp_path, n=3)
       monkeypatch.setattr(edeps, "HAVE_FACES", True)
       monkeypatch.setattr(efaces, "FaceDetector", lambda cfg: FlakyDetector(which=0))
       monkeypatch.setattr(etagger, "build_tagger", lambda cfg, wd: FakeTagger())

       _run(escan.cmd_enrich_scan, conn, workdir)  # must not raise

       assert conn.execute("SELECT COUNT(*) c FROM faces").fetchone()["c"] == 2  # 2 of 3 ok
       assert (
           conn.execute(
               "SELECT COUNT(*) c FROM actions WHERE action='enrich_detect_error'"
           ).fetchone()["c"]
           == 1
       )
       bad = conn.execute("SELECT * FROM enrich_state WHERE errors=1").fetchall()
       assert len(bad) == 1
       assert bad[0]["faces_done"] == 0  # not marked done -> retried next run
       assert bad[0]["tags_done"] == 1  # the tagger side still succeeded


   def test_scan_gives_up_on_a_file_after_three_failures(tmp_path, monkeypatch, capsys):
       conn, workdir, lib, ids = _seed(tmp_path, n=2)
       conn.execute(
           "INSERT INTO enrich_state(file_id, faces_done, tags_done, errors, ts)"
           " VALUES (?,0,0,3,'')",
           (ids[0],),
       )
       conn.commit()
       detector = FakeDetector(which=0)
       calls = {"n": 0}
       original = detector.detect

       def counting(rgb):
           calls["n"] += 1
           return original(rgb)

       detector.detect = counting
       monkeypatch.setattr(edeps, "HAVE_FACES", True)
       monkeypatch.setattr(efaces, "FaceDetector", lambda cfg: detector)
       monkeypatch.setattr(etagger, "build_tagger", lambda cfg, wd: FakeTagger())

       _run(escan.cmd_enrich_scan, conn, workdir)

       assert calls["n"] == 1  # only the healthy file was processed
       out = capsys.readouterr().out
       assert "1 files to process" in out
       assert "repeated errors" in out
   ```

   And extend `test_open_db_adds_applied_sig_to_legacy_enrich_state` in `tests/test_db_migration.py`
   with one more assertion:

   ```python
       assert "errors" in cols
       assert conn.execute("SELECT errors FROM enrich_state").fetchone()["errors"] == 0
   ```

2. **Run them, expect failure.**

   ```
   uv run pytest -q tests/test_enrich_commands.py -k scan tests/test_db_migration.py
   ```

   Expect `test_scan_survives_a_detector_failure_and_retries_next_run` to FAIL with
   `RuntimeError: boom: model blew up on this file` escaping the command, and the migration test to
   fail on `assert "errors" in cols`.

3. **Implement — `db.py`.** Add the column to `SCHEMA`'s `enrich_state` (next to `applied_sig`):

   ```sql
       errors INTEGER DEFAULT 0,  -- consecutive model failures; 3 strikes and scan skips the file
   ```

   and extend the `enrich_state` migration block from C2:

   ```python
       cols = {r["name"] for r in conn.execute("PRAGMA table_info(enrich_state)")}
       if "applied_sig" not in cols:
           conn.execute("ALTER TABLE enrich_state ADD COLUMN applied_sig TEXT")
           conn.commit()
       if "errors" not in cols:
           conn.execute("ALTER TABLE enrich_state ADD COLUMN errors INTEGER DEFAULT 0")
           conn.commit()
   ```

4. **Implement — `enrich/scan.py`.** Add `import time` to the imports and a module constant, then
   rewrite the command:

   ```python
   # A file that fails this many times is left alone until something changes: a transient CUDA
   # OOM must be retried on the next run, a truncated JPEG must not burn a model call forever.
   MAX_ERRORS = 3
   COMMIT_EVERY = 20  # ~20 files of inference is the most we're willing to lose to a crash
   ```

   ```python
   def cmd_enrich_scan(conn, workdir, run_id, log_fh, args, cfg):
       rows = conn.execute(
           """SELECT f.id, f.dest_path, COALESCE(e.faces_done, 0) fd, COALESCE(e.tags_done, 0) td
              FROM files f LEFT JOIN enrich_state e ON e.file_id = f.id
              WHERE f.status = 'copied' AND f.kind = 'image' AND f.dest_path IS NOT NULL
                AND (e.file_id IS NULL OR e.faces_done = 0 OR e.tags_done = 0)
                AND COALESCE(e.errors, 0) < ?""",
           (MAX_ERRORS,),
       ).fetchall()
       skipped_errors = conn.execute(
           """SELECT COUNT(*) c FROM files f JOIN enrich_state e ON e.file_id = f.id
              WHERE f.status = 'copied' AND f.kind = 'image' AND f.dest_path IS NOT NULL
                AND (e.faces_done = 0 OR e.tags_done = 0) AND COALESCE(e.errors, 0) >= ?""",
           (MAX_ERRORS,),
       ).fetchone()["c"]
       if skipped_errors:
           print(f"enrich scan: skipping {skipped_errors} file(s) with repeated errors "
                 f"(>= {MAX_ERRORS} failures).")
       if not rows:
           print("enrich scan: nothing to do (all copied images already enriched).")
           return

       detector = faces_mod.FaceDetector(cfg) if deps.HAVE_FACES else None
       tagger = tagger_mod.build_tagger(cfg, workdir)
       if detector is None:
           print("NOTE: face stack unavailable - skipping faces (pip install 'photoflow[enrich]').")
       if tagger is None:
           print("NOTE: no tagger available - skipping content tags (see README enrich setup).")
       if detector is None and tagger is None:
           print("enrich scan: install photoflow[enrich] to detect faces or tag content.")
           return

       faces_dir = workdir / "faces"
       faces_dir.mkdir(exist_ok=True)
       total = len(rows)
       print(f"enrich scan: {total} files to process")
       n_files = n_faces = n_tags = n_errors = 0
       t0 = time.monotonic()

       for r in rows:
           try:
               im = open_rgb(r["dest_path"])
           except Exception as e:
               log_action(conn, log_fh, run_id, r["id"], "enrich_open_error", str(e)[:200])
               _bump_errors(conn, r["id"])
               n_errors += 1
               continue

           faces_ok = detector is None or bool(r["fd"])
           tags_ok = tagger is None or bool(r["td"])
           errored = False

           if detector is not None and not r["fd"]:
               import numpy as np

               try:
                   # materialize inside the guard: a generator would raise later, outside it
                   dets = list(detector.detect(np.asarray(im)))
               except Exception as e:
                   log_action(conn, log_fh, run_id, r["id"], "enrich_detect_error", str(e)[:200])
                   errored = True
               else:
                   n_faces += _store_faces(conn, r["id"], im, dets, cfg, faces_dir)
                   faces_ok = True

           if tagger is not None and not r["td"]:
               try:
                   items = list(tagger.tag(im))
               except Exception as e:
                   log_action(conn, log_fh, run_id, r["id"], "enrich_tag_error", str(e)[:200])
                   errored = True
               else:
                   n_tags += _store_tags(conn, r["id"], items, tagger, cfg)
                   tags_ok = True

           if errored:
               n_errors += 1
               _bump_errors(conn, r["id"])

           conn.execute(
               "INSERT INTO enrich_state(file_id, faces_done, tags_done, ts) VALUES (?,?,?,?) "
               "ON CONFLICT(file_id) DO UPDATE SET faces_done=MAX(faces_done, excluded.faces_done),"
               " tags_done=MAX(tags_done, excluded.tags_done), ts=excluded.ts",
               (
                   r["id"],
                   1 if (detector is not None and faces_ok) else 0,
                   1 if (tagger is not None and tags_ok) else 0,
                   datetime.now().isoformat(timespec="seconds"),
               ),
           )
           n_files += 1
           if n_files % COMMIT_EVERY == 0:
               conn.commit()
               rate = n_files / max(time.monotonic() - t0, 1e-6) * 60
               print(f"  enriched {n_files}/{total} ({rate:.1f} files/min)", flush=True)

       conn.commit()
       log_action(
           conn,
           log_fh,
           run_id,
           0,
           "enrich_scan",
           f"files={n_files} faces={n_faces} tags={n_tags} errors={n_errors}",
       )
       conn.commit()
       print(
           f"enrich scan complete: {n_files} files, {n_faces} faces, {n_tags} tags, "
           f"{n_errors} errors. Next: photoflow enrich cluster"
       )
   ```

   And the three helpers, above `cmd_enrich_scan`:

   ```python
   def _bump_errors(conn, file_id: int) -> None:
       """Count one failed attempt. MAX_ERRORS strikes and the candidate query drops the file."""
       conn.execute(
           "INSERT INTO enrich_state(file_id, errors, ts) VALUES (?,1,?) "
           "ON CONFLICT(file_id) DO UPDATE SET errors=COALESCE(errors,0)+1, ts=excluded.ts",
           (file_id, datetime.now().isoformat(timespec="seconds")),
       )


   def _store_faces(conn, file_id: int, im, dets, cfg, faces_dir) -> int:
       n = 0
       for fc in dets:
           cur = conn.execute(
               "INSERT INTO faces(file_id, bbox, det_score, embedding, img_w, img_h)"
               " VALUES (?,?,?,?,?,?)",
               (
                   file_id,
                   json.dumps([float(v) for v in fc["bbox"]]),
                   fc["det_score"],
                   fc["embedding"].tobytes(),
                   im.width,
                   im.height,
               ),
           )
           face_id = cur.lastrowid
           try:
               face_crop(im, fc["bbox"], cfg.face_crop_pad).save(
                   faces_dir / f"{face_id}.jpg", "JPEG", quality=80
               )
               conn.execute("UPDATE faces SET thumb=? WHERE id=?", (f"faces/{face_id}.jpg", face_id))
           except Exception:
               pass
           n += 1
       return n


   def _store_tags(conn, file_id: int, items, tagger, cfg) -> int:
       n = 0
       for tag, score in items:
           st = classify_tag(score, cfg.tag_score_accept, cfg.tag_score_review)
           if st is None:
               continue
           conn.execute(
               "INSERT OR IGNORE INTO tags(file_id, tag, source, score, status) VALUES (?,?,?,?,?)",
               (file_id, tag, getattr(tagger, "source", "clip"), score, st),
           )
           n += 1
       return n
   ```

5. **Run the tests, expect PASS.**

   ```
   uv run pytest -q tests/test_enrich_commands.py tests/test_db_migration.py
   uv run pytest -q
   ```

6. **Lint + format.**

   ```
   uv run ruff check src tests && uv run ruff format src tests
   ```

7. **Commit.**

   ```
   git add src/photoflow/db.py src/photoflow/enrich/scan.py tests/test_enrich_commands.py tests/test_db_migration.py
   git commit -m "feat(enrich): scan survives per-file model failures, commits every 20, shows progress"
   ```

---

### Task C6: persist the tag blacklist in the DB (T23 / R5)

**Files:**
* `src/photoflow/db.py` (`SCHEMA`: new `tag_blacklist` table)
* `src/photoflow/enrich/apply.py` (blacklist = DB ∪ CSV; persist the CSV rows)
* `src/photoflow/enrich/review.py:103-146` (exclude blacklisted tags; re-emit the `*` rows)
* `src/photoflow/enrich/page.py:93-109`, `:195-225`, `:411-412` (`blacklist_rows`, payload field, JS seed)
* `src/photoflow/enrich/scan.py` (skip inserting blacklisted tags)
* `tests/test_enrich_page.py`, `tests/test_enrich_commands.py`

**Recommended agent:** sonnet — additive, well-specified, five small edits across five files.

**Depends on:** C2 (apply structure), C5 (scan structure).

**Why (R5):** the page appends global-blacklist rows (`file_id='*'`, `decision='reject'`) to
tags.csv, but `tag_rows()` (`page.py:93`) never re-emits them, the JS `blacklist` Set
(`page.py:411`) is seeded only from localStorage, and apply never persists them. So after the next
`enrich review` the blacklisted tag is written into every file — and because the keyword write is a
union it can never be removed again. That violates the decisions-carry-forward invariant (HANDOFF §2.4)
for the enrich overlay.

#### Steps

1. **Write the failing tests.** In `tests/test_enrich_page.py`, add `blacklist_rows` to the import
   list and append:

   ```python
   def test_blacklist_rows_are_wildcard_reject_rows():
       rows = blacklist_rows(["person", "document"])
       assert [r["file_id"] for r in rows] == ["*", "*"]
       assert all(list(r.keys()) == TAG_COLUMNS for r in rows)
       assert {r["tag"] for r in rows} == {"person", "document"}
       assert all(r["decision"] == "reject" for r in rows)


   def test_tags_payload_carries_the_blacklist_and_page_seeds_it():
       # R5: the JS Set was seeded only from localStorage, so a blacklist saved on one machine
       # (or after clearing site data) silently came back as "apply this tag everywhere".
       payload = build_tags_payload([], [], workdir_key="W", blacklist=["person"])
       assert payload["blacklist"] == ["person"]
       html = render_page(
           build_people_payload(CLUSTERS, NOISE, face_rows(CLUSTERS, NOISE, {}), [], "W", 0.5),
           payload,
       )
       assert "TAGS.blacklist" in html  # the Set is seeded from the payload, not just storage
       assert '"person"' in html
   ```

   In `tests/test_enrich_commands.py`, append a blacklist section at the end of the file:

   ```python
   # --------------------------------------------------------------- durable tag blacklist (R5)


   def test_apply_persists_the_csv_blacklist_into_the_db(tmp_path, monkeypatch):
       conn, workdir, lib, ids = _seed(tmp_path, n=1)
       fid = ids[0]
       conn.execute(
           "INSERT INTO tags(file_id, tag, source, score, status) VALUES (?,?,?,?,?)",
           (fid, "person", "clip", 0.9, "auto"),
       )
       conn.commit()
       _write_csv(workdir / "faces.csv", FACE_COLS, [])
       _write_csv(
           workdir / "tags.csv",
           ["file_id", "tag", "source", "score", "suggestion", "decision"],
           [
               {
                   "file_id": "*",
                   "tag": "person",
                   "source": "",
                   "score": "",
                   "suggestion": "auto",
                   "decision": "reject",
               }
           ],
       )
       monkeypatch.setattr(eapply, "exiftool_available", lambda: True)
       monkeypatch.setattr(eapply, "read_keywords", lambda paths: {p: set() for p in paths})
       monkeypatch.setattr(eapply, "exiftool_apply_argfile", _fake_exiftool({}))

       _run(eapply.cmd_enrich_apply, conn, workdir, dry_run=False, all=False)

       assert [r["tag"] for r in conn.execute("SELECT tag FROM tag_blacklist")] == ["person"]


   def test_apply_never_writes_a_db_blacklisted_tag(tmp_path, monkeypatch):
       conn, workdir, lib, ids = _seed(tmp_path, n=1)
       fid = ids[0]
       conn.execute("INSERT INTO tag_blacklist(tag, ts) VALUES ('person','')")
       conn.execute(
           "INSERT INTO tags(file_id, tag, source, score, status) VALUES (?,?,?,?,?)",
           (fid, "person", "clip", 0.9, "auto"),
       )
       conn.execute(
           "INSERT INTO tags(file_id, tag, source, score, status) VALUES (?,?,?,?,?)",
           (fid, "beach", "clip", 0.9, "auto"),
       )
       conn.commit()
       _write_csv(workdir / "faces.csv", FACE_COLS, [])
       _write_csv(
           workdir / "tags.csv", ["file_id", "tag", "source", "score", "suggestion", "decision"], []
       )
       captured = {}
       monkeypatch.setattr(eapply, "exiftool_available", lambda: True)
       monkeypatch.setattr(eapply, "read_keywords", lambda paths: {p: set() for p in paths})
       monkeypatch.setattr(eapply, "exiftool_apply_argfile", _fake_exiftool(captured))

       _run(eapply.cmd_enrich_apply, conn, workdir, dry_run=False, all=False)

       assert "-XMP-dc:Subject=beach" in captured["lines"]
       assert "-XMP-dc:Subject=person" not in captured["lines"]


   def test_review_carries_the_blacklist_forward(tmp_path):
       conn, workdir, lib, ids = _seed(tmp_path, n=1)
       fid = ids[0]
       conn.execute("INSERT INTO tag_blacklist(tag, ts) VALUES ('person','')")
       conn.execute(
           "INSERT INTO tags(file_id, tag, source, score, status) VALUES (?,?,?,?,?)",
           (fid, "person", "clip", 0.4, "review"),
       )
       conn.execute(
           "INSERT INTO tags(file_id, tag, source, score, status) VALUES (?,?,?,?,?)",
           (fid, "boat", "clip", 0.4, "review"),
       )
       conn.commit()

       _run(ereview.cmd_enrich_review, conn, workdir)

       rows = list(csv.DictReader((workdir / "tags.csv").open(encoding="utf-8")))
       wildcard = [r for r in rows if r["file_id"] == "*"]
       assert [r["tag"] for r in wildcard] == ["person"]  # re-emitted so the page + apply agree
       assert all(r["tag"] != "person" for r in rows if r["file_id"] != "*")  # not up for review
       html = (workdir / "enrich_review.html").read_text(encoding="utf-8")
       assert "TAGS.blacklist" in html and '"person"' in html
   ```

2. **Run them, expect failure.**

   ```
   uv run pytest -q tests/test_enrich_page.py tests/test_enrich_commands.py -k blacklist
   ```

   Expect `ImportError: cannot import name 'blacklist_rows'` at collection of `test_enrich_page.py`,
   and `sqlite3.OperationalError: no such table: tag_blacklist` from the command tests.

3. **Implement — `db.py`.** Append to `SCHEMA` (after the `enrich_state` table). No `_migrate` entry
   is needed: `CREATE TABLE IF NOT EXISTS` runs on every `open_db`, so old DBs get it for free.

   ```sql
   CREATE TABLE IF NOT EXISTS tag_blacklist (  -- durable "never write this tag" decisions (R5)
       tag TEXT PRIMARY KEY,
       ts TEXT
   );
   ```

4. **Implement — `enrich/page.py`.** Add the row builder next to `tag_rows` (after line 109):

   ```python
   def blacklist_rows(tags: list[str]) -> list[dict]:
       """tags.csv rows for the global blacklist (file_id='*', reject).

       Re-emitted from the DB on every review so the decision carries forward: the page seeds
       its Set from them, and apply drops the tag everywhere.
       """
       return [
           {
               "file_id": "*",
               "tag": t,
               "source": "",
               "score": "",
               "suggestion": "auto",
               "decision": "reject",
           }
           for t in tags
       ]
   ```

   Change `build_tags_payload` (line 195) to take and return the blacklist:

   ```python
   def build_tags_payload(
       items: list, rows: list[dict], workdir_key: str, blacklist: list[str] | None = None
   ) -> dict:
   ```

   and its return statement to

   ```python
       return {
           "workdir": workdir_key,
           "reviewTags": review,
           "autoSummary": summary,
           "blacklist": sorted(blacklist or []),
       }
   ```

   In `PAGE_TEMPLATE`, seed the JS Set from the payload (line 411-412):

   ```javascript
   const tagState = {}, blacklist = new Set();
   // Seed from the DB-backed payload FIRST, then let localStorage add this session's picks:
   // seeding only from storage meant a saved blacklist silently reverted on another machine (R5).
   for (const t of (TAGS.blacklist || [])) blacklist.add(t);
   for (const g of TAGS.reviewTags) for (const p of g.photos) tagState[p.file_id+"|"+g.tag] = p.decision||"";
   ```

5. **Implement — `enrich/review.py`.** Import `blacklist_rows` from `photoflow.enrich.page`, load the
   set before building the tag items, filter both queries, and append the rows:

   ```python
       blacklist = {r["tag"] for r in conn.execute("SELECT tag FROM tag_blacklist")}
   ```

   ```python
       for r in conn.execute(
           """SELECT t.file_id, t.tag, t.source, t.score, f.dest_path
              FROM tags t JOIN files f ON f.id = t.file_id WHERE t.status='review' ORDER BY t.tag"""
       ):
           if r["tag"] in blacklist:
               continue  # a blacklisted tag is never written, so don't spend review time on it
           ...
   ```

   ```python
       auto_items = [
           {"file_id": r["file_id"], "tag": r["tag"], "suggestion": "auto"}
           for r in conn.execute("SELECT file_id, tag FROM tags WHERE status='auto'")
           if r["tag"] not in blacklist
       ]
   ```

   ```python
       t_rows = tag_rows(tag_items, _read_prior_tags(workdir / "tags.csv")) + blacklist_rows(
           sorted(blacklist)
       )
       ...
       tags_payload = build_tags_payload(tag_items + auto_items, t_rows, wk, sorted(blacklist))
   ```

6. **Implement — `enrich/apply.py`.** Replace the blacklist computation in step 2 and move it
   **above** the `if not dry: conn.commit()` line so the inserts ride the same transaction (and are
   rolled back on a dry run):

   ```python
       # 2. tag decision overlay: blacklist wildcards (durable in tag_blacklist + this run's CSV
       # rows) and per-(file,tag) review decisions.
       csv_blacklist = {
           r["tag"]
           for r in tag_csv
           if str(r.get("file_id")) == "*" and r.get("decision") == "reject"
       }
       db_blacklist = {r["tag"] for r in conn.execute("SELECT tag FROM tag_blacklist")}
       for t in sorted(csv_blacklist - db_blacklist):
           conn.execute("INSERT OR IGNORE INTO tag_blacklist(tag, ts) VALUES (?,?)", (t, now))
       blacklist = csv_blacklist | db_blacklist
   ```

7. **Implement — `enrich/scan.py`.** Load the set once and skip those tags at insert time (cheap,
   keeps the tags table from re-growing junk after a blacklist):

   ```python
       blacklist = {r["tag"] for r in conn.execute("SELECT tag FROM tag_blacklist")}
   ```

   pass it into `_store_tags(conn, r["id"], items, tagger, cfg, blacklist)` and add at the top of the
   per-tag loop:

   ```python
           if tag in blacklist:
               continue
   ```

8. **Run the tests, expect PASS.**

   ```
   uv run pytest -q tests/test_enrich_page.py tests/test_enrich_commands.py
   uv run pytest -q
   ```

9. **Lint + format.**

   ```
   uv run ruff check src tests && uv run ruff format src tests
   ```

10. **Commit.**

    ```
    git add src/photoflow/db.py src/photoflow/enrich/apply.py src/photoflow/enrich/review.py src/photoflow/enrich/page.py src/photoflow/enrich/scan.py tests/test_enrich_page.py tests/test_enrich_commands.py
    git commit -m "fix(enrich): persist the tag blacklist in the DB so it carries forward"
    ```

---

## Done criteria for Lane C

* `uv run pytest` fully green (baseline 156 passed / 1 skipped, plus ~20 new tests).
* `uv run ruff check src tests` clean.
* Six commits, one per task, in order C1 → C6.
* No file under `photoflow_work/` was read, written, or opened by any test or command run.
* Hand back to the coordinator for the A → B → C merge; flag the `db.py` `_migrate` and
  `exiftool.py` overlaps in the handoff note.

## Owner runbook after this lane lands (not executed by the implementer)

1. `photoflow enrich apply --dry-run` — confirms the counts look sane (every already-enriched file
   reports as changed on the first run, because `applied_sig` starts NULL).
2. `photoflow enrich apply` — one final full rewrite that installs the preserved-hierarchy write and
   fills in `applied_sig`. From then on runs report `written 0 / unchanged N`.
3. `photoflow enrich merge "<Correct Name>" "<misspelling>"` — now strips the old name from the
   files and queues those files for the next apply.
