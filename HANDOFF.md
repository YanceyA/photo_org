# HANDOFF: photoflow — restructure prototype into a production repo

**Audience:** coding agent (e.g. Claude Code) working in a fresh repo.
**Inputs provided:** `photoflow.py` (working single-file reference implementation,
~830 lines, tested end-to-end) and `README.md` (user-facing docs).
**Goal:** decompose into a normal Python package architecture without changing
observable behavior, then build on it. The single file is the spec — when this
document and the code disagree about behavior, the code wins.

---

## 1. What this project is

An incremental, non-destructive photo library organizer. It scans messy source
folders, fingerprints every file into a SQLite manifest, dedupes by content,
resolves a best-effort capture date, and copies keepers into a
`YYYY/MM/YYYYMMDD_HHMMSS_<slug>_<hash8>.ext` library with provenance written as
generic XMP. Near-duplicates are never auto-deleted — they go to a human review
queue (HTML + CSV). Every action is logged to an actions table and JSONL files.

Pipeline stages map to CLI subcommands: `scan → plan → review → apply → status`.

## 2. Non-negotiable invariants (preserve in any refactor)

1. **Sources are read-only.** No code path may write to, move, or delete a
   source file. Output is copy-only (`shutil.copy2`, mtime preserved).
2. **Near-dupes are never auto-deleted.** Perceptual-hash matches only ever
   *flag* for manual review. Only exact content-hash matches are auto-skipped.
3. **Manual decisions are durable.** `skipped_manual` status survives re-plans;
   regenerating `decisions.csv` carries forward existing decisions by file_id.
4. **`plan` is idempotent and recomputes roles for ALL files every run.** Only
   statuses `copied`, `error`, `skipped_manual` are durable; role/group_id/
   dupe_of are derived state. (This was a real bug in development — see §7.)
5. **Incremental imports dedupe against already-copied content.** A keeper
   preference of `already-copied > earliest mtime` keeps the library stable.
6. **Every file gets an audit record** with action + reason (JSONL per run and
   `actions` table). Date resolution records its source (`exif|filename|folder|
   mtime`) and confidence per file.
7. **Naming/dest scheme is stable** (hash8 suffix guarantees uniqueness;
   dateless files go to `unknown-date/`). Changing it orphans existing
   libraries — gate behind a config/migration if ever needed.
8. **XMP stays generic Dublin Core** (`dc:subject` folder keywords,
   `dc:description` original paths). Embed for jpg/jpeg/png/tif/tiff/heic/heif;
   sidecar `.xmp` for RAW and video. Never embed into RAW or video containers.
9. **Bursts are unique, kept silently:** lookalike group where every member has
   EXIF time, same camera model, consecutive gaps ≤ `BURST_WINDOW_S`.
10. **RAW+JPEG same-stem-same-dir pairs: both kept.** Live Photo image+video
    same-stem pairs: both kept, video inherits the image's date if it lacks one.
11. **Graceful degradation:** missing Pillow/ImageHash → exact dedupe only,
    with a printed notice; missing exiftool → hard exit with install hint.

## 3. Target repository layout

