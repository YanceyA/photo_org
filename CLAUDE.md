# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

**photoflow** — an incremental, non-destructive photo library organizer. It scans messy source folders, fingerprints files into a SQLite manifest (`photoflow_work/photoflow.db`), dedupes by content hash, flags near-dupes for human review, resolves capture dates via a cascade (EXIF → filename → folder year → mtime), and copies keepers into a `YYYY/MM/YYYYMMDD_HHMMSS_<slug>_<hash8>.ext` library with provenance written as Dublin Core XMP.

The package under `src/photoflow/` **is** the implementation — the single-file reference (`photoflow.py`) was deleted after a parity check against the integration suite. `HANDOFF.md` remains the historical spec + invariants document; `docs/plans/` holds the executed refactor plan. When HANDOFF.md and the code disagree about behavior, the code wins.

## Commands

```
uv run pytest                              # full suite (integration tests need exiftool on PATH)
uv run ruff check src tests                # lint
uv run ruff format src tests               # format
uv run photoflow <cmd>                     # or: python -m photoflow <cmd>
just test / just lint / just fmt / just run <cmd>
```

App subcommands: `scan <SRC> [SRC ...] [--refresh-meta [PREFIX ...]] [--kind K]`, `plan`, `review`, `apply --out <DIR> [--dry-run]`, `refile --out <DIR> [--dry-run]`, `prune-sidecars --out <DIR> [--dry-run]`, `status`. Plus the optional **enrich** subsystem (nested): `enrich scan|cluster|assign|merge|review|apply|status` — faces (InsightFace→HDBSCAN) + content tags (RAM++/CLIP) written into the *already-copied* library as portable XMP. (`assign` = centroid label-propagation of named people onto unassigned faces; `merge` = fold duplicate/misspelled person names into one.) Gated behind `pip install -e .[enrich]`; degrades gracefully when the model stack is absent. Design: `docs/plans/2026-06-13-enrich-design.md`.

All state lives in `--workdir` (default `./photoflow_work`). Requires Python 3.11+, exiftool on PATH (hard requirement — scan exits without it). Pillow/ImageHash/pillow-heif are optional; without them, near-dupe flagging degrades gracefully to exact dedupe only. Pure-logic tests (dates, bktree, naming, hashing) run without exiftool; exiftool-dependent tests are marked `@pytest.mark.exiftool` and skip if it's absent. Enrich pure-logic tests (clustering, regions, page, thresholds) run in CI via numpy+scikit-learn; model-dependent tests are marked `@pytest.mark.enrich` and skip when the [enrich] stack is absent.

Tuning: drop a `photoflow.toml` in the workdir to override defaults (`near_dupe_threshold`, `burst_window_s`, `min_year`, `slug_max`, `exiftool_batch`, extension sets). Constants/defaults live in `src/photoflow/config.py`.

## Non-negotiable invariants

HANDOFF.md §2 is the full list — preserve these in any change. The most violation-prone:

1. **Sources are read-only.** Output is copy-only (`shutil.copy2`). No code path writes to, moves, or deletes a source file.
2. **Near-dupes are never auto-deleted** — perceptual-hash matches only ever flag for manual review. Only exact content-hash matches are auto-skipped.
3. **`plan` recomputes role/group_id/dupe_of for ALL files every run.** Only statuses `copied`, `error`, `skipped_manual` are durable. Resetting only non-copied files was a real bug (HANDOFF.md §7) — keep its regression test.
4. **`decisions.csv` regeneration carries forward existing decisions by file_id.**
5. **Naming/dest scheme is stable** — changing it orphans existing libraries.
6. **XMP stays generic Dublin Core**; embed only for jpg/jpeg/png/tif/tiff/heic/heif, sidecar `.xmp` for RAW and video.
7. **Sidecars (`.thm/.aae/.xmp`) are never copied** unless `copy_sidecars = true`; they stay in the manifest as `skipped_sidecar` (a non-durable status). `refile`/`prune-sidecars` are the only moves of files already *inside* the library — both never touch sources, and `prune-sidecars` only ever relocates into `workdir/pruned/`, never deletes.
8. **Library mtime = source mtime** — every exiftool write passes `-P`, so `-overwrite_original` can't reset the copy2-preserved mtime.

## Architecture (module map)

`src/photoflow/`:

