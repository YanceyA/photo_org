# photoflow — code review findings & improvement plan (2026-08-22)

> **Status: Tier 1 + Tier 2 APPROVED by owner 2026-08-22** (T1–T12, T21–T23). Tiers 3/4 not approved.
> Implementation plan: `docs/plans/2026-08-22-impl-master.md` (+ lane files A/B/C). Nothing implemented yet.
> Owner constraints added at approval: an `enrich scan` is running against the live `photoflow_work/` —
> the implementation session must never run photoflow against it; and `J:\photos_org` is an Immich
> external library — see the master plan's Immich notes before running `refile`/`prune-sidecars`.
> Part A is the review (what was found, with evidence). Part B is the proposed work, grouped into
> tiers so individual items can be approved, pruned or re-ordered. Once the scope is approved, each
> approved task will be expanded into a step-by-step TDD plan (superpowers:writing-plans format) and
> executed in a separate session with `superpowers:executing-plans`.
>
> **For Claude (implementation session):** read Part B only after the owner has marked which tasks
> are approved. Preserve every HANDOFF.md §2 invariant; in particular sources stay read-only and
> anything that touches the *library* (J:\photos_org) must be dry-run-first and logged.

Review method: four independent passes — (1) line-by-line correctness review of `src/`,
(2) core-pipeline workflow/robustness review, (3) enrich subsystem review, (4) Capture One / RAW
gap analysis — then every high-impact claim was re-verified by me against the code and, read-only,
against the real manifest (`photoflow_work/photoflow.db`, 153,299 rows; 45,194 copied) and live
`exiftool 13.59` runs on files under `H:\`. No files outside this plan were modified.

Baseline at review time (branch `feature/enrich`, HEAD `0b9ad1c`): `ruff check` clean,
`pytest` 156 passed / 1 skipped (2m38s with exiftool).

Owner constraints that shaped the ranking: single occasional user, a few imports per year, wants
high-value / low-effort work, no speculative features. Effort: **S** < 1 h, **M** ≈ half day,
**L** ≥ 1 day. Value: **H/M/L** for *this* user.

---

## Part A — Findings

### A.1 Headline (verified on your data)

| # | Finding | Evidence | Impact |
|---|---|---|---|
| H1 | **Video capture dates are mostly lost.** `exiftool.py:33` passes `-fast2`, which stops exiftool before the trailing `moov` atom that iPhone/most MP4/MOV files use, so no `CreateDate` is returned. | `exiftool -fast2 IMG_0735.MOV` → `{}`; without `-fast2` → `CreateDate 2010:09:03 16:03:31`. Manifest: **3,100 of 4,580 videos have `exif_date IS NULL`**; they were filed by filename (2,698), folder (500) or mtime (520). That MOV from 2010 is in `2018/` because its managed-catalog folder was `Originals/2018/8/13/…` (import date). | H |
| H2 | **The 862 videos that *did* get a date are 12–13 h off.** QuickTime dates are UTC by spec; photoflow stores them raw. For NZ that means wrong day/month/year around midnight. Fix is `-api QuickTimeUTC=1` (and prefer `Keys:CreationDate`, which iPhones write with a tz offset). | `-api QuickTimeUTC=1` → `2010:09:04 04:03:31+12:00` | H |
| H3 | **There is no repair path for H1/H2.** `scan` skips any path with same size+mtime (`scan.py:44-49`); `status='copied'` is durable and `dest_path` never changes. Fixing the exiftool flags alone changes nothing for already-copied files — you'd have to wipe the DB and re-copy 45k files. | code | H |
| H4 | **744 Canon `.crw` RAW files were silently skipped** — `.crw` is not in `raw_ext` (`config.py:27`). Their `.thm` thumbnails *were* ingested (see H5). Also missing: `.iiq .3fr .eip .erf .mrw .sr2 .srf .nrw .rwl .mef .kdc .dcr` (EIP = Capture One Enhanced Image Package). | `find H:\_photos_backup -iname '*.crw'` → 744; e.g. `H:\_photos_backup\2018_Originals\62\CRW_0166.CRW` next to a copied `CRW_0166.THM` | H |
| H5 | **Sidecars are copied into the library as standalone "photos".** `classify()` returns `sidecar` for `.thm/.aae/.xmp` and nothing in `apply` filters them; since `.thm ∉ EMBED_EXT` each also gets a `.thm.xmp`. | `J:\photos_org\2003\11\20031116_155825_CRW-0166_f86d6393.thm` (+ `.thm.xmp`): **237 `.thm` + 237 `.thm.xmp` + 1 orphan `.xmp` = 475 junk files** in the library | H |
| H6 | **No directory exclusion, no min-size filter.** `scan.py:33` skips only *files* starting with `.`; it descends into `CaptureOne/`, `Cache/`, `Proxies/`, `Thumbnails/`, `Trash/`, `$RECYCLE.BIN`, `*.lrdata`, `@eaDir`, `.thumbnails/`. You were saved this round by extension luck (`.cop/.cot/.cof/.cos` are unknown → skipped); a C1 `Trash/` or `Output/` folder or a Lightroom previews tree would be ingested wholesale. | `H:\_photos_backup\OPB Photos 2018\Pregnancy\CaptureOne\{Cache,Settings110}` exists in your sources (119 `.cos` + 118 `.cop/.cot/.cof`); `H:\$RECYCLE.BIN` was walked | H |
| H7 | **`apply` is not crash-safe and never verifies a copy.** `apply.py:59-60`: `if not dest.exists(): shutil.copy2(...)` — a truncated file from disk-full / USB yank is accepted forever; no temp+rename; no per-file try/except (one locked file aborts the run); provenance XMP (`xmp_args`) is flushed only at the very end so an interrupt loses it for every file already committed as `copied`; `exiftool_apply_argfile` ignores the return code. | code | H |
| H8 | **Interrupted `scan` can lose files permanently.** Rows are inserted per path with `content_hash=NULL`, hashes committed every 500; on resume the size+mtime skip rule (`scan.py:44-49`) treats those rows as done, `plan` filters `content_hash IS NOT NULL`, so they are never copied and never reported. (Not yet hit: 0 NULL-hash rows today — but it's a latent data-loss path.) | code; `phash_pending_images` already shows the right manifest-driven pattern | H |
| H9 | **Library mtimes are destroyed.** `copy2` preserves mtime, then `exiftool -overwrite_original` without `-P` (`xmp.py:28`, `enrich/apply.py:149`) resets it to "now". HANDOFF §2.1 claims mtime is preserved. Every `enrich apply` re-bumps it (see E1). | code | M |
| H10 | **`enrich apply` rewrites the whole library on every run.** `enrich/apply.py:87-92` selects every file with a person or tag; `enrich_state.applied` is written but never read. 9 enrich-apply runs so far ⇒ 9 full rewrites (and 9 mtime bumps of every enriched file → full re-index for Immich/digiKam, full re-upload for mtime-based backup). | code | H |
| H11 | **`enrich apply` clobbers foreign hierarchical tags.** `regions.py:103-104` clears `-XMP-lr:HierarchicalSubject=` and rewrites only `People|<name>`; `read_keywords` (`exiftool.py:73`) reads only `dc:Subject` + `IPTC:Keywords`. Any `Places|Paris` style hierarchy added in digiKam/Lightroom is deleted on first apply. Same for `PersonInImage`. | verified with exiftool 13.59 by the reviewer | M (H if you tag in digiKam) |
| H12 | **`enrich merge` leaves the old name in the files.** `keyword_remove_argfile_lines` (`regions.py:108`) is written and unit-tested but has **zero callers**; `merge.py` repoints faces and tells you to re-run apply, which only unions. Regions self-heal, keywords don't. | `grep keyword_remove_argfile_lines src/` → definition only | M |

### A.2 Capture One Pro & RAW groups (your specific question)

What is actually in your sources:

* `H:\_photos_backup\Archive Photos\` **is a Capture One managed catalog** (`Archive Photos.cocatalogdb` + `Originals\` + `Cache\`, bundle extension dropped). Good news: photoflow walked `Originals\` and ingested the real files (7,999 jpg, 51 avi, 43 mov, 46 tif, 13 x3f…); `Cache\` (9k `.cot/.cop/.cof`) was skipped by extension luck. Bad news: its **248 `.crw`** were skipped (H4), its 218 `.thm` copied as junk (H5), and files without EXIF inherited the *import* year from `Originals/2018/8/13/…` (117 copied files folder-dated from that tree; most are the videos of H1 and will self-correct once H1 is fixed).
* `H:\_photos_backup\OPB Photos 2018\Pregnancy\` is a **Capture One session folder** (`CaptureOne\Settings110\*.cos`, `Cache\Proxies|Thumbnails`). Source JPEGs were ingested; adjustments were (correctly) not — photoflow is a pixel organizer. Today nothing documents that, and nothing stops a session's `Trash\` or `Output\` from being ingested next time.
* Capture One adjustments (`.cos`, settings inside `.eip`), variants, ratings, colour tags and C1 keywords live in the catalog SQLite DB and are **not** carried into the library. Carrying ratings/keywords is possible (the `.cocatalogdb` is plain SQLite) but is an L-effort feature — listed under "needs your decision" below, not recommended unless you actually use C1 ratings/keywords.

"RAW photo groups":

* RAW+JPEG pairing is **same-folder + same-stem only** (`planner.py:57-69`). A C1/LR export in `Output\` or `Exports\` never pairs with its RAW in `Capture\`; RAW files get no pHash (`scan.py:117`, PIL can't open them) so near-dupe logic can't relate them either. Both are kept, which is safe, just un-linked.
* Pair members land in the library with **different `_hash8` suffixes**, so `…_mountain_a1b2c3d4.jpg` + `…_mountain_9f8e7d6c.dng` — adjacent in sort order but no viewer stacks them; the only surviving link is `group_id` in the DB. Making them share the keeper's hash8 is a naming-scheme change (invariant §2.5) that would apply to *new* copies only — needs your call (B.4-1).
* Pre-existing `.xmp` sidecars next to RAWs (Lightroom/Bridge/C1) are copied as orphans with their own hash8 stem and a bogus `.xmp.xmp` (H5). The cheap fix is to stop copying them; the better fix (M) is to copy them as `<rawdest>.xmp` and merge provenance into them.

### A.3 Other core findings (M/L impact)

| # | Finding | Where |
|---|---|---|
| C1 | `scan` aborts on a single unreadable entry (`p.is_file()`, `p.stat()` unguarded) and `sorted(root.rglob("*"))` materialises the whole tree before printing anything. | `scan.py:32-43` |
| C2 | `--out` must be retyped every session; nothing records the library root — a typo silently starts a second library. Overlapping roots aren't detected: you scanned `H:\` *and* `H:\_photos_backup` (48k rows hashed twice). | `cli.py`, `apply.py` |
| C3 | Progress: the whole exiftool pass prints one line then nothing for hundreds of batches; no totals/bytes/elapsed; no end-of-run summary. | `scan.py:78`, `exiftool.py:26` |
| C4 | `status` is three GROUP BYs; doesn't show held-for-review, errors, unknown-date, last run, next command. | `status.py` |
| C5 | No `verify` command (HANDOFF §8.7). Note `content_hash` is the *source* hash; after XMP embed the dest bytes differ, so a hash verify needs a `dest_hash` column. Existence+size verify is cheap. | — |
| C6 | No per-folder date override ("this scanned-photo folder is all 1998"). 2,758 copied images + 208 videos are mtime-dated (usually the *copy* date presented as capture date). | `dates.py` |
| C7 | `apply --dry-run` still `mkdir`s every dest folder. | `apply.py:56` |
| C8 | Review page is good; only missing "accept all suggested keepers" bulk action and a thumbnail-less hint for RAW/video members. | `review_page.py` |
| C9 | `new_run` + a JSONL log file is created even for `status` (noise in `runs` and `logs/`). | `cli.py:82-88` |
| C10 | Doc drift: `scan.py:104`, `planner.py:174,176` print `Next: python photoflow.py plan` (file deleted); README says pillow-heif optional but `pyproject.toml:6-8` makes it a hard dep; README sells the size+mtime resume rule that is H8; HANDOFF §2.1 "mtime preserved" is false post-embed. | |

### A.4 Other enrich findings

| # | Finding | Where |
|---|---|---|
| E1 | `enrich scan` commits every 200 files (~10 min of CPU inference lost per crash) and `detector.detect` / `tagger.tag` are unguarded — one truncated JPEG or CUDA OOM aborts the run *and* rolls back the batch. No total printed up front. | `enrich/scan.py:58,84,107` |
| E2 | `exiftool_apply_argfile` ignores return code; apply marks `applied=1` regardless; a read-only/locked file reports success. | `exiftool.py:111`, `enrich/apply.py:155` |
| E3 | No "just do the right thing" entry point: second run needs `scan → assign → cluster → review → (browser) → apply`, order-sensitive. | `cli.py` |
| E4 | RAM++ path (`tagger.py:112-158`, `ensure_ram_checkpoint`, 3 config keys, ~35 README lines) can't run on this machine's Python 3.14 and always falls through to SigLIP. `enrich_batch` has zero references. | |
| E5 | `assign` writes `assign_review_sim<X>.html` on every run, dry or not — workdir accumulates near-identical pages (3 already). | `assign.py:123` |
| E6 | Scale: HDBSCAN over 512-d with euclidean metric degrades to O(n²); `nearest_person` is a Python loop per face × person. 40k faces today; fine if it completed acceptably, worth a 3-line PCA + one matmul if not. | `clustering.py:49`, `review.py:82`, `assign.py:63` |
| E7 | RAW and video are never enriched (`kind='image'` filter) — undocumented; 17.9k RAW keepers get no people/tags. | `enrich/scan.py:26` |
| E8 | First apply replaces any pre-existing MWG regions (e.g. faces tagged in digiKam) — correct for photoflow-authored regions, surprising otherwise; document. | `regions.py:20-68` |
| E9 | Review page UX (good already): missing in-page "merge with cluster above", bulk name/ignore on the noise pool, thumbnail size control, "hide named" toggle. | `page.py` |

### A.5 Line-by-line correctness review (`/code-review high src`)

Findings from the independent bug hunt, adversarially verified (verdict as reported; "✓me" = I re-checked the source myself). Items already covered above are cross-referenced.

| # | Where | Defect | Verdict | Plan |
|---|---|---|---|---|
| R1 | `enrich/apply.py:134` | If `read_keywords()` fails for a 200-file batch (non-JSON stdout from one corrupt XMP → `exiftool.py:96` swallows and returns `{}`), apply falls back to `existing=set()` and the clear-then-rewrite `-XMP-dc:Subject=` wipes every pre-existing keyword, including provenance folder keywords, silently. | CONFIRMED | T9 |
| R2 | `enrich/apply.py:164` | `enrich apply --dry-run` still runs step 1/1b UPDATEs (person upsert, `faces.person_id`, `ignored=1`); the guarded commit is skipped but the unconditional `log_action(); conn.commit()` at the end commits them — a dry run durably mutates the DB and hides those clusters from the next `enrich review`. | CONFIRMED ✓me | T9 |
| R3 | `review_page.py:311-335` | Carried-forward library keeper A (status=copied) + new higher-res C: Enter/`acceptSuggested` keeps C and `clickKeep` only demotes non-keep siblings → A=keep, C=keep → `apply` imports the near-dupe despite the button saying "keep the suggested, skip the rest". Also: status=copied members are editable but `apply` only processes `planned/review` (a "skip" on them is silently ignored); round-1 keepers re-entering as `role=review/status=copied` render undecided; the undo branch (re-click keeper → unkeep) is now only reachable when every member is decided; global keydown blurs a Tab-focused BUTTON so Enter on "Save" fires `acceptSuggested` instead. Fix belongs in `decision_rows`/`build_payload` (pin copied members as locked keepers), not per-handler JS. | CONFIRMED (undo/keydown: PLAUSIBLE) | **T21 (new, Tier 1)** |
| R4 | `enrich/clustering.py:14` | Top-level `import numpy`; `cli.py` imports every enrich module at load, so **every** photoflow command hard-requires numpy although core `dependencies` is only `pillow-heif` — `pip install .` without `[enrich]` → `ModuleNotFoundError` before argparse. Masked in dev because the dev group pulls numpy. | CONFIRMED ✓me | **T22 (new)** |
| R5 | `enrich/page.py:93,411` | Global blacklist rows (`file_id='*'`) the page appends to tags.csv are not carried forward: `tag_rows()` doesn't emit them, the JS `blacklist` Set is seeded only from localStorage, apply never persists them. After the next `enrich review` the blacklisted tag is written into every file — and the union-only write can't remove it later. Violates decisions-carry-forward for enrich. | CONFIRMED ✓me | **T23 (new)** |
| R6 | `enrich/regions.py:103` | = H11 (HierarchicalSubject / PersonInImage clobbered). | PLAUSIBLE (reviewer 3 verified with exiftool) | T10 |
| R7 | `enrich/merge.py:62` | = H12 (`keyword_remove_argfile_lines` dead). | CONFIRMED ✓me | T11 |
| R8 | `enrich/apply.py:53` | Step 1 re-applies every `keep` row of a stale `faces.csv` on every run → re-running apply after `enrich merge` (as merge's own message instructs) re-creates the deleted alias and re-points its faces back. | PLAUSIBLE (code read matches) | T11 |
| R9 | `enrich/review.py:41`, `enrich/apply.py:30` | CSV readers open `utf-8` not `utf-8-sig` and index `row['file_id']`; an Excel "CSV UTF-8" round-trip (BOM) → `KeyError` aborts `enrich review`/`apply`; for faces.csv the "not interested" step silently no-ops. Top-level decisions.csv is immune only because its first column is never indexed by name. | CONFIRMED | T14 |
| R10 | `assign.py:42`, `enrich/review.py:64` | `np.frombuffer` on embedding blobs unguarded (cluster.py guards; siblings crash on a malformed/other-dim blob). | CONFIRMED (cut from top-10) | T15 |
| R11 | `enrich/apply.py:62` | Noise faces (`cluster_id=''`) are dropped from `by_cluster`, so a skip on them is ignored; page offers no dismiss for noise. | CONFIRMED (cut) | B.4-7 |
| R12 | misc | `enrich status` open-cluster count stale after apply; page `save()` never clears the dirty flag; enrich apply logs one `file_id=0` audit row (no per-file audit); no `exiftool_available()` check before enrich apply; `db.py:114` first real migration bypasses `schema_version`. | CONFIRMED (cut) | T9 / T18 |

Cleanup-only (confirmed, low value — do opportunistically inside the task that touches the file, never as a standalone churn pass): `read_keywords` duplicates `exiftool_json`'s batch loop; centroid build duplicated `assign.py:38` vs `enrich/review.py:59`; `_file_uri` triplicated (`review_page.py:59`, `page.py:130`, `assign.py:21`); `_upsert_person` duplicated in `merge.py:30`; medoid computed and discarded `cluster.py:53`; `imgutil.make_thumb` converts before `thumbnail` (defeats JPEG draft mode) and duplicates `review._make_thumbs`; N+1 SQL in `enrich/apply.py:109`; `HAVE_SKLEARN` unused; `enrich_batch` unread (T14).

### A.6 Already good — do not churn

`plan` idempotence + durable statuses and their regression tests; keeper preference (copied > earliest mtime); `phash_pending_images` manifest-driven resume; exiftool `SourceFile` path normalisation; argfile batching and `-wm cg` merge semantics; the interactive review page (keyboard flow, suggested keeper, badges, localStorage, byte-compatible CSV); decisions.csv carry-forward; audit trail; naming scheme (45,194 dest paths, 45,194 distinct); enrich durable/ephemeral split; MWG region geometry (spec-correct, verified idempotent); `dc:Subject` read-union-replace; graceful model-stack degradation; EXISTS-not-IN queries; tag review grouped by tag.

---

## Part B — Proposed work

Each task lists: files, the change, the test that proves it, effort/value. Tasks inside a tier are
independent unless noted. Approve/prune by task id.

### B.1 Tier 1 — Fix the library you already have (recommended: do all)

These repair real damage in `J:\photos_org` or stop it recurring. Order matters only where noted.

**T1. Correct video metadata read** — S / H  
Files: `src/photoflow/exiftool.py`, `src/photoflow/scan.py`, `tests/test_scan.py` (+ a tiny MP4 builder in `tests/conftest.py`).  
Change: `exiftool_json()` takes a `fast: bool`; `scan` calls it with `-fast2` for images/RAW and *without* for `kind='video'`; always add `-api QuickTimeUTC=1`; add `-CreationDate` (QuickTime Keys, tz-aware, what iPhones write) to `EXIF_TAGS` and prefer it over `CreateDate`/`MediaCreateDate` for videos. Store tz-aware strings as-is (`parse_exif_date` already regex-matches the leading `YYYY:MM:DD HH:MM:SS`).  
Test: hand-built minimal MP4 (`ftyp` + `mdat` + trailing `moov/mvhd` with a known creation_time — 168 bytes, built with `struct`, no binary asset; **prototyped during review: exiftool returns `{}` with `-fast2` and `CreateDate 2010:09:03 16:03:31` without, `+12:00` with QuickTimeUTC**) → `exiftool_json` returns `CreateDate` for it; `@pytest.mark.exiftool`. Unit test that the video branch omits `-fast2` (inspect the argfile via a monkeypatched `subprocess.run`).

**T2. `scan --refresh-meta [PATH_PREFIX …]`** — S / H (depends on T1)  
Files: `cli.py`, `scan.py`, `tests/test_scan.py`.  
Change: re-run only the exiftool pass for manifest rows matching the prefixes (or `--kind video`), updating `exif_date/camera/width/height`; no re-hash. Then `plan` already recomputes `date_taken` for every row, including copied ones (`planner.py:141-165`).  
Test: row with stale `exif_date`, run refresh, assert updated; assert `content_hash` untouched.

**T3. `refile --out DIR [--dry-run]`** — M / H (depends on T2; this is the one that repairs the 3,100 videos)  
Files: new `src/photoflow/refile.py`, `cli.py`, `tests/test_refile.py`.  
Change: for every `status='copied'` row, compute `dest_for(row)`; where it differs from `dest_path`, move the *library* file (and its `.xmp` sidecar if present) with `os.replace` after checking the new path is free, update `dest_path`, `log_action('refiled', old -> new)`. Dry-run prints the moves and a count by reason (`date changed`, `folder changed`). Never touches sources. Refuse to run if any target collides.  
Test: fixture with a copied row whose `date_taken` was changed → file moved, sidecar moved, DB updated, audit row; dry-run moves nothing.  
Runbook afterwards (owner runs): `scan --refresh-meta --kind video` → `plan` → `refile --out J:\photos_org --dry-run` → inspect → `refile --out J:\photos_org`.

**T4. Scan resume correctness** — S / H  
Files: `scan.py`, `db.py` (`_migrate`: add `meta_read INTEGER DEFAULT 0` to `files`), `tests/test_scan.py`.  
Change: skip rule becomes size+mtime **and** `content_hash IS NOT NULL`; the exif pass becomes manifest-driven (`WHERE status='scanned' AND meta_read=0`), setting `meta_read=1` per batch — same pattern as `phash_pending_images`. T2 reuses this by resetting `meta_read=0` for its targets.  
Test: insert a row with NULL hash, re-run scan on the same tree → row gets hashed; kill-between-phases simulation via monkeypatch.

**T5. `apply` hardening** — M / H  
Files: `apply.py`, `exiftool.py`, `xmp.py`, `tests/test_pipeline.py`.  
Change: (a) copy to `dest.with_suffix(dest.suffix + ".part")` then `os.replace`; (b) if `dest.exists()` and size ≠ source size → re-copy, else trust; (c) per-file `try/except OSError` → `status='error', error=str(e)`, continue; (d) flush `xmp_args` in the same 500-file batch as the commit (and before the final commit) so provenance can't be orphaned; (e) add `-P` to `embed_args` so library mtime = source mtime; (f) `exiftool_apply_argfile` returns `(returncode, stderr)`, caller prints failures and logs `xmp_embed_failed`; (g) `--dry-run` no longer `mkdir`s.  
Test: truncated pre-existing dest gets re-copied; unreadable source → error row, run continues; mtime preserved after embed (`@pytest.mark.exiftool`); dry-run creates no dirs.

**T6. Sidecar policy** — S / H  
Files: `apply.py`, `config.py` (`copy_sidecars: bool = False`), `models.py`, `tests/test_pipeline.py`, README.  
Change: `apply` skips `kind='sidecar'` rows → `status='skipped_sidecar'` + audit (they stay in the manifest for dedupe/audit). When `copy_sidecars=true` (or later, T-opt), keep today's behaviour.  
Test: fixture gains `IMG_0001.THM` next to a JPEG and a `foo.xmp` next to `foo.dng`; assert neither reaches the library and no `.thm.xmp` is written.  
**T6b. One-off cleanup of the 475 junk files already in `J:\photos_org`** — S. Separate opt-in command `prune-sidecars --out DIR [--dry-run]`: finds copied rows with `kind='sidecar'` (and their `.xmp`), *moves* them to `photoflow_work/pruned/<same relative path>` (never deletes), sets `status='skipped_sidecar'`, `dest_path=NULL`, audit. Dry-run first. (Cheaper alternative: a documented manual step — your call.)

**T7. Source hygiene: extensions, exclude dirs, min size, robust walk** — S / H  
Files: `config.py`, `scan.py`, `tests/test_scan.py`, `tests/test_config.py`, README.  
Change: (a) `raw_ext` default += `.crw .iiq .3fr .eip .erf .mrw .sr2 .srf .nrw .rwl .mef .kdc .dcr`; (b) new `exclude_dirs: frozenset[str]` default `{"CaptureOne", "Cache", "Proxies", "Thumbnails", "Trash", "$RECYCLE.BIN", "System Volume Information", "@eaDir", ".thumbnails", ".Trash", ".Trashes", "Previews.lrdata", "Smart Previews.lrdata", "Lightroom Settings", "__MACOSX"}` matched case-insensitively against each path component; (c) `min_size_bytes: int = 0` (document `20000` as a sensible value for thumbnail-laden sources); (d) replace `sorted(rglob)` with `os.walk(..., onerror=…)` pruning `dirs[:]` in place, sorting per directory, `try/except OSError` around `stat`, printing `skipped N unreadable / pruned M dirs` at the end.  
Test: fixture with `CaptureOne/Cache/x.jpg`, `Trash/y.jpg`, an unreadable file (chmod 000 on POSIX / skip on Windows), a 1 KB JPEG with `min_size_bytes=20000`; assert manifest contents. Config test for the new keys.  
Runbook afterwards: `scan H:\_photos_backup` → `plan` → `apply` picks up the 744 `.crw` (and any `.iiq/.eip`).

**T21. Review page: lock in-library keepers; Enter never yields two keepers** — S / H (R3)  
Files: `review_page.py` (`decision_rows`, `build_payload`, JS `acceptSuggested`/`clickKeep`/keydown), `tests/test_review_page.py`, `tests/test_review.py`.  
Change: members with `status='copied'` are emitted as `locked` in the payload and rendered as a non-clickable "in library" keeper; `acceptSuggested` on a group that has a locked keeper marks every *new* member `skip` (safe default — never import a near-dupe silently); clicking Keep on a new member keeps it *in addition* and the group shows "2 keepers" explicitly; re-click of a non-locked keeper always un-keeps it; keydown handler ignores events whose target is a BUTTON/INPUT instead of blurring and continuing. `apply` already ignores copied rows, so no apply change.  
Test: payload marks copied rows locked; `decision_rows` never writes a decision for a locked row that the CSV would later try to flip; JS logic is exercised via the existing page-template tests (string assertions) — keep it minimal.

**T8. Doc drift** — S / M  
`scan.py:104`, `planner.py:174,176` → `photoflow plan/apply`; README resume rule + pillow-heif wording; HANDOFF §2.1 mtime sentence; README: a "Capture One / Lightroom libraries" paragraph (what is ingested — `Originals/`, `Capture/`; what is not — adjustments, ratings, `Trash/`; point `scan` at the catalog folder or session root and rely on `exclude_dirs`); README: RAW/video are not enriched (E7).

### B.2 Tier 2 — Enrich correctness (recommended: T9–T12; T13/T14 nice-to-have)

**T9. `enrich apply` incremental + mtime-safe + failure-aware** — S / H  
Files: `enrich/apply.py`, `db.py` (`_migrate`: `enrich_state.applied_sig TEXT`), `exiftool.py`, `tests/test_enrich_commands.py`.  
Change: per file compute `sig = sha1(json.dumps({"tags": tags, "people": people, "regions": regions}))`; skip when `sig == enrich_state.applied_sig` unless `--all`; write `-P` on every `-overwrite_original` block; `exiftool_apply_argfile` return code/stderr checked per `-execute` block (exiftool prints `1 image files updated` / errors — parse `-execute` output with `-echo4`-style markers or simply run per-batch and record failures), failed files keep their old `applied_sig`. Print `written / unchanged / failed`. Also (R1) a target absent from `existing_map` (keyword read failed) is **skipped with a warning**, never written with `existing=set()`; (R2) `--dry-run` wraps the whole command in a transaction that is rolled back (or simply returns before step 1), so a dry run mutates nothing; (R12) `exiftool_available()` check up front; per-file audit rows (`enrich_applied`, detail = sig).  
Test: apply twice → second run writes 0; rename a person → only affected files rewritten; mtime unchanged after apply (`@pytest.mark.exiftool`); dry-run leaves `faces.person_id`/`persons` untouched; a file missing from the keyword read is not written.

**T10. Preserve foreign hierarchical tags / PersonInImage** — S / H  
Files: `exiftool.py` (`read_keywords` also returns `HierarchicalSubject` and `PersonInImage` sets), `enrich/regions.py` (`keyword_argfile_lines` unions existing hierarchy minus `People|*`, and existing PersonInImage minus names we own), `tests/test_enrich_regions.py`, `tests/test_enrich_commands.py` (real exiftool: file with `Places|Paris` keeps it).

**T11. `enrich merge` strips stale names from files** — S / M  
Files: `enrich/merge.py`, `tests/test_enrich_commands.py`.  
Change: after repointing faces, collect `dest_path`s that carried the alias, emit `keyword_remove_argfile_lines(aliases)` per file, invalidate their `applied_sig` so the next apply rewrites regions. (R8) `enrich apply` step 1 only applies a `faces.csv` keep row when that face's `person_id IS NULL` (a stale CSV can no longer resurrect a merged alias); `merge` also rewrites `faces.csv` in place (alias → canonical) so the page and CSV agree. Real-exiftool test: alias gone from `dc:Subject`, canonical present; merge → apply with the old faces.csv does not recreate the alias.

**T12. `enrich scan` crash-resilience + progress** — S / H  
Files: `enrich/scan.py`, `tests/test_enrich_commands.py`.  
Change: commit every 20 files; wrap `detector.detect` and `tagger.tag` separately in `try/except Exception` → `log_action('enrich_detect_error'|'enrich_tag_error')`, mark that side done so it isn't retried forever (or leave undone + count — decide: *leave undone* so a transient OOM retries next run, but cap via an `enrich_state.errors` counter); print `N/total` + files/min at every commit; print total up front.

**T13. `enrich update` convenience command** — S / M  
`cli.py`: dispatch `scan → assign → cluster → review`, then print the HTML path and "then: `photoflow enrich apply`". Pure dispatch; test via the CLI test harness.

**T14. Delete dead weight** — S / M  
Remove RAM++ tagger path (`tagger.py:112-158`, `ensure_ram_checkpoint`), config keys `enrich_tagger/ram_checkpoint/ram_image_size` (keep `clip_*`), `enrich_batch`, README RAM++ section; `assign` writes its review page only with `--dry-run` and to a single `assign_review.html`. Unknown-key config loading stays fatal, so add a one-line migration note in README for anyone with those keys in `photoflow.toml`.

**T22. Core commands must not require numpy** — S / H (R4)  
Files: `cli.py`, `enrich/clustering.py`, `tests/test_enrich_deps.py`.  
Change: import the enrich command modules inside the `args.cmd == "enrich"` branch (or make `ENRICH_COMMANDS` a lazy dict of import paths); move `import numpy` in `clustering.py` into the functions (as `assign.py`/`review.py` already do). Test: run `python -c "import photoflow.cli"` with `numpy` blocked via `sys.modules['numpy']=None` in a subprocess → succeeds; `photoflow status` works.

**T23. Persist the tag blacklist in the DB** — S / H (R5)  
Files: `db.py` (`tag_blacklist(tag TEXT PRIMARY KEY, ts)`), `enrich/apply.py` (write `*`/reject rows into it; blacklist = DB ∪ CSV), `enrich/review.py` + `enrich/page.py` (seed the page's `blacklist` Set and emit the `*` rows into tags.csv from the DB; blacklisted tags are excluded from `tags.csv` rows and from the auto-applied summary), `enrich/scan.py` (optionally skip inserting blacklisted tags), `tests/test_enrich_commands.py`, `tests/test_enrich_page.py`.  
Test: blacklist "document" → apply → scan+review again → tags.csv still carries `*,document,reject` and the payload seeds it; apply never writes "document".

**T15. Clustering/assign scale (only if the last `enrich cluster` felt slow)** — S / M  
`clustering.py`: optional PCA to 128 dims before HDBSCAN (`cfg.enrich_pca_dims`, 0 = off); `review.py`/`assign.py`: replace per-face `nearest_person` loops with one normalised `embs @ centroids.T`. Tests: same labels on the synthetic fixture; matmul equals loop.

### B.3 Tier 3 — Workflow quality of life (cheap; approve à la carte)

**T16. Remember the library root; detect overlapping sources** — S / M  
`db.py`: `meta(key,value)` table; `apply` stores `library_root`; `--out` optional thereafter, hard error if a *different* `--out` is given without `--force-out`; `scan` warns when a new root is a parent/child of an existing `source_root`.

**T17. Progress + summary lines** — S / M  
`exiftool_json`: `  metadata batch i/n`; counters get elapsed + rate; `scan`/`apply` end with a one-line summary (files, bytes, errors, elapsed).

**T18. Richer `status`** — S / M  
Add: held-for-review groups, error rows (count + first 5 messages), unknown-date count, copied bytes, per-source-root counts, last run per command with timestamp, library root (T16), and the "next command" hint. Pure SQL. Skip `new_run`/log-file creation for `status` (C9).

**T19. `verify --out DIR`** — S / M  
For each `status='copied'` row: dest exists and size matches source; report missing/mismatched; `--hash` option re-hashes dest and stores `dest_hash` (new column) on first run, compares on later runs (bit-rot check that survives XMP embeds).

**T20. Review page: "Accept all suggested" button (+ undo)** — S / M  
`review_page.py` JS: one header button calling the existing `acceptSuggested` over undecided groups; counter shows how many it decided; Ctrl+Z undoes the batch. Thumbnail-less cards show "no preview (RAW/video)".

### B.4 Tier 4 — Needs your decision (not recommended by default)

1. **RAW+JPEG pair share one stem** (M): `dest_for` uses the pair keeper's hash8 for both members so viewers stack them. Changes the naming scheme for pairs (invariant §2.5) — applies to new copies only; existing 4,702 pairs would stay as-is unless refiled. Only worth it if your viewer stacks by stem.
2. **Copy pre-existing RAW sidecars as `<rawdest>.xmp` and merge provenance into them** (M): keeps Lightroom/C1 develop settings and keywords next to the RAW. Only 8 such `.xmp` files exist in your sources today → low value for you.
3. **Per-folder date override** (M): `photoflow_work/date_overrides.csv` (`path_prefix,date,force`) consulted in `plan` between the filename and folder steps; plus a toggle to send `mtime`-dated files to `unknown-date/` instead of trusting the copy date. Worth it only if you have scanned-print folders to assert.
4. **Capture One ratings/keywords/colour-tag import** (L): read `*.cocatalogdb` (SQLite) and write `xmp:Rating` / `dc:subject` into the copied files. Only if you actually rated/keyworded in C1.
5. **RAW embedded-preview pHash** (L, HANDOFF §8.4): `exiftool -b -PreviewImage` → pHash so RAWs join near-dupe groups and pair with exports in other folders. Big win only for C1 `Output/` vs `Capture/` layouts.
6. **Enrich RAW/video** (M): decode RAW previews for face/tag scan. 17.9k RAW keepers currently un-enriched — but most have a JPEG twin that *is* enriched.
7. **Enrich review page extras** (M): in-page cluster merge, noise-pool bulk actions, thumbnail size, "hide named".

### B.5 Not proposed (considered, rejected for this user)

Decisions-in-DB (§8.1 — CSV works and the page edits it), exiftool `-stay_open` daemon (§8.2 — batching is already fine), parallel hashing (§8.3 — disk-bound), schema-migration runner (§8.8 — `_migrate` is enough), Live Photo pairing by ContentIdentifier (§8.5 — only 19 live pairs), Immich integration (§8.9 — doc only, folded into T8).

---

## Suggested approval shape

* **Minimum worthwhile:** T1–T8 + T21 (fixes your library and the review page) + T9–T12, T22, T23 (enrich correctness). ≈ 2–2.5 days.
* **Comfortable:** + T13, T14, T16, T17, T18, T20. ≈ +1 day.
* Tier 4 items individually on request.

After approval: I'll turn the approved set into a task-by-task TDD plan (one commit per task, tests first, ruff + full suite green before each commit), to be executed in a separate session from a fresh worktree off `feature/enrich`. The library-repair runbook (T2→T3, T6b, T7 re-scan) is executed by you, dry-run first, after the code lands.