```
photoflow/
├── pyproject.toml            # uv-managed; project + tool config (ruff, pytest)
├── README.md                 # adapt the provided one
├── HANDOFF.md                # this file (keep, update as decisions are made)
├── justfile                  # dev tasks: test, lint, fmt, run
├── .pre-commit-config.yaml   # ruff format + check
├── .github/workflows/ci.yml  # uv sync, ruff, pytest on push (optional, phase 5)
├── src/photoflow/
│   ├── __init__.py           # __version__
│   ├── cli.py                # argparse (or typer later) — thin; wires commands
│   ├── config.py             # Config dataclass + photoflow.toml loader (tomllib)
│   ├── db.py                 # schema, open_db, migrations, run/action helpers
│   ├── models.py             # FileRecord dataclass, Role/Status/Kind enums
│   ├── naming.py             # slugify + dest_for (pure)
│   ├── hashing.py            # content_hash (blake2b), perceptual_hash, hamming
│   ├── bktree.py             # BKTree (pure, unit-testable)
│   ├── exiftool.py           # subprocess wrapper: batch read (-j argfile),
│   │                         #   batch write (-execute argfile), merge (-wm cg)
│   ├── dates.py              # all regexes + resolve_date cascade (pure)
│   ├── scan.py               # walk, classify, fingerprint, store
│   ├── planner.py            # exact groups, raw/jpeg + live pairs,
│   │                         #   near-dupe components, burst test, role writes
│   ├── review.py             # decisions.csv (carry-forward) + review.html + thumbs
│   ├── apply.py              # copy, provenance, decisions consumption
│   ├── xmp.py                # sidecar XML builder + embed-arg builder
│   └── audit.py              # JSONL + actions-table logging
└── tests/
    ├── conftest.py           # synthetic fixture builder (see §5)
    ├── test_dates.py         # exif/filename/folder/mtime cascade, bogus years
    ├── test_hashing.py       # hamming, BKTree add/query radius behavior
    ├── test_naming.py        # slugify, dest_for variants (time/no-time/no-date)
    ├── test_planner.py       # exact groups, keeper preference, pairs, bursts
    ├── test_review.py        # decision carry-forward on regeneration
    └── test_pipeline.py      # integration: full run + incremental round (§5)
```

Conventions: Python 3.11+, type hints throughout, `pathlib` everywhere,
ruff for lint+format, pytest. Keep modules import-light so pure logic
(dates, bktree, naming) tests run without exiftool installed; mark
exiftool-dependent tests with `@pytest.mark.exiftool` and skip if absent.

## 4. Migration plan (phased; commit per phase)

**Phase 0 — scaffold.** `uv init`, drop `photoflow.py` in as
`src/photoflow/_reference.py` (untouched), add `python -m photoflow` entry that
just calls its `main()`. Smoke-test the CLI still works. Commit.

**Phase 1 — extract pure logic + tests first.** Move dates, hashing, bktree,
slug/dest naming into modules with unit tests copied from observed behavior of
the reference. These have zero side effects and lock in the spec.

**Phase 2 — extract infrastructure.** db.py (schema verbatim; add a
`schema_version` pragma/table now to enable future migrations), exiftool.py,
audit.py, xmp.py.

**Phase 3 — split the commands.** scan/planner/review/apply/status into their
modules; cli.py becomes a thin dispatcher. Delete `_reference.py` only after
the integration test (§5) passes against the new package.

**Phase 4 — config file.** `photoflow.toml` in workdir overriding the constants
(`near_dupe_threshold`, `burst_window_s`, `min_year`, `slug_max`,
`exiftool_batch`, extension sets). CLI flags override file. Defaults must equal
current constants.

**Phase 5 — tooling polish.** pre-commit, justfile, CI, optional `typer` +
`rich` progress bars (cosmetic only; keep plain-text fallback).

Do not start backlog items (§8) until Phase 3's integration test is green.

## 5. Test strategy

**Synthetic fixture (conftest.py).** Recreate the fixture used to validate the
prototype — no binary assets in the repo, generate with Pillow:

- `Old Laptop/Holiday 2015/beach.jpg` — noise image; exiftool sets
  `DateTimeOriginal=2015:07:14 10:30:00`, `Model=Canon EOS 70D`
- `Random/beach copy.jpg` — byte-copy of beach.jpg (exact dupe, cross-folder)
- `Phone Backup/Camera/IMG_20190304_101112.jpg` — no EXIF (filename date path)
- `Random/sunset_big.jpg` 1000×750 and `sunset_small.jpg` — same scene,
  downscaled re-encode (near-dupe → review path)
- `Old Laptop/Holiday 2015/mountain.jpg` + `mountain.dng` — same stem; the
  .dng must NOT be byte-identical (append bytes) or it collapses into an
  exact dupe instead of a RAW pair
- `Old Laptop/Holiday 2015/no_meta.png` — folder-year date path
- Burst trio: three noise images, same Model, DateTimeOriginal 2 s apart

**Integration test (the important one):**
1. scan + plan → assert: 1 exact dupe, 1 RAW pair, 1 burst group kept,
   1 review group; date_source distribution as expected.
