# Lane B — apply hardening / sidecars / review page / docs Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans (or subagent-driven-development) to implement this plan task-by-task.

**Goal:** Make `photoflow apply` crash-safe and mtime-preserving, stop copying sidecar
files (`.thm/.aae/.xmp`) into the library and give the owner a non-destructive way to
evict the 475 already-copied junk files, fix the interactive review page so an
already-in-library keeper can never be silently paired with a newly imported near-dupe,
and bring README/HANDOFF/CLAUDE.md back in line with the code.

**Architecture:** `photoflow` is a `scan → plan → review → apply` pipeline over a SQLite
manifest (`workdir/photoflow.db`). Every command module shares the signature
`(conn, workdir, run_id, log_fh, args, cfg)` and is dispatched by `src/photoflow/cli.py`.
Pure logic (`naming.py`, `dates.py`, `review_page.py`) has no I/O and is unit-tested
without exiftool; `exiftool.py`/`xmp.py` are the only places that shell out to exiftool.
`role`/`group_id`/`dupe_of` are derived state recomputed by every `plan`; only statuses
`copied`, `error`, `skipped_manual` are durable (`models.py:DURABLE_STATUSES`).

**Tech Stack:** Python 3.11+ (`uv`), stdlib `sqlite3`/`argparse`/`shutil`/`subprocess`,
Pillow + ImageHash (optional), exiftool on PATH, pytest (`@pytest.mark.exiftool` marks
tests that need it), ruff (line-length 100, target py311), node (already on PATH — used
by the existing review-page JS tests via a DOM shim).

## Constraints (read before task B1)

* Work in a **fresh git worktree off `feature/enrich`** (`superpowers:using-git-worktrees`).
  HEAD at planning time: `0b9ad1c`.
* **Never run `photoflow` against the repo's `photoflow_work/`** — that is the owner's live
  manifest (153k rows) and an `enrich scan` is running against it. Every test uses a
  `tmp_path` workdir and a `tmp_path` library. Do not read, write, or delete anything under
  `photoflow_work/`.
* **Sources are read-only** (HANDOFF §2.1). No task here may write to, move or delete a
  source file. `prune-sidecars` (B3) moves files inside the *library*, never deletes, and
  never touches sources.
* One commit per task. `uv run pytest` must be **fully green** (156 passed / 1 skipped at
  baseline, plus the tests you add) and `uv run ruff check src tests` clean before each
  commit.
* `src/photoflow/exiftool.py` is also edited by Lane A (`exiftool_json` signature) and
  Lane C (`read_keywords`). **Touch only `exiftool_apply_argfile` and `merge_metadata`**
  so the merges stay clean.
* No binary assets: every test image is synthesized with Pillow via the helpers in
  `tests/conftest.py` (`_gradient`, `_set_exif`).

---

### Task B1: `apply` hardening — atomic copies, error isolation, mtime preservation

**Files:**
* `src/photoflow/exiftool.py:103-132` (`exiftool_apply_argfile`, `merge_metadata` only)
* `src/photoflow/xmp.py:26-31` (`embed_args`)
* `src/photoflow/apply.py:1-106` (whole module)
* `tests/test_xmp.py:9-17` (update `test_embed_args_exact_lines`)
* `tests/test_apply.py` (new file)

**Recommended agent:** opus — the copy/flush/commit ordering is the crash-safety
argument of the whole command; getting the flush on the wrong side of the commit
re-introduces H7.

**Depends on:** nothing.

#### Steps

1. **Write the failing tests.** Create `tests/test_apply.py` with exactly this content:

```python
"""apply hardening: atomic copies, per-file error isolation, mtime preservation.

The whole module needs exiftool because `scan` hard-exits without it.
"""

import os
import shutil
from pathlib import Path

import pytest
from conftest import _gradient, pf, q

from photoflow import apply as apply_mod
from photoflow.apply import cmd_apply
from photoflow.config import load_config
from photoflow.db import new_run, open_db
from photoflow.naming import dest_for

pytestmark = pytest.mark.exiftool

OLD_MTIME = 1104537600.0  # 2005-01-01 UTC


class Args:
    def __init__(self, out, dry_run=False, decisions=None):
        self.out = str(out)
        self.dry_run = dry_run
        self.decisions = decisions


def run_apply(work: Path, out: Path, **kw) -> None:
    """Call cmd_apply in-process (so tests can monkeypatch inside it)."""
    conn = open_db(work)
    run_id = new_run(conn, "apply", {})
    (work / "logs").mkdir(exist_ok=True)
    with open(work / "logs" / f"test_{run_id}.jsonl", "a", encoding="utf-8") as fh:
        cmd_apply(conn, work, run_id, fh, Args(out, **kw), load_config(work))
    conn.commit()
    conn.close()


def test_truncated_dest_is_recopied(photo_fixture: Path, tmp_path: Path):
    """A dest left half-written by a disk-full / yanked-USB run must not be trusted."""
    work, lib = tmp_path / "work", tmp_path / "library"
    pf(work, "scan", str(photo_fixture))
    pf(work, "plan")
    row = q(work, "SELECT * FROM files WHERE role='keep' AND source_path LIKE ?", "%beach.jpg")[0]
    dest = dest_for(row, lib.resolve(), 40)
    dest.parent.mkdir(parents=True)
    dest.write_bytes(b"truncated")

    pf(work, "apply", "--out", str(lib))

    src_size = Path(row["source_path"]).stat().st_size
    assert dest.read_bytes()[:2] == b"\xff\xd8"  # a real JPEG again
    assert dest.stat().st_size >= src_size  # >= : provenance XMP was embedded after the copy
    actions = {a["action"] for a in q(work, "SELECT action FROM actions")}
    assert "recopied_size_mismatch" in actions


def test_copy_error_is_isolated_to_one_row(photo_fixture: Path, tmp_path: Path, monkeypatch):
    """One unreadable/locked source must not abort the run."""
    work, lib = tmp_path / "work", tmp_path / "library"
    pf(work, "scan", str(photo_fixture))
    pf(work, "plan")
    victim = str(photo_fixture / "Old Laptop" / "Holiday 2015" / "beach.jpg")
    real_copy2 = shutil.copy2

    def fake_copy2(src, dst, *a, **kw):
        if str(src) == victim:
            raise PermissionError(13, "locked by another process")
        return real_copy2(src, dst, *a, **kw)

    monkeypatch.setattr(apply_mod.shutil, "copy2", fake_copy2)
    run_apply(work, lib)

    rows = {Path(r["source_path"]).name: r for r in q(work, "SELECT * FROM files")}
    assert rows["beach.jpg"]["status"] == "error"
    assert "locked by another process" in rows["beach.jpg"]["error"]
    assert rows["mountain.jpg"]["status"] == "copied"  # the run kept going
    assert not list(lib.rglob("*.part"))  # the partial copy was cleaned up
    actions = {a["action"] for a in q(work, "SELECT action FROM actions")}
    assert "copy_error" in actions


def test_dry_run_creates_no_directories(photo_fixture: Path, tmp_path: Path):
    work, lib = tmp_path / "work", tmp_path / "library"
    pf(work, "scan", str(photo_fixture))
    pf(work, "plan")
    pf(work, "apply", "--out", str(lib), "--dry-run")
    assert not lib.exists()


def test_library_mtime_equals_source_mtime(tmp_path: Path):
    """copy2 preserves mtime; exiftool -P must not reset it when embedding XMP."""
    src = tmp_path / "src"
    src.mkdir()
    photo = src / "old.jpg"
    _gradient(640, 480, seed=31).save(photo, "JPEG", quality=92)
    os.utime(photo, (OLD_MTIME, OLD_MTIME))
    work, lib = tmp_path / "work", tmp_path / "library"

    pf(work, "scan", str(src))
    pf(work, "plan")
    pf(work, "apply", "--out", str(lib))

    dest = Path(q(work, "SELECT dest_path FROM files")[0]["dest_path"])
    assert dest.suffix == ".jpg" and dest.exists()
    assert abs(dest.stat().st_mtime - OLD_MTIME) < 2  # FAT/exFAT tolerance
```

