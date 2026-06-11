# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

**photoflow** — an incremental, non-destructive photo library organizer. It scans messy source folders, fingerprints files into a SQLite manifest (`photoflow_work/photoflow.db`), dedupes by content hash, flags near-dupes for human review, resolves capture dates via a cascade (EXIF → filename → folder year → mtime), and copies keepers into a `YYYY/MM/YYYYMMDD_HHMMSS_<slug>_<hash8>.ext` library with provenance written as Dublin Core XMP.

The repo is mid-migration: `photoflow.py` is the working single-file reference implementation (~830 lines, the behavioral spec), and **`HANDOFF.md` is the authoritative migration plan** for decomposing it into a `src/photoflow/` package. Read HANDOFF.md before any structural work — it defines the phased plan (§4), target layout (§3), test strategy (§5), and acceptance criteria (§9). When HANDOFF.md and the code disagree about behavior, the code wins.

## Commands

Current (single-file stage):

```
python photoflow.py scan <SRC> [SRC ...]   # fingerprint sources into manifest
python photoflow.py plan                   # recompute roles/groups/dates
python photoflow.py review                 # emit review.html + decisions.csv
python photoflow.py apply --out <DIR> [--dry-run]
python photoflow.py status
```

All state lives in `--workdir` (default `./photoflow_work`). Requires Python 3.11+, exiftool on PATH (hard requirement — scan exits without it). Pillow/ImageHash/pillow-heif are optional; without them, near-dupe flagging degrades gracefully to exact dedupe only.

Target tooling (per HANDOFF.md, once scaffolded): `uv` for env/deps, `pytest` for tests, `ruff` for lint+format, entry point `python -m photoflow <cmd>`. Pure-logic tests (dates, bktree, naming) must run without exiftool; mark exiftool-dependent tests `@pytest.mark.exiftool` and skip if absent.

## Non-negotiable invariants

HANDOFF.md §2 is the full list — preserve these in any change. The most violation-prone:

1. **Sources are read-only.** Output is copy-only (`shutil.copy2`). No code path writes to, moves, or deletes a source file.
2. **Near-dupes are never auto-deleted** — perceptual-hash matches only ever flag for manual review. Only exact content-hash matches are auto-skipped.
3. **`plan` recomputes role/group_id/dupe_of for ALL files every run.** Only statuses `copied`, `error`, `skipped_manual` are durable. Resetting only non-copied files was a real bug (HANDOFF.md §7) — keep its regression test.
4. **`decisions.csv` regeneration carries forward existing decisions by file_id.**
5. **Naming/dest scheme is stable** — changing it orphans existing libraries.
6. **XMP stays generic Dublin Core**; embed only for jpg/jpeg/png/tif/tiff/heic/heif, sidecar `.xmp` for RAW and video.

## Architecture (single file, by section)

`photoflow.py` is organized in commented sections that map 1:1 to the planned package modules:

- **DB** — `files` table is the manifest; `role` (keep/exact_dupe/raw_jpeg_pair/live_pair/burst/review) and `group_id`/`dupe_of` are *derived* state recomputed by plan; `status` (scanned/planned/review/copied/skipped_dupe/skipped_manual/error) is the durable lifecycle. `runs` + `actions` tables plus per-run JSONL in `logs/` form the audit trail — every file action is logged with a reason.
- **exiftool wrapper** — batched via argfiles (`-@ file`, `-execute` blocks) to dodge Windows command-length limits; metadata merge uses `-wm cg` (fill-missing-only, never overwrites keeper tags). All JSON reads must use `.get()` — exiftool omits absent tags.
- **Dedupe pipeline (plan)** — exact groups by BLAKE2b hash (keeper preference: already-copied > earliest mtime, which keeps incremental imports stable); RAW+JPEG and Live Photo pairs by same-stem-same-dir (pair members excluded from near-dupe components); near-dupe components via BK-tree over pHash ints + union-find; burst test (all members have EXIF time, one camera model, gaps ≤ 10 s) keeps groups silently instead of queueing review.
- **Date resolution** — regex cascade with year sanity window (`MIN_YEAR`–now+1); defensive parsing because wild EXIF contains `0000:00:00` and garbage. Source + confidence recorded per file; dateless files go to `unknown-date/`.
- **scan skip rule** — path already in manifest with same size and mtime ±1 s (FAT/exFAT tolerance) is not re-fingerprinted.

Tuning constants at the top of the file: `NEAR_DUPE_THRESHOLD`, `BURST_WINDOW_S`, `MIN_YEAR`, `SLUG_MAX`, `EXIFTOOL_BATCH`.

## Migration ground rules

- Work phase-by-phase per HANDOFF.md §4; commit per phase. Phase 0 keeps `photoflow.py` as `src/photoflow/_reference.py` untouched; delete it only after the §5 integration test passes against the new package.
- Don't start backlog items (HANDOFF.md §8) until Phase 3's integration test is green.
- Test fixtures are generated synthetically with Pillow (no binary assets in repo); fake RAW files must not be byte-identical to their JPEG or they collapse into exact dupes.
- Keep HANDOFF.md updated as decisions are made.