2. review → programmatically fill decisions.csv (keep big, skip small,
   merge_from = small's id) → apply → assert tree layout, XMP description on
   beach.jpg contains BOTH source rel-paths, .dng has a sidecar.
3. **Incremental regression round:** new source dir with (a) byte-copy of
   beach.jpg, (b) one genuinely new image. scan + plan + apply → assert
   exactly ONE new file copied, sunset_small still `skipped_manual`,
   sunset_big NOT re-copied or re-queued. This is the §7 bug's regression test.
4. Run review again → assert prior decisions still present in decisions.csv.
5. apply `--dry-run` → assert no filesystem changes.

## 6. Key implementation notes (things that look odd but are deliberate)

- **exiftool batching via argfiles** (`-@ file`, and `-execute`-separated
  blocks for writes): avoids Windows command-length limits and per-file
  process spawn cost. Keep; consider `-stay_open` later (§8).
- **`-wm cg` for metadata merge**: fill-missing-only semantics — never
  overwrites existing tags on the keeper.
- **BK-tree over unique phash ints, then union-find** to form lookalike
  components; raw/jpeg pair members are excluded from components before the
  burst/review classification.
- **scan skip rule:** path already in manifest with same size and mtime
  (±1 s tolerance — FAT/exFAT timestamps) → skip re-fingerprinting.
- **HEIC:** pillow-heif optional; without it HEIC gets exact dedupe only and
  no thumbnail. Don't make it a hard dependency (Windows wheels availability).
- **Videos:** exact-hash dedupe only, by explicit product decision (owner is
  confident there are no resized video variants).

## 7. Known issues / lessons already learned

- **Fixed bug (keep the regression test):** plan originally only reset
  role/state for non-copied files; a copied file retained a stale `review`
  role, fell out of the keeper set, and its near-dupe twin was silently
  promoted to `keep` on the next incremental run. Fix = invariant §2.4.
- `decisions.csv` regeneration originally clobbered decisions; fixed with
  carry-forward by file_id. If decisions move into the DB (§8) keep CSV as the
  editing surface.
- Fake RAW files in tests must not be byte-identical to their JPEG.
- exiftool JSON output may omit requested tags; all reads must `.get()`.
- **Fixed bug (Windows):** exiftool emits forward-slash `SourceFile` paths even
  on Windows, so keying the batch-read result by the raw string made every
  lookup miss — EXIF dates/camera/dimensions silently dropped for all files
  (bursts demoted to review, EXIF naming never fired). The reference single
  file has this bug; the package's `exiftool_json` normalizes keys via
  `str(Path(...))` (no-op on POSIX). Keep the normalization.
- Date strings are parsed defensively (regex, year sanity window) — EXIF in
  the wild contains `0000:00:00`, timezone suffixes, and garbage.

## 8. Backlog (post-refactor, rough priority order)

1. **Decisions in DB** (`decisions` table; CSV import/export as the UI).
2. **exiftool `-stay_open` daemon** wrapper for large-library speed.
3. **Parallel hashing** (thread pool; disk-bound, modest win on SSD/NVMe).
4. **RAW embedded-preview pHash** (`exiftool -b -PreviewImage`) so RAW+JPEG
   pairs are detected across folders, not just same-stem-same-dir.
5. **Live Photo pairing via QuickTime ContentIdentifier / MediaGroupUUID**
   instead of stem matching.
6. **Review UX:** keep/skip buttons in review.html that export a filled CSV
   client-side (static JS, no server).
7. **`verify` command:** re-hash library files vs manifest (bit-rot check).
8. **Schema migrations** runner keyed on `schema_version`.
9. **Immich integration notes:** external library mode reads this layout +
   XMP directly; document, don't integrate.

## 9. Acceptance criteria for the refactor

- `python -m photoflow <cmd>` exposes the same five subcommands, same flags,
  same workdir artifacts (db, logs/, thumbs/, review.html, decisions.csv).
- Integration test (§5) passes, including the incremental regression round.
- Running the new package and the reference single file against the same
  fixture produces an identical organized tree (paths + content hashes) and
  equivalent manifest role/status counts.
- `ruff check` and `ruff format --check` clean; no exiftool requirement for
  pure-logic unit tests.