2. **Run them and watch them fail** — `uv run pytest -q tests/test_apply.py`.
   Expected: `4 failed`. `test_truncated_dest_is_recopied` fails on the
   `read_bytes()[:2]` assert (`b'tr' != b'\xff\xd8'` — the stub is trusted),
   `test_copy_error_is_isolated_to_one_row` fails with an uncaught `PermissionError`
   escaping `cmd_apply`, `test_dry_run_creates_no_directories` fails on
   `assert not lib.exists()`, `test_library_mtime_equals_source_mtime` fails because
   `exiftool -overwrite_original` reset the mtime to now.

3. **`exiftool.py`: make the argfile runner report instead of swallow.** Replace
   `exiftool_apply_argfile` (currently lines 103-115) and add the result type. Add
   `from dataclasses import dataclass` to the imports at the top of the file.

```python
@dataclass(frozen=True)
class ExiftoolResult:
    """Outcome of one batched exiftool run. Non-zero rc is data, not an exception:
    one locked file must never abort a whole apply/enrich-apply pass."""

    returncode: int
    stderr: str
    stdout: str


def exiftool_apply_argfile(lines: list[str]) -> ExiftoolResult:
    """Run one exiftool process over a prepared -execute argfile (fast batching)."""
    if not lines:
        return ExiftoolResult(0, "", "")
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
        return ExiftoolResult(res.returncode, res.stderr or "", res.stdout or "")
    finally:
        os.unlink(argfile)
```

   In `merge_metadata`, add `-P` as the first argument after `"exiftool"` so a fill-missing
   merge cannot bump the keeper's mtime either:

```python
    subprocess.run(
        [
            "exiftool",
            "-P",  # preserve FileModifyDate (HANDOFF §2.1)
            "-overwrite_original",
            "-wm",
            "cg",
            "-tagsfromfile",
            donor_path,
            "-all:all",
            keeper_path,
        ],
        capture_output=True,
    )
```

4. **`xmp.py`: `-P` first in every embed block.** Replace the body of `embed_args`:

```python
def embed_args(dest: str, description: str, keywords: list[str]) -> list[str]:
    """exiftool argfile lines to embed Dublin Core XMP into one file.

    -P preserves FileModifyDate: without it -overwrite_original resets the library
    file's mtime to "now", breaking HANDOFF §2.1 and re-triggering mtime-based
    re-indexing (Immich) / re-upload (backup) of the whole library.
    """
    lines = ["-P", "-overwrite_original", f"-XMP-dc:Description={description}"]
    lines += [f"-XMP-dc:Subject={k}" for k in keywords]
    lines += [dest, "-execute"]
    return lines
```

   Update `tests/test_xmp.py:9-17` so the expected list starts with `"-P"`:

```python
def test_embed_args_exact_lines():
    assert embed_args("d.jpg", "desc", ["k1", "k2"]) == [
        "-P",
        "-overwrite_original",
        "-XMP-dc:Description=desc",
        "-XMP-dc:Subject=k1",
        "-XMP-dc:Subject=k2",
        "d.jpg",
        "-execute",
    ]
```

5. **`apply.py`: atomic copy, size check, error isolation, batched XMP flush.** Rewrite
   the module. Full new content of `src/photoflow/apply.py`:

```python
"""Apply command: copy keepers into the organized library, embed provenance, merge metadata."""

from __future__ import annotations

import csv
import os
import shutil
from pathlib import Path

from photoflow.audit import log_action
from photoflow.exiftool import exiftool_apply_argfile, merge_metadata
from photoflow.naming import dest_for
from photoflow.xmp import EMBED_EXT, embed_args, xmp_sidecar


def _copy_atomic(src: str, dest: Path) -> None:
    """Copy via <dest>.part + os.replace so a crash / full disk can never leave a
    truncated file sitting at dest (os.replace is atomic within one filesystem)."""
    tmp = dest.with_name(dest.name + ".part")
    try:
        shutil.copy2(src, tmp)
        os.replace(tmp, dest)
    except OSError:
        tmp.unlink(missing_ok=True)
        raise


def _flush_xmp(conn, log_fh, run_id, xmp_args: list[str]) -> None:
    """Embed the queued provenance blocks and report (never raise) exiftool failures.

    Called before each commit, so a crash can only ever leave files embedded but not
    yet marked copied - which the next run repairs. The reverse order would mark rows
    copied with no provenance and never revisit them.
    """
    if not xmp_args:
        return
    print(f"  embedding XMP provenance for {xmp_args.count('-execute')} files (exiftool)...")
    res = exiftool_apply_argfile(xmp_args)
    if res.returncode != 0:
        head = " / ".join(res.stderr.strip().splitlines()[:3])
        print(f"exiftool reported errors: {head}")
        log_action(conn, log_fh, run_id, 0, "xmp_embed_errors", f"rc={res.returncode} {head}")
    xmp_args.clear()


def cmd_apply(conn, workdir, run_id, log_fh, args, cfg):
    out_root = Path(args.out).expanduser().resolve()
    decisions: dict[int, tuple[str, int | None]] = {}
    dec_path = Path(args.decisions) if args.decisions else workdir / "decisions.csv"
    if dec_path.exists():
        with open(dec_path, newline="", encoding="utf-8") as f:
            for row in csv.DictReader(f):
                d = (row.get("decision") or "").strip().lower()
                if d in ("keep", "skip"):
                    mf = (row.get("merge_from_file_id") or "").strip()
                    decisions[int(row["file_id"])] = (d, int(mf) if mf else None)

    rows = conn.execute("SELECT * FROM files WHERE status IN ('planned','review')").fetchall()
    copied = skipped = held = errors = 0
    xmp_args: list[str] = []
    merge_jobs: list[tuple[int, int]] = []

    for r in rows:
        role = r["role"]
        if role == "exact_dupe":
            conn.execute("UPDATE files SET status='skipped_dupe' WHERE id=?", (r["id"],))
            log_action(
                conn, log_fh, run_id, r["id"], "skipped_exact_dupe", f"dupe_of={r['dupe_of']}"
            )
            skipped += 1
            continue
        if role == "review":
            d = decisions.get(r["id"])
            if d is None:
                held += 1
                continue
            if d[0] == "skip":
                conn.execute("UPDATE files SET status='skipped_manual' WHERE id=?", (r["id"],))
                log_action(conn, log_fh, run_id, r["id"], "skipped_manual_review", "")
                skipped += 1
                continue
            if d[1]:
                merge_jobs.append((r["id"], d[1]))

        dest = dest_for(r, out_root, cfg.slug_max)
        if args.dry_run:
            print(f"DRY  {r['source_path']}  ->  {dest}")
            continue

        try:
            dest.parent.mkdir(parents=True, exist_ok=True)
            src_size = os.path.getsize(r["source_path"])
            if not dest.exists():
                _copy_atomic(r["source_path"], dest)
            elif dest.stat().st_size < src_size:
                # Only reachable when a previous run died between the copy and its
                # commit. A dest SMALLER than the source is a truncated copy from a
                # pre-atomic-copy version -> re-copy. A dest that is LARGER is a file
                # that was already XMP-embedded (embedding grows it) before the crash ->
                # trust it; the provenance lines are re-emitted below anyway, so it
                # converges without a wasted copy. (Coordinator note: `<` not `!=`.)
                _copy_atomic(r["source_path"], dest)
                log_action(conn, log_fh, run_id, r["id"], "recopied_size_mismatch", str(dest))
        except OSError as e:
            conn.execute("UPDATE files SET status='error', error=? WHERE id=?", (str(e), r["id"]))
            log_action(conn, log_fh, run_id, r["id"], "copy_error", str(e))
            errors += 1
            continue

        # provenance metadata: original folder names + dupes' folders as keywords
        rels = [r["rel_path"] or ""]
        for d2 in conn.execute("SELECT rel_path FROM files WHERE dupe_of=?", (r["id"],)):
            rels.append(d2["rel_path"] or "")
        kw = sorted({part for rel in rels for part in Path(rel).parts[:-1] if part})[:12]
        desc = "photoflow src: " + " | ".join(filter(None, rels))
        if r["ext"] in EMBED_EXT:
            xmp_args += embed_args(str(dest), desc, kw)
        else:
            xmp_sidecar(dest, desc, kw)

        conn.execute(
            "UPDATE files SET status='copied', dest_path=? WHERE id=?", (str(dest), r["id"])
        )
        log_action(
            conn,
            log_fh,
            run_id,
            r["id"],
            "copied",
            f"{r['source_path']} -> {dest} (date:{r['date_source']}/"
            f"{r['date_confidence']}, role:{role})",
        )
        copied += 1
        if copied % 500 == 0:
            _flush_xmp(conn, log_fh, run_id, xmp_args)
            conn.commit()
            print(f"  copied {copied}...")
    if not args.dry_run:
        _flush_xmp(conn, log_fh, run_id, xmp_args)
    conn.commit()

    # metadata merges chosen during review: fill missing tags from the twin
    for keeper_id, donor_id in merge_jobs:
        k = conn.execute("SELECT dest_path FROM files WHERE id=?", (keeper_id,)).fetchone()
        d = conn.execute("SELECT source_path FROM files WHERE id=?", (donor_id,)).fetchone()
        if k and k["dest_path"] and d and not args.dry_run:
            merge_metadata(d["source_path"], k["dest_path"])
            log_action(conn, log_fh, run_id, keeper_id, "metadata_merged", f"from file {donor_id}")
    conn.commit()
    print(
        f"apply complete: {copied} copied, {skipped} skipped, {held} still held for review, "
        f"{errors} errors."
    )
    if errors:
        print("Errored files keep status='error' (durable) - fix the source and clear the row.")
    if held:
        print("Held files: fill in decisions.csv and run apply again.")
```