- **`cli.py`** — argparse dispatcher; opens the DB, starts a run, and calls the command module. The command modules (`scan.py`, `planner.py`, `review.py`, `apply.py`, `refile.py`, `prune.py`, `status.py`) share the signature `(conn, workdir, run_id, log_fh, args, cfg)`. `review.py` orchestrates decisions.csv + thumbnails + the interactive review.html (logic in `review_page.py`). `refile.py` (`refile` command) moves already-copied library files to the destination their current resolved date implies; `prune.py` (`prune-sidecars` command) moves already-copied sidecar files out of the library into `workdir/pruned/`. Both are in-library-only moves (never touch sources), support `--dry-run`, and abort the whole run on any destination collision.
- **Pure logic** — `dates.py` (resolution cascade), `naming.py` (slug + dest scheme), `bktree.py`, `hashing.py`, `review_page.py` (decisions.csv rows, JSON payload + HTML/JS template for the interactive review page). No I/O beyond the file being hashed; testable without exiftool.
- **Infra** — `db.py`, `audit.py`, `exiftool.py`, `xmp.py`.
- **`config.py`** — frozen `Config` dataclass with defaults + `photoflow.toml` loader (unknown keys are fatal).
- **`models.py`** — role/status vocabularies.
- **`enrich/`** — optional faces+tags subsystem, same command signature. Pure/CI-testable: `clustering.py` (HDBSCAN over embeddings), `regions.py` (MWG region geometry + keyword/subject argfile builders), `page.py` (faces.csv/tags.csv rows, payloads, the interactive `enrich_review.html`), `tagger.classify_tag`/vocab, `faces.face_crop`. Lazy heavy-import wrappers: `faces.py` (InsightFace), `tagger.py` (RAM++/CLIP), gated by `deps.py` (`HAVE_*`, device/provider selection — faces default to CPU). Commands: `enrich/{scan,cluster,assign,merge,review,apply,status}.py`. New DB tables `persons`/`faces`/`tags`/`enrich_state` (additive `IF NOT EXISTS`); `enrich_state` carries a per-file `applied_sig`/`errors` pair so `enrich apply` only rewrites files whose inputs actually changed (`--all` forces a full rewrite); a `tag_blacklist` table persists blacklisted tags across `enrich review`/`enrich apply` runs. **(Lane C — not yet landed on this branch as of this doc pass; verify table/column names once merged.)**

Key behaviors by area:

- **DB** — `files` table is the manifest; `role` (keep/exact_dupe/raw_jpeg_pair/live_pair/burst/review) and `group_id`/`dupe_of` are *derived* state recomputed by plan; `status` (scanned/planned/review/copied/skipped_dupe/skipped_manual/error) is the durable lifecycle. `runs` + `actions` tables plus per-run JSONL in `logs/` form the audit trail — every file action is logged with a reason.
- **exiftool wrapper** — batched via argfiles (`-@ file`, `-execute` blocks) to dodge Windows command-length limits; metadata merge uses `-wm cg` (fill-missing-only, never overwrites keeper tags). All JSON reads must use `.get()` — exiftool omits absent tags. Windows lesson: exiftool emits forward-slash `SourceFile` paths even on Windows — normalize before keying lookups (HANDOFF.md §7).
- **Dedupe pipeline (plan)** — exact groups by BLAKE2b hash (keeper preference: already-copied > earliest mtime, which keeps incremental imports stable); RAW+JPEG and Live Photo pairs by same-stem-same-dir (pair members excluded from near-dupe components); near-dupe components via BK-tree over pHash ints + union-find; burst test (all members have EXIF time, one camera model, gaps ≤ 10 s) keeps groups silently instead of queueing review.
- **Date resolution** — regex cascade with year sanity window (`min_year`–now+1); defensive parsing because wild EXIF contains `0000:00:00` and garbage. Source + confidence recorded per file; dateless files go to `unknown-date/`.
- **scan skip rule** — a path already in the manifest is re-fingerprinted only if its size or mtime (±1 s, FAT/exFAT tolerance) changed *or* its `content_hash` is still NULL (an interrupted prior scan), so incremental scans never silently drop a partially-processed file. `files.meta_read` tracks whether exiftool has read a row's metadata; `scan --refresh-meta [PREFIX ...] [--kind K]` re-reads metadata (no re-hash, no status change) for manifest rows under the given path-component prefixes, skipping durable `error`/`skipped_manual` rows. On first run after upgrading, existing rows with a content hash are marked `meta_read=1` (no surprise full re-read).

Test fixtures are generated synthetically with Pillow (no binary assets in repo); fake RAW files must not be byte-identical to their JPEG or they collapse into exact dupes.

## History

The package refactor was executed per `docs/plans/2026-06-11-photoflow-package-refactor.md` (the reference single file was deleted after passing the HANDOFF.md §5 integration test). HANDOFF.md §8 backlog is the future-work list.