6. **Run the new tests** — `uv run pytest -q tests/test_apply.py tests/test_xmp.py`.
   Expected: `7 passed` (4 new + 3 xmp).

7. **Run the whole suite** — `uv run pytest -q`. Expected: the baseline count plus 4
   (`160 passed, 1 skipped` if the baseline was 156/1). Nothing else may fail:
   `test_pipeline.py` exercises `apply` end-to-end and must stay green.

8. **Lint & format** — `uv run ruff check src tests && uv run ruff format src tests`.
   Expected: `All checks passed!` and `N files left unchanged` (or reformat + re-run
   `ruff check` until clean).

9. **Commit:**

```
git add src/photoflow/apply.py src/photoflow/exiftool.py src/photoflow/xmp.py \
        tests/test_apply.py tests/test_xmp.py
git commit -m "fix(apply): atomic copies, per-file error isolation, mtime-preserving XMP

- copy via <dest>.part + os.replace; a truncated pre-existing dest is re-copied
- per-file try/except OSError -> status='error' + copy_error audit, run continues
- flush queued XMP provenance before every commit so an interrupt can't orphan it
- exiftool_apply_argfile returns ExiftoolResult (rc/stderr/stdout) instead of
  swallowing failures; apply prints and audits xmp_embed_errors
- embed_args/merge_metadata pass -P so library mtime = source mtime (HANDOFF 2.1)
- --dry-run no longer mkdirs the destination tree"
```

---

### Task B2: sidecar policy — stop copying `.thm/.aae/.xmp` into the library

**Files:**
* `src/photoflow/config.py:33` (add `copy_sidecars` right after `sidecar_ext`)
* `src/photoflow/models.py:7-8` (comment only — see step 3)
* `src/photoflow/apply.py` (`cmd_apply`, new first branch in the row loop)
* `tests/conftest.py:78-117` (`photo_fixture`: add `IMG_0001.jpg`+`IMG_0001.THM` and
  `mountain.xmp`)
* `tests/test_pipeline.py:26-31` and `:57-66` (role count + library assertions)
* `tests/test_config.py` (new default assertion)
* `tests/test_apply.py` (two new tests)
* `README.md:53-86` (one bullet in "What it does")

**Recommended agent:** sonnet — mechanical once the fixture ripple is understood, but the
fixture change moves a count that `test_pipeline.py` asserts (see step 2).

**Depends on:** B1 (both edit `apply.py`'s row loop).

#### Steps

1. **Write the failing tests.** Append to `tests/test_apply.py`:

```python
def test_sidecars_are_not_copied_into_the_library(photo_fixture: Path, tmp_path: Path):
    """.thm/.aae/.xmp are not photos: copying them littered the library with standalone
    'assets', each with its own bogus .thm.xmp provenance sidecar (review finding H5)."""
    work, lib = tmp_path / "work", tmp_path / "library"
    pf(work, "scan", str(photo_fixture))
    pf(work, "plan")
    pf(work, "apply", "--out", str(lib))

    names = [p.name.lower() for p in lib.rglob("*") if p.is_file()]
    assert not [n for n in names if n.endswith(".thm")]
    assert not [n for n in names if n.endswith(".thm.xmp")]
    # the only .xmp left is the provenance sidecar apply writes for the RAW keeper
    assert [n for n in names if n.endswith(".xmp")] == [
        n for n in names if n.endswith(".dng.xmp")
    ]
    statuses = {
        Path(r["source_path"]).name: r["status"]
        for r in q(work, "SELECT source_path, status FROM files WHERE kind='sidecar'")
    }
    assert set(statuses.values()) == {"skipped_sidecar"}
    assert "IMG_0001.THM" in statuses and "mountain.xmp" in statuses


def test_copy_sidecars_true_restores_old_behaviour(photo_fixture: Path, tmp_path: Path):
    work, lib = tmp_path / "work", tmp_path / "library"
    work.mkdir(parents=True)
    (work / "photoflow.toml").write_text("copy_sidecars = true\n", encoding="utf-8")
    pf(work, "scan", str(photo_fixture))
    pf(work, "plan")
    pf(work, "apply", "--out", str(lib))

    names = [p.name.lower() for p in lib.rglob("*") if p.is_file()]
    assert [n for n in names if n.endswith(".thm")]
    statuses = {r["status"] for r in q(work, "SELECT status FROM files WHERE kind='sidecar'")}
    assert statuses == {"copied"}
```

   And to `tests/test_config.py`, inside `test_defaults_match_legacy_constants`, add:

```python
    assert c.copy_sidecars is False  # sidecars are not photos (review finding H5)
```

2. **Extend the fixture.** In `tests/conftest.py`, insert before the final `return src`
   (after the burst trio block at line 108-116):

```python
    # camera thumbnail sidecar next to its JPEG (Canon writes .THM beside .CRW/.JPG)
    _gradient(640, 480, seed=7).save(rnd / "IMG_0001.jpg", "JPEG", quality=92)
    _gradient(160, 120, seed=8).save(rnd / "IMG_0001.THM", "JPEG", quality=70)

    # pre-existing Lightroom/Capture One sidecar next to the RAW
    (old / "mountain.xmp").write_text(
        '<?xpacket begin=""?><x:xmpmeta xmlns:x="adobe:ns:meta/"/><?xpacket end="w"?>',
        encoding="utf-8",
    )
```

   Then fix the counts the fixture change moves, in `tests/test_pipeline.py`:
   * line 28: `assert roles.get("raw_jpeg_pair") == 2` → `== 3` with a comment
     `# mountain.jpg + mountain.dng + mountain.xmp (same stem, same folder)`.
   * after line 66, add the sidecar assertions:

```python
    assert not any(f.lower().endswith(".thm") for f in files)  # sidecars never copied
    assert not any(f.lower().endswith(".thm.xmp") for f in files)
```

   Nothing else moves: `IMG_0001.jpg` has no EXIF and no date-shaped name, so
   `date_sources["exif"] == 5` and `date_sources["filename"] == 1` still hold, and seeds
   7/8 are far from every other fixture hash so `review == 2` is unchanged.

3. **Run and watch fail** —
   `uv run pytest -q tests/test_apply.py tests/test_config.py tests/test_pipeline.py`.
   Expected: `test_sidecars_are_not_copied_into_the_library` fails on
   `assert not [n for n in names if n.endswith(".thm")]`,
   `test_copy_sidecars_true_restores_old_behaviour` fails with
   `SystemExit: photoflow.toml: unknown key 'copy_sidecars'`,
   `test_defaults_match_legacy_constants` fails with `AttributeError: copy_sidecars`.

4. **`config.py`:** add the field immediately after `sidecar_ext` (line 33):

```python
    sidecar_ext: frozenset[str] = frozenset({".xmp", ".aae", ".thm"})
    # Sidecars (.thm thumbnails, .aae edit lists, foreign .xmp) are metadata about a
    # photo, not a photo. Default False: they stay in the manifest for dedupe/audit but
    # are never copied. Set true to restore the pre-2026-08 behaviour.
    copy_sidecars: bool = False
```

5. **`models.py`:** documentation only — there is no full status vocabulary in this repo,
   only `DURABLE_STATUSES`, and `skipped_sidecar` must **not** join it (it has to be
   recomputable). Replace line 8 with:

```python
# Lifecycle statuses: scanned | planned | review | copied | skipped_dupe |
# skipped_manual | skipped_sidecar | error. Only these three survive a re-plan;
# everything else (incl. skipped_sidecar) is reset to 'scanned' by cmd_plan.
DURABLE_STATUSES = frozenset({"copied", "error", "skipped_manual"})  # HANDOFF §2.4
```

   (Verified: `planner.py:20-21` resets `status='scanned'` `WHERE status NOT IN
   ('copied','error','skipped_manual')`, so a `skipped_sidecar` row becomes `scanned` →
   `planned` on the next plan and is skipped again by apply — or copied, if
   `copy_sidecars` was turned on in the meantime. That is the intended behaviour.)

6. **`apply.py`:** add as the *first* branch inside `for r in rows:` (immediately above
   `role = r["role"]`), so no sidecar is copied regardless of its role:

```python
        if not cfg.copy_sidecars and r["kind"] == "sidecar":
            conn.execute("UPDATE files SET status='skipped_sidecar' WHERE id=?", (r["id"],))
            log_action(conn, log_fh, run_id, r["id"], "skipped_sidecar", "")
            skipped += 1
            continue
```

7. **README** (same commit), add to the "What it does" list after the "RAW+JPEG pairs"
   bullet (line 66):

```markdown
- **Sidecars are not photos** — `.thm` camera thumbnails, `.aae` edit lists and
  pre-existing `.xmp` files are fingerprinted into the manifest (so they still
  count for dedupe and audit) but never copied into the library; their status
  becomes `skipped_sidecar`. Set `copy_sidecars = true` in `photoflow.toml` to
  copy them anyway. If an earlier run already copied some, evict them with
  `photoflow prune-sidecars --out <DIR>` (below).
```

8. **Run tests** — `uv run pytest -q`. Expected: baseline + 6 new tests, all passing,
   0 failures.

9. **Lint & format** — `uv run ruff check src tests && uv run ruff format src tests`.

10. **Commit:**

```
git add src/photoflow/apply.py src/photoflow/config.py src/photoflow/models.py \
        tests/conftest.py tests/test_apply.py tests/test_config.py \
        tests/test_pipeline.py README.md
git commit -m "feat(apply): skip sidecar files by default (copy_sidecars=false)

.thm/.aae/.xmp rows get status='skipped_sidecar' + an audit row instead of being
copied as standalone library assets (each of which also got a bogus .thm.xmp).
Non-durable status: plan resets it like skipped_dupe. copy_sidecars=true in
photoflow.toml restores the old behaviour."
```

---

### Task B3: `prune-sidecars --out DIR [--dry-run]` — evict already-copied sidecars

**Files:**
* `src/photoflow/prune.py` (new)
* `src/photoflow/cli.py:47-52` (new subparser) and `:82-88` (dispatch dict)
* `tests/test_prune.py` (new)

**Recommended agent:** sonnet — self-contained new command with a clear contract.

**Depends on:** B2 (`skipped_sidecar` status semantics).

#### Steps

1. **Write the failing test.** Create `tests/test_prune.py`:

```python
"""prune-sidecars: move already-copied sidecars out of the library (never delete).

No exiftool needed: the manifest rows and library files are built directly.
"""

from pathlib import Path

from photoflow.config import Config
from photoflow.db import new_run, open_db
from photoflow.prune import cmd_prune_sidecars


class Args:
    def __init__(self, out, dry_run=False):
        self.out = str(out)
        self.dry_run = dry_run


def _fixture(tmp_path: Path):
    """Two copied .thm rows, one with a provenance .xmp beside it, one already gone."""
    work, lib = tmp_path / "work", tmp_path / "lib"
    conn = open_db(work)
    dests = []
    for n, has_sidecar in ((1, True), (2, False)):
        d = lib / "2003" / "11" / f"20031116_15582{n}_CRW-016{n}_abcd000{n}.thm"
        d.parent.mkdir(parents=True, exist_ok=True)
        d.write_bytes(b"thumbnail-bytes")
        if has_sidecar:
            d.with_name(d.name + ".xmp").write_text("<xmp/>", encoding="utf-8")
        conn.execute(
            "INSERT INTO files(source_path, kind, ext, status, dest_path) VALUES (?,?,?,?,?)",
            (str(tmp_path / f"src{n}.thm"), "sidecar", ".thm", "copied", str(d)),
        )
        dests.append(d)
    # a copied JPEG must be left completely alone
    keep = lib / "2003" / "11" / "20031116_155830_photo_ffff0000.jpg"
    keep.write_bytes(b"\xff\xd8jpeg")
    conn.execute(
        "INSERT INTO files(source_path, kind, ext, status, dest_path) VALUES (?,?,?,?,?)",
        (str(tmp_path / "src3.jpg"), "image", ".jpg", "copied", str(keep)),
    )
    conn.commit()
    return conn, work, lib, dests, keep


def _run(conn, work, lib, dry_run=False):
    run_id = new_run(conn, "prune-sidecars", {})
    (work / "logs").mkdir(exist_ok=True)
    with open(work / "logs" / f"test_{run_id}.jsonl", "a", encoding="utf-8") as fh:
        cmd_prune_sidecars(conn, work, run_id, fh, Args(lib, dry_run), Config())


def test_prune_moves_sidecars_and_updates_the_manifest(tmp_path: Path):
    conn, work, lib, dests, keep = _fixture(tmp_path)
    _run(conn, work, lib)

    pruned = work / "pruned" / "2003" / "11"
    for d in dests:
        assert not d.exists()
        assert (pruned / d.name).read_bytes() == b"thumbnail-bytes"
    assert (pruned / (dests[0].name + ".xmp")).exists()  # its provenance sidecar came too
    assert keep.exists()  # the JPEG is untouched

    rows = conn.execute("SELECT status, dest_path FROM files WHERE kind='sidecar'").fetchall()
    assert {r["status"] for r in rows} == {"skipped_sidecar"}
    assert all(r["dest_path"] is None for r in rows)
    actions = [r["action"] for r in conn.execute("SELECT action FROM actions")]
    assert actions.count("pruned_sidecar") == 3  # 2 thumbs + 1 sidecar


def test_prune_dry_run_changes_nothing(tmp_path: Path, capsys):
    conn, work, lib, dests, keep = _fixture(tmp_path)
    _run(conn, work, lib, dry_run=True)

    assert all(d.exists() for d in dests)
    assert not (work / "pruned").exists()
    rows = conn.execute("SELECT status FROM files WHERE kind='sidecar'").fetchall()
    assert {r["status"] for r in rows} == {"copied"}
    assert "DRY" in capsys.readouterr().out


def test_prune_reports_missing_files_but_still_clears_the_row(tmp_path: Path):
    conn, work, lib, dests, keep = _fixture(tmp_path)
    dests[1].unlink()  # someone already deleted it by hand
    _run(conn, work, lib)

    row = conn.execute(
        "SELECT status, dest_path FROM files WHERE dest_path IS NULL AND kind='sidecar'"
    ).fetchall()
    assert len(row) == 2  # both rows cleared, including the vanished one
```

2. **Run and watch fail** — `uv run pytest -q tests/test_prune.py`. Expected:
   `ModuleNotFoundError: No module named 'photoflow.prune'` (collection error, 3 errors).

3. **Create `src/photoflow/prune.py`:**

```python
"""prune-sidecars: move sidecar files an earlier apply copied out of the library.

Never deletes. Files are moved to workdir/pruned/<path relative to --out> so the
owner can inspect (and restore) them; the manifest row goes back to
status='skipped_sidecar' with dest_path cleared, which is exactly the state a
fresh apply would have produced under copy_sidecars=false.
"""

from __future__ import annotations

import shutil
from pathlib import Path

from photoflow.audit import log_action


def _free_path(p: Path) -> Path:
    """Never overwrite something already sitting in the pruned tree."""
    if not p.exists():
        return p
    for n in range(1, 1000):
        cand = p.with_name(f"{p.stem}_{n}{p.suffix}")
        if not cand.exists():
            return cand
    raise OSError(f"no free name for {p}")


def cmd_prune_sidecars(conn, workdir, run_id, log_fh, args, cfg):
    out_root = Path(args.out).expanduser().resolve()
    pruned_root = workdir / "pruned"
    rows = conn.execute(
        "SELECT id, dest_path FROM files "
        "WHERE status='copied' AND kind='sidecar' AND dest_path IS NOT NULL"
    ).fetchall()
    if not rows:
        print("no copied sidecars in the library - nothing to prune.")
        return

    pruned = missing = outside = 0
    for r in rows:
        dest = Path(r["dest_path"])
        try:
            rel = dest.resolve().relative_to(out_root)
        except ValueError:
            print(f"  not under --out, leaving alone: {dest}")
            outside += 1
            continue
        moves = [(dest, pruned_root / rel)]
        sidecar = dest.with_name(dest.name + ".xmp")  # the provenance .xmp apply wrote
        if sidecar.exists():
            moves.append((sidecar, pruned_root / rel.parent / sidecar.name))

        if args.dry_run:
            for old, new in moves:
                print(f"DRY  {old}  ->  {new}")
            pruned += 1
            continue

        for old, new in moves:
            if not old.exists():
                print(f"  already gone: {old}")
                missing += 1
                continue
            new = _free_path(new)
            new.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(old), str(new))  # move, not replace: workdir may be another drive
            log_action(conn, log_fh, run_id, r["id"], "pruned_sidecar", f"{old} -> {new}")
        conn.execute(
            "UPDATE files SET status='skipped_sidecar', dest_path=NULL WHERE id=?", (r["id"],)
        )
        pruned += 1
    conn.commit()

    verb = "would prune" if args.dry_run else "pruned"
    print(f"{verb} {pruned} sidecar(s); {missing} already gone, {outside} outside --out.")
    if not args.dry_run and pruned:
        print(f"moved into {pruned_root} (nothing was deleted).")
        print("Immich will see these as removed assets; rescan the external library.")
```

4. **Wire the CLI.** In `src/photoflow/cli.py`, add the import next to the others
   (`from photoflow.prune import cmd_prune_sidecars`), add the subparser after the
   `apply` parser block (line 50):

```python
    p = sub.add_parser(
        "prune-sidecars", help="move already-copied sidecar files out of the library"
    )
    p.add_argument("--out", required=True, help="output library root")
    p.add_argument("--dry-run", action="store_true")
```

   and add the dispatch entry to the dict at line 82-88:

```python
            "prune-sidecars": cmd_prune_sidecars,
```

5. **Run tests** — `uv run pytest -q tests/test_prune.py`. Expected: `3 passed`.
   Then `uv run pytest -q` for the full suite: baseline + 9, 0 failures.

6. **Smoke-test the CLI against a throwaway workdir** (never `photoflow_work/`):
   `uv run photoflow --workdir "$TMPDIR/pf" prune-sidecars --out "$TMPDIR/lib" --dry-run`
   → prints `no copied sidecars in the library - nothing to prune.` and exits 0.

7. **Lint & format** — `uv run ruff check src tests && uv run ruff format src tests`.

8. **Commit:**

```
git add src/photoflow/prune.py src/photoflow/cli.py tests/test_prune.py
git commit -m "feat(prune-sidecars): move already-copied sidecars out of the library

One-off repair for libraries built before copy_sidecars existed. Copied
kind='sidecar' rows (and the .xmp provenance file apply wrote next to them) are
MOVED to workdir/pruned/<relative path> - never deleted - and the manifest row
returns to skipped_sidecar/dest_path=NULL. --dry-run prints the moves only."
```

---

### Task B4: review page — lock in-library keepers so Enter never imports a near-dupe

**Files:**
* `src/photoflow/review_page.py:25-49` (`suggested_keeper_id`, `decision_rows`),
  `:66-108` (`build_payload`), `:181-191` (the keyboard hint paragraph),
  `:247-289` (`build`), `:291-315` (`acceptSuggested`), `:317-339` (`clickKeep`),
  `:384-415` (`refresh`), `:422-450` (keydown handler)
* `tests/test_review_page.py:136-146` (rewrite), `:221-263` (rewrite), plus 3 new tests
* `tests/test_review.py` (one new test)

**Recommended agent:** opus — this is finding R3: a correctness bug that spans the Python
payload, the CSV contract and four JS handlers, and the existing JS behaviour tests encode
the *old* (buggy) semantics, so they must be rewritten deliberately rather than patched.

**Depends on:** nothing (independent of B1–B3; can run in parallel).

#### Background you must read first

`plan` re-queues an already-copied keeper into a review group when a new near-dupe of it
turns up (`role='review'`, `status='copied'`). Today the page lets you decide such a
member, but `apply` only processes `status IN ('planned','review')` — so a "skip" on it is
silently ignored, and `acceptSuggested` on that group can end with **two** keepers (the
carried-forward copied one plus the new suggestion), which imports the near-dupe despite
the button reading "keep the suggested, skip the rest".

Fix: a `status == 'copied'` member is a **locked keeper**. It is always `keep` in the CSV,
it is not clickable, and `acceptSuggested` on a group containing one skips every other
undecided member (safe default: never import a look-alike silently). Keeping an extra
member is still possible — click its Keep explicitly — and the header then says
`N keepers`.

#### Steps

1. **Write the failing tests.** In `tests/test_review_page.py`:

   a. Add this helper right below the `g()` factory (after line 30):

```python
def locked_groups():
    """A group pairing an already-copied keeper with a newly imported look-alike."""
    return {7: [g(id=1, status="copied"), g(id=2, status="review", width=10, height=10)]}
```

   b. Replace `test_payload_flags_members_already_in_library` (lines 136-146) with:

```python
def test_payload_marks_copied_members_locked():
    """A member already copied into the library is a locked keeper: apply cannot act on
    it (it only processes planned/review), so the page must not offer a decision."""
    groups = locked_groups()
    rows = decision_rows(groups, {"1": {"decision": "keep", "merge_from_file_id": ""}})
    f1, f2 = build_payload(groups, rows, "w", set())["groups"][0]["files"]
    assert f1["locked"] is True and f2["locked"] is False
    # members without a status column (older callers / fixtures) are never locked
    plain = build_payload(GROUPS, decision_rows(GROUPS, {}), "w", set())
    assert plain["groups"][0]["files"][0]["locked"] is False


def test_decision_rows_lock_copied_members_against_the_csv():
    """A stale 'skip' in decisions.csv must never flip a locked keeper (it would read as
    a decision that apply silently ignores)."""
    rows = decision_rows(locked_groups(), {"1": {"decision": "skip", "merge_from_file_id": ""}})
    assert rows[0]["decision"] == "keep"
    assert rows[1]["decision"] == ""  # the new member is still undecided
```

   c. Replace `test_page_js_group_with_carried_forward_keeper_is_not_decided`
      (lines 221-263) with:

```python
def test_page_js_locked_library_keeper_is_not_editable():
    """R3: the group is undecided until the NEW member is decided; the locked keeper
    can't be clicked; Enter skips the new member instead of keeping it too."""
    groups = locked_groups()
    rows = decision_rows(groups, {"1": {"decision": "keep", "merge_from_file_id": ""}})
    page = render_page(build_payload(groups, rows, "w", set()))
    out = _run_page_js(
        page,
        """
        const out = {};
        out.progress = document.getElementById("progress").textContent;
        out.decidedClass = document.getElementById("g7").classList.contains("decided");
        out.state1 = document.getElementById("f1").querySelector(".state").textContent;
        out.state2 = document.getElementById("f2").querySelector(".state").textContent;
        document.getElementById("hide").onchange({ target: { checked: true } });
        out.displayWhenHiding = document.getElementById("g7").style.display;
        window.pf.keep(1);                       // locked: no-op
        out.afterClickLocked = { ...window.pf.dec };
        window.pf.accept(7);                     // Enter: new member SKIPS, never keeps
        out.afterAccept = { ...window.pf.dec };
        window.pf.keep(2);                       // explicit: keep it IN ADDITION
        out.afterKeep2 = { ...window.pf.dec };
        out.header = document.getElementById("g7").querySelector(".keepcount").textContent;
        window.pf.keep(2);                       // re-click un-keeps it again
        out.afterUnkeep2 = { ...window.pf.dec };
        console.log(JSON.stringify(out));
        """,
    )
    assert out["progress"] == "decided 0 / 1 groups"
    assert out["decidedClass"] is False
    assert out["state1"] == "KEEP · in library"
    assert out["state2"] == "on hold"
    assert out["displayWhenHiding"] == ""  # still visible under "hide decided"
    assert out["afterClickLocked"] == {"1": "keep", "2": ""}
    assert out["afterAccept"] == {"1": "keep", "2": "skip"}
    assert out["afterKeep2"] == {"1": "keep", "2": "keep"}
    assert out["header"] == "2 keepers"
    assert out["afterUnkeep2"] == {"1": "keep", "2": "skip"}


def test_page_js_keys_ignored_on_buttons():
    """A Tab-focused button must keep Enter for itself: blurring and continuing fired
    acceptSuggested when the user pressed Enter on 'Save'."""
    page = render_page(build_payload(GROUPS, decision_rows(GROUPS, {}), "w", set()))
    out = _run_page_js(
        page,
        """
        key("Enter", { tagName: "BUTTON" });
        console.log(JSON.stringify({ ...window.pf.dec }));
        """,
    )
    assert out == {"1": "", "2": ""}


def test_render_page_contains_locked_guards():
    """String-level pins for the JS guards (same style as the hardening test above)."""
    groups = locked_groups()
    page = render_page(build_payload(groups, decision_rows(groups, {}), "w", set()))
    assert "if (locked[id]) return;" in page  # clickKeep is a no-op on locked members
    assert 'includes(tag)) return;' in page  # keydown ignores BUTTON/INPUT/TEXTAREA/SELECT
    assert "keepcount" in page  # header advertises multiple keepers
    assert "disabled" in page  # locked cards render a disabled Keep button
```

   d. In `tests/test_review.py`, append (the module-level `pytest.mark.exiftool` applies;
      this test itself does not shell out, it drives `cmd_review` directly):

```python
def test_review_regeneration_relocks_copied_members(tmp_path: Path):
    """Invariant #4 says decisions carry forward - but a copied member's decision is not
    the user's to change, so a stale 'skip' for it is overridden back to 'keep'."""
    from photoflow.db import new_run, open_db
    from photoflow.review import cmd_review

    work = tmp_path / "work"
    conn = open_db(work)
    conn.executemany(
        "INSERT INTO files(source_path, kind, ext, role, status, group_id, width, height, size)"
        " VALUES (?,?,?,?,?,?,?,?,?)",
        [
            (str(tmp_path / "a.jpg"), "image", ".jpg", "review", "copied", 7, 4000, 3000, 10),
            (str(tmp_path / "b.jpg"), "image", ".jpg", "review", "review", 7, 1600, 1200, 10),
        ],
    )
    conn.commit()
    ids = [r["id"] for r in conn.execute("SELECT id FROM files ORDER BY id")]
    dec = work / "decisions.csv"
    dec.write_text(
        "group_id,file_id,source_path,resolution,size_kb,suggestion,decision,"
        "merge_from_file_id\n"
        f"7,{ids[0]},x,4000x3000,0,keep,skip,\n",
        encoding="utf-8",
    )
    run_id = new_run(conn, "review", {})
    (work / "logs").mkdir(exist_ok=True)
    with open(work / "logs" / "t.jsonl", "a", encoding="utf-8") as fh:
        cmd_review(conn, work, run_id, fh, None, None)

    with open(dec, newline="", encoding="utf-8") as f:
        rows = {r["file_id"]: r["decision"] for r in csv.DictReader(f)}
    assert rows[str(ids[0])] == "keep"  # locked: the CSV skip was overridden
    assert rows[str(ids[1])] == ""
```

2. **Run and watch fail** —
   `uv run pytest -q tests/test_review_page.py tests/test_review.py`. Expected 6 failures:
   the two payload/decision tests fail with `KeyError: 'locked'` / `assert '' == 'keep'`,
   the JS test fails on `afterClickLocked` (the click is honoured today), the BUTTON test
   fails because the handler blurs and continues, the guard-string test fails on the first
   `in page` assertion, and the `cmd_review` test fails with `assert 'skip' == 'keep'`.

3. **`review_page.py` — Python side.** Add the predicate below `suggested_keeper_id`
   (after line 27):

```python
def is_locked(member) -> bool:
    """True for a member already copied into the library.

    `apply` only processes status IN ('planned','review'), so any decision the user
    makes on a copied row is silently ignored. The page therefore renders it as a
    non-editable keeper and the CSV always says 'keep' for it. Rows without a status
    column (fixtures / older callers) are never locked.
    """
    return "status" in member.keys() and member["status"] == "copied"
```

   In `decision_rows`, replace the `rows.append({...})` block's decision cell:

```python
        for m in members:
            old = prior.get(str(m["id"]), {})
            # A locked member's decision is not the user's to change: override whatever
            # the CSV says (invariant #4 carries forward *user* decisions, and this is
            # not one) so apply and the page can never disagree.
            decision = "keep" if is_locked(m) else old.get("decision", "")
            rows.append(
                {
                    "group_id": gid,
                    "file_id": m["id"],
                    "source_path": m["source_path"],
                    "resolution": f"{m['width']}x{m['height']}",
                    "size_kb": round((m["size"] or 0) / 1024),
                    "suggestion": "keep" if m["id"] == best_id else "keep?",
                    "decision": decision,
                    "merge_from_file_id": old.get("merge_from_file_id", ""),
                }
            )
```

   In `build_payload`, replace the `"inLibrary": (...)` entry with:

```python
                    # already copied in an earlier round: a keeper the page may not edit
                    "locked": is_locked(m),
```

4. **`review_page.py` — JS side.** Five minimal edits inside `PAGE_TEMPLATE`
   (keep every line ≤ 100 chars: `review_page.py` is not in ruff's E501 ignore list).

   a. `build()` — badge + Keep button. Replace the `const badge = ...` expression's
      second half and the Keep button line:

```javascript
      const badge = (f.kind === "raw" ? '<span class="badge raw">RAW</span>'
        : f.kind === "video" ? '<span class="badge video">VIDEO</span>' : "") +
        (f.locked ? '<span class="badge lib" ' +
          'title="already copied into the library in an earlier round">in library</span>' : "");
      const keepbtn = f.locked
        ? '<button class="keepbtn on" disabled title="already in the library">Keep</button>'
        : '<button class="keepbtn" onclick="pf.keep(' + f.id + ')">Keep</button>';
```

      and use `keepbtn` in the card string in place of the inline button:

```javascript
        '<div class="actions">' + keepbtn +
        '<button class="donate" onclick="pf.donate(' + f.id + ')" ' +
```

      In the group header (`div.innerHTML = ...`), add the keeper counter:

```javascript
    div.innerHTML = "<h3>group " + g.gid + " \\u00b7 " + g.files.length + " files " +
      '<span class="keepcount"></span>' +
      '<button class="acceptbtn" onclick="pf.accept(' + g.gid + ')" ' +
      'title="keep the suggested photo, skip the rest (Enter)">\\u2713 keep suggested</button>' +
      '</h3><div class="cards">' + cards + "</div>";
```

   b. State maps — extend the load loop (line 210-218) and the localStorage overlay so a
      stale overlay can't unlock a locked member:

```javascript
const byGid = {}, groupOf = {}, dec = {}, donorOf = {}, locked = {};
```

```javascript
  for (const f of g.files) {
    groupOf[f.id] = g.gid;
    locked[f.id] = !!f.locked;
    dec[f.id] = f.locked ? "keep" : norm(f.decision);
    if (dec[f.id] === "keep" && f.merge) donorOf[g.gid] = Number(f.merge);
  }
```

```javascript
    for (const [id, d] of Object.entries(saved.dec || {}))
      if (id in dec && !locked[id]) { dec[id] = norm(d); restored = true; }
```

   c. `acceptSuggested` — the safe default:

```javascript
function acceptSuggested(gid) {  // keep the suggested member (rest skip), then advance
  const g = byGid[gid];
  if (!groupDecided(g)) {
    if (g.files.some((f) => f.locked)) {
      // an in-library keeper already covers this group: never import a look-alike
      // silently - the new members skip unless the user keeps one explicitly
      g.files.forEach((f) => { if (!f.locked && !dec[f.id]) dec[f.id] = "skip"; });
      cur = gid;
      persist();
      refresh();
    } else {
      const s = g.files.find((f) => f.suggested) || g.files[0];
      clickKeep(s.id);
    }
  }
  setCursor(nextVisible(gid, +1), true);
}
```

   d. `clickKeep` — locked guard + restored undo branch:

```javascript
function clickKeep(id) {
  if (locked[id]) return;  // in-library keeper: apply can't act on it, so nor can you
  const gid = groupOf[id], mem = byGid[gid].files;
  cur = gid;  // Enter continues from the group you last touched
  if (dec[id] === "keep") {
    dec[id] = "";
    if (mem.some((f) => f.locked || dec[f.id] === "keep")) {
      dec[id] = "skip";  // another keeper remains; this one becomes a skip
    } else {
      mem.forEach((f) => (dec[f.id] = ""));  // zero keepers: whole group back to hold
      donorOf[gid] = null;
    }
  } else {
    dec[id] = "keep";  // keeps IN ADDITION to any locked keeper in this group
    mem.forEach((f) => { if (!f.locked && dec[f.id] !== "keep") dec[f.id] = "skip"; });
    if (donorOf[gid] === id) donorOf[gid] = null;
  }
  persist();
  refresh();
}
```

   e. `refresh()` — keeper counter and the locked state label. Inside the
      `for (const g of DATA.groups)` loop, after the `gdiv.style.display` line:

```javascript
    const keeps = g.files.filter((f) => dec[f.id] === "keep").length;
    gdiv.querySelector(".keepcount").textContent = keeps > 1 ? keeps + " keepers" : "";
```

      and in the per-file state text, swap `f.inLibrary` for `f.locked`:

```javascript
        : d === "keep" ? (f.locked ? "KEEP \\u00b7 in library" : "KEEP")
```

   f. keydown handler — replace lines 424-426 with:

```javascript
  const tag = (e.target && e.target.tagName) || "";
  if (["BUTTON", "INPUT", "TEXTAREA", "SELECT"].includes(tag)) return;
```

   g. The hint paragraph (lines 186-189) still describes the old confirm-by-clicking
      flow. Replace those four lines with:

```html
Untouched groups stay on hold. A member tagged <b>in library</b> was already copied in
an earlier round: it stays a keeper and can't be clicked. <kbd>Enter</kbd> on such a
group <b>skips</b> the new look-alikes — click <b>Keep</b> on one only if you really
want a second copy in the library. Click a thumbnail to open the
```

5. **Run the review tests** —
   `uv run pytest -q tests/test_review_page.py tests/test_review.py`.
   Expected: `20 passed` (15 existing minus 1 replaced, plus 5 new + 1 in test_review.py;
   exact count will read `20 passed` — if node is missing they skip instead).
   `test_page_js_fresh_group_click_semantics_unchanged` and
   `test_page_js_accept_suggested_and_cursor` must still pass **unchanged**: no group in
   `GROUPS`/`GROUPS3` has a `status`, so nothing is locked there.

6. **Run the whole suite** — `uv run pytest -q`. Expected 0 failures.

7. **Eyeball the page once** (optional but recommended): build a page in a throwaway
   workdir and open it —
   `uv run python -c "from photoflow.review_page import *; ..."` is fine, or run the
   existing `tests/test_review_page.py::test_page_js_locked_library_keeper_is_not_editable`
   with `-s`. Do **not** run `photoflow review` against `photoflow_work/`.

8. **Lint & format** — `uv run ruff check src tests && uv run ruff format src tests`.

9. **Commit:**

```
git add src/photoflow/review_page.py tests/test_review_page.py tests/test_review.py
git commit -m "fix(review): lock in-library keepers so Enter never imports a near-dupe

R3: a status='copied' member re-queued next to a new look-alike was editable in
the page, but apply only processes planned/review rows - so its 'skip' was
ignored and 'keep suggested' could end with two keepers, importing the near-dupe.

- decision_rows always writes 'keep' for a copied member and overrides a stale CSV
- build_payload marks it locked; the card renders a disabled Keep button
- acceptSuggested skips every new member of a group that has a locked keeper
- clickKeep no-ops on locked ids; re-clicking a non-locked keeper un-keeps it again
- the header shows 'N keepers' when a second copy is kept deliberately
- keydown ignores BUTTON/INPUT/TEXTAREA/SELECT instead of blurring and continuing"
```

---

### Task B5: documentation — resume rule, deps, Capture One, sidecars, repair runbook

**Files:**
* `README.md:17-22` (deps), `:53-86` (what it does), `:292-297` (files it manages),
  `:299-316` (tuning), `:318-324` (performance/resume), new sections after `:86`
* `HANDOFF.md:24-26` (§2.1 mtime sentence)
* `CLAUDE.md:33-40` (App subcommands paragraph)

**Recommended agent:** sonnet — prose only, but every flag name must be verified against
the code that actually landed.

**Depends on:** B1–B4 (flag names must be final). `refile` and `scan --refresh-meta` are
**Lane A** deliverables (T2/T3): before documenting them, run
`uv run photoflow --help` and `uv run photoflow scan --help` in the integration branch and
match the real names. If Lane A has not landed yet, still write the runbook exactly as
specified below (T2/T3 fix those names) and say so in the commit body.

#### Steps

1. **README — dependency wording** (lines 17-22). `pyproject.toml:6-8` makes `pillow-heif`
   a *hard* dependency of the package, which contradicts "the image extras are optional".
   Decision: say so. Replace the bullet with:

```markdown
3. Python deps: `uv sync` (installs the package plus dev/image deps). Without uv:
   `pip install -e .[images]` for the optional image extras.
   - `pillow-heif` is a **hard dependency** (declared in `pyproject.toml`), so
     HEIC thumbnails/phash always work.
   - Pillow/ImageHash are optional extras: without them you lose near-dupe
     flagging and review thumbnails (exact dedupe still works).
```

2. **README — resume rule** (replace the last sentence of "Performance notes",
   lines 322-324):

```markdown
**Resume rule.** A path already in the manifest is re-fingerprinted only when its
size or mtime changed (±1 s, for FAT/exFAT), *and* only rows that actually finished
hashing count as done — a row whose `content_hash` is still NULL is picked up again
on the next `scan`, so an interrupted scan cannot silently drop files. Metadata
reads are manifest-driven for the same reason. Nothing is re-hashed just because you
re-ran the command.
```

   (If Lane A's T4 has not landed, this describes the intended contract; T4 is the task
   that makes the NULL-hash half true. Note it in the commit body.)

3. **README — new section** after the "Audit trail" bullet (line 86), before
   `## Enrich`:

```markdown
## Capture One / Lightroom libraries

Point `scan` at the **catalog folder** (a Capture One managed catalog is a folder
containing `<name>.cocatalogdb` plus `Originals/`) or at the **session root**
(`Capture/`, `Selects/`, `Output/`, `CaptureOne/`). photoflow ingests the pixel
files it finds under them — `Originals/`, `Capture/`, exports in `Output/` — and
dedupes across all of it.

What it does **not** carry over: adjustments (`.cos` settings, the settings inside
an `.eip`), variants, ratings, colour tags and catalog keywords. Those live in the
catalog database, not in the image files; photoflow is a pixel organizer, so it
copies the pixels and writes its own provenance XMP.

`exclude_dirs` prunes the noise directories by default — `CaptureOne`, `Cache`,
`Proxies`, `Thumbnails`, `Trash`, `$RECYCLE.BIN`, `*.lrdata` preview trees,
`@eaDir`, `__MACOSX` — matched case-insensitively against every path component, so
a session's `Trash/` or a Lightroom previews tree is never ingested. Canon `.crw`,
Phase One `.iiq` and Capture One `.eip` are in the default `raw_ext` set.

Two caveats worth knowing: a RAW and its JPEG only pair when they share a stem in
the *same* folder (a `Capture/` RAW and its `Output/` export stay unlinked, both
kept), and `enrich` runs on images only — RAW and video files get no people or
content tags (their JPEG twins usually do).
```

   (`exclude_dirs`, `min_size_bytes` and the extended `raw_ext` are Lane A's T7. Verify
   `Config` actually has `exclude_dirs` before committing this paragraph; if T7 has not
   landed, keep the paragraph but drop the `exclude_dirs` sentence and note it.)

4. **README — new section** after that one (outer fence is `~~~` only so the inner
   command block survives; write it into README with normal triple backticks):

~~~markdown
## Repair runbook (existing library)

These commands change files **inside the library** (never sources) and all support
`--dry-run`. Run the dry run, read it, then run it for real.

```
photoflow scan --refresh-meta --kind video   # re-read metadata only, no re-hash
photoflow plan                               # recompute dates from the new metadata
photoflow refile --out "D:/Photos-Organized" --dry-run
photoflow refile --out "D:/Photos-Organized"          # move files to their new YYYY/MM
photoflow prune-sidecars --out "D:/Photos-Organized" --dry-run
photoflow prune-sidecars --out "D:/Photos-Organized"  # evict copied .thm/.aae/.xmp
```

`refile` moves a copied file (and its `.xmp` sidecar) when `plan` resolved a better
date for it; `prune-sidecars` moves already-copied sidecars into
`photoflow_work/pruned/` — neither ever deletes anything.

**Immich note.** Both commands change paths, so an Immich external library sees
removals and additions: rescan the library afterwards. Routine `apply` /
`enrich apply` runs no longer churn Immich — exiftool is called with `-P`, so the
library file's mtime keeps matching its source and unchanged files are not
re-indexed or re-uploaded by mtime-based backups.
~~~

5. **README — extension + tuning lists.** In "Files it manages" (line 292-297) add the new
   RAW extensions from Lane A T7 to the `RAW:` line and add a `Sidecars:` line:

```markdown
Sidecars: xmp aae thm (fingerprinted for dedupe/audit, never copied — see
`copy_sidecars`)
```

   In "Tuning" (line 309-316) add the new keys:

```markdown
- `copy_sidecars` (false) — copy `.thm/.aae/.xmp` files into the library too.
- `exclude_dirs` — directory names pruned during `scan`, matched
  case-insensitively per path component.
- `min_size_bytes` (0) — skip files smaller than this; `20000` is a sensible
  value for thumbnail-laden sources.
```

   (Drop the last two bullets if Lane A T7 has not landed.)

6. **HANDOFF.md §2.1** (line 24-26) — the "mtime preserved" claim is now true again but
   only because of `-P`; make that explicit:

```markdown
1. **Sources are read-only.** No code path may write to, move or delete a source
   file. Output is copy-only (`shutil.copy2` via a `.part` temp + `os.replace`).
   Library mtime equals source mtime: `copy2` preserves it and every exiftool write
   (`embed_args`, `merge_metadata`, `enrich apply`) passes `-P` so
   `-overwrite_original` cannot reset it. Moves *inside* the library (`refile`,
   `prune-sidecars`) are the only file relocations, and they never touch sources.
```

7. **CLAUDE.md** — update the "App subcommands" sentence (line 35) to:

```markdown
App subcommands: `scan <SRC> [SRC ...] [--refresh-meta]`, `plan`, `review`,
`apply --out <DIR> [--dry-run]`, `refile --out <DIR> [--dry-run]`,
`prune-sidecars --out <DIR> [--dry-run]`, `status`.
```

   and add to the invariants list, after item 6:

```markdown
7. **Sidecars (`.thm/.aae/.xmp`) are never copied** unless `copy_sidecars = true`;
   they stay in the manifest as `skipped_sidecar` (a non-durable status).
8. **Library mtime = source mtime** — every exiftool write passes `-P`.
```

8. **Verify every command name you wrote actually exists:**

```
uv run photoflow --help
uv run photoflow scan --help
uv run photoflow prune-sidecars --help
```

   Expected: the subcommand list matches the README/CLAUDE.md text exactly. Fix the prose,
   not the code, if they differ (except where a name is Lane A's and hasn't landed — then
   leave the doc and say so in the commit body).

9. **Run the suite** — `uv run pytest -q` (docs shouldn't move it, but CLAUDE.md/README are
   read by no test, so this is just a guard) and
   `uv run ruff check src tests && uv run ruff format src tests`.

10. **Commit:**

```
git add README.md HANDOFF.md CLAUDE.md
git commit -m "docs: resume rule, sidecar policy, Capture One libraries, repair runbook

- README: pillow-heif is a hard dep (was documented as optional); the resume rule
  now describes size+mtime AND content_hash; new 'Capture One / Lightroom
  libraries' and 'Repair runbook' sections (refile / prune-sidecars, Immich
  rescan note, -P keeps mtimes so routine runs no longer churn Immich); RAW and
  video are not enriched; copy_sidecars / exclude_dirs / min_size_bytes tuning keys
- HANDOFF 2.1: the mtime claim is true again only because every exiftool write
  passes -P; spell that out and note the two in-library move commands
- CLAUDE.md: refile, prune-sidecars and scan --refresh-meta in the command list"
```

---

## Verification checklist (run before declaring Lane B done)

```
uv run pytest -q                      # baseline + ~15 new tests, 0 failures
uv run ruff check src tests           # All checks passed!
uv run ruff format --check src tests  # N files already formatted
git log --oneline feature/enrich..HEAD  # exactly 5 commits, one per task
```

Then confirm by hand, in a throwaway workdir only:

```
uv run photoflow --workdir /tmp/pf-smoke status
uv run photoflow --workdir /tmp/pf-smoke prune-sidecars --out /tmp/pf-lib --dry-run
```

Never point any of these at the repo's `photoflow_work/`.
