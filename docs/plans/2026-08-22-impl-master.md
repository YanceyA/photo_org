# photoflow Tier 1 + 2 fixes — Master Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (one fresh
> subagent per task, review between tasks) driven from this file; each lane file is a
> superpowers:writing-plans style plan and is executed task-by-task. Use
> superpowers:using-git-worktrees before touching code.

**Goal:** Land the owner-approved Tier 1 + Tier 2 tasks from
`docs/plans/2026-08-22-review-and-improvement-plan.md` (T1–T12, T21–T23) as small, tested, committed
increments on a branch off `feature/enrich`, without touching the owner's live data.

**Architecture:** Work is split into three *lanes* grouped by the files they touch so subagents don't
collide. Lanes are independent except for three shared files (`exiftool.py`, `db.py`, `cli.py`), where
every edit is additive and localized; the coordinator merges lanes in the order A → B → C.

- **Lane A — scan / metadata / refile:** `docs/plans/2026-08-22-impl-lane-a-scan-dates.md` (T7, T4, T1, T2, T3)
- **Lane B — apply hardening / sidecars / review page / docs:** `docs/plans/2026-08-22-impl-lane-b-apply-review.md` (T5, T6, T6b, T21, T8)
- **Lane C — enrich correctness:** `docs/plans/2026-08-22-impl-lane-c-enrich.md` (T22, T9, T10, T11, T12, T23)

**Tech Stack:** Python 3.11+ (owner runs 3.14), `uv`, pytest, ruff (line length 100), SQLite, exiftool
13.x on PATH, Pillow for synthetic fixtures. No binary test assets.

---

## 0. Hard constraints (read before anything else)

1. **Never run `photoflow` against the repo's `photoflow_work/`.** It is the owner's live manifest
   (153k rows) and an `enrich scan` is running against it *right now*. Every test and every manual
   check uses a `tmp_path` / scratch workdir (`--workdir <tmp>`). The new additive migrations
   (`files.meta_read`, `enrich_state.applied_sig`, `enrich_state.errors`, `tag_blacklist`) are applied
   by `open_db()` the first time the owner runs any command after the scan finishes — that is expected
   and safe (`CREATE TABLE IF NOT EXISTS` / `ALTER TABLE ADD COLUMN` guarded by `PRAGMA table_info`).
2. **Sources are read-only; the library is the owner's.** No task deletes anything. `refile` and
   `prune-sidecars` *move* library files and only when the owner runs them (dry-run first). Nothing in
   this plan runs them.
3. **Immich references `J:\photos_org` as an external library.** `-P` (T5/T9) stops mtime churn going
   forward. `refile` (T3) and `prune-sidecars` (T6b) move files → Immich sees delete + add; album
   membership / favourites of moved assets are path-keyed and will be lost for those assets. The
   runbook says so and tells the owner to rescan the external library afterwards.
4. **Work in a git worktree** branched from `feature/enrich` (e.g. `feature/review-fixes`); commit per
   task with conventional messages; `uv run ruff check src tests && uv run ruff format src tests` and
   the **full** `uv run pytest` green before each commit (exiftool is on PATH here; the model-stack
   `@pytest.mark.enrich` tests may skip).
5. Preserve HANDOFF.md §2 invariants. Naming/dest scheme does not change (T3 only *applies* the
   existing scheme to rows whose date changed).
6. Do not start any Tier 3/4 item, and do not "clean up" adjacent code beyond what a task's plan says.

## 1. Execution shape

**Recommended:** three parallel worktrees, one coordinator session.

```
coordinator (this session, opus)
 ├─ worktree A  branch feature/review-fixes-a   Lane A tasks A1→A5 (sequential within lane)
 ├─ worktree B  branch feature/review-fixes-b   Lane B tasks B1→B5
 └─ worktree C  branch feature/review-fixes-c   Lane C tasks C1→C6
merge order into feature/review-fixes: A, then B, then C (resolve the overlaps in §2), full suite green,
then stop — the owner integrates into feature/enrich / master themselves (see memory: branches stay unmerged).
```

Within a lane, dispatch **one fresh subagent per task** (subagent-driven-development): give it the
lane file, the task id, the constraints in §0, and the model below; review its diff + test output
before the next task. If you prefer a single worktree, run the lanes sequentially A → B → C — nothing
breaks, it just takes longer.

### Agent / model assignment

| Task | Lane | Model | Why |
|---|---|---|---|
| A1 (T7 exclude_dirs, min size, raw_ext, os.walk) | A | sonnet | mechanical, fully specified |
| A2 (T4 scan resume + `meta_read`) | A | opus | touches the skip rule that guards data loss |
| A3 (T1 video metadata, MP4 fixture) | A | opus | tz semantics + tag-preference order affect every capture date; fixture code is given |
| A4 (T2 `scan --refresh-meta`) | A | sonnet | thin CLI over A2/A3 |
| A5 (T3 `refile`) | A | opus | moves library files; collision/rollback logic |
| B1 (T5 apply hardening) | B | opus | crash-safety semantics, several interacting changes |
| B2 (T6 sidecar policy) | B | sonnet | small, specified |
| B3 (T6b `prune-sidecars`) | B | sonnet | mirrors refile's move helper, specified |
| B4 (T21 review page locked keepers) | B | opus | JS + payload semantics, easy to get subtly wrong |
| B5 (T8 docs) | B | sonnet | prose; run last so flag names are final |
| C1 (T22 lazy enrich imports) | C | sonnet | mechanical |
| C2 (T9 enrich apply incremental/-P/dry-run/R1) | C | opus | most intertwined task in the plan |
| C3 (T10 preserve foreign hierarchical tags) | C | opus | union rules + real-exiftool verification |
| C4 (T11 merge strips stale names, R8) | C | sonnet | CSV rule is settled in the lane file (page only emits `person_id IS NULL` faces — verified `enrich/review.py:75-79`) |
| C5 (T12 enrich scan resilience) | C | sonnet | specified |
| C6 (T23 tag blacklist table) | C | sonnet | specified |

Use a **fresh opus reviewer** (superpowers:code-reviewer or `/code-review`) after each lane finishes,
before merging it.

## 2. Shared-file overlaps (merge notes)

| File | Lane A | Lane B | Lane C | Resolution |
|---|---|---|---|---|
| `src/photoflow/exiftool.py` | `exiftool_json(..., fast=True)`, `EXIF_TAGS += -CreationDate`, `-api QuickTimeUTC=1` | `exiftool_apply_argfile` returns `ExiftoolResult`; `-P` in `merge_metadata` | `read_keywords` returns `KeywordSets` (+ HierarchicalSubject/PersonInImage); apply batches use the `ExiftoolResult` | three different functions — textual merge is clean; re-run `tests/test_enrich_commands.py` after merging C |
| `src/photoflow/db.py` (`_migrate`) | `files.meta_read` | — | `enrich_state.applied_sig`, `enrich_state.errors`; `SCHEMA` gains `tag_blacklist` | additive; keep each ALTER guarded by its own `PRAGMA table_info` check; order irrelevant |
| `src/photoflow/cli.py` | `scan` args (`sources` nargs="*", `--refresh-meta`, `--kind`), `refile` parser | `prune-sidecars` parser | lazy enrich import block; `enrich apply --all` | merge all parsers; after merge, run `tests/test_enrich_cli.py` + the CLI tests in `tests/test_pipeline.py` |
| `src/photoflow/config.py` | `exclude_dirs`, `min_size_bytes`, `raw_ext` default | `copy_sidecars` | — | additive fields |
| `tests/conftest.py` | `make_minimal_mp4`, fixture tree gains `CaptureOne/…`, `Trash/…` | fixture tree gains `IMG_0001.THM`, `mountain.xmp` | — | both extend the builder; keep existing fixture counts/asserts passing (some tests assert file counts — update them once, deliberately) |
| `README.md` / `CLAUDE.md` / `HANDOFF.md` | — | B5 owns all prose | C-lane README lines (enrich apply `--all`, blacklist persistence) go into B5's commit or a tiny follow-up | let B5 run after C6 if possible |

## 3. Definition of done

- All 16 tasks committed on the merged branch; `uv run ruff check src tests` clean; `uv run pytest`
  green (record the counts in the final commit message: baseline was 156 passed / 1 skipped).
- New regression tests exist for: exclude_dirs/min-size/raw_ext; NULL-hash resume; video
  metadata via the synthetic MP4; refresh-meta; refile (dry + real + collision); apply re-copy /
  error isolation / dry-run no-mkdir / mtime preserved; sidecar skip + prune; locked review keepers;
  core import without numpy; enrich apply idempotent + dry-run-safe + keyword-read-failure-safe;
  foreign HierarchicalSubject/PersonInImage preserved; merge strips alias + stale CSV can't resurrect;
  enrich scan per-file error isolation; blacklist carry-forward.
- README/CLAUDE.md/HANDOFF updated (B5), including the owner runbook below.
- Branch left **unmerged** for the owner (consistent with how `feature/enrich` and
  `feature/interactive-review` were handled).

## 4. Owner runbook (after the code lands; owner runs these, not the implementation session)

Wait for the in-progress `enrich scan` to finish first. Then, from the repo root with the new code:

```
# 0. back up the manifest
copy photoflow_work\photoflow.db photoflow_work\photoflow.db.pre-fixes.bak

# 1. repair video dates (T1/T2/T3)
uv run photoflow scan --refresh-meta --kind video          # re-reads QuickTime dates, no re-hash
uv run photoflow plan                                       # recomputes date_taken for all rows
uv run photoflow refile --out J:\photos_org --dry-run       # inspect the MOVE list (expect ~3k)
uv run photoflow refile --out J:\photos_org                 # moves library files + .xmp sidecars

# 2. remove the 475 junk sidecars from the library (T6b) — moves to photoflow_work\pruned\
uv run photoflow prune-sidecars --out J:\photos_org --dry-run
uv run photoflow prune-sidecars --out J:\photos_org

# 3. pick up the 744 .crw (and any .iiq/.eip) that were skipped (T7)
uv run photoflow scan H:\_photos_backup                     # exclude_dirs/min-size now apply
uv run photoflow plan
uv run photoflow apply --out J:\photos_org

# 4. enrich: the FIRST apply after the fixes rewrites every enriched file once (applied_sig starts
#    NULL) — this also installs the HierarchicalSubject/PersonInImage fix and restores -P mtimes.
#    Every apply after that reports "written 0 / unchanged N" unless something changed.
uv run photoflow enrich apply --dry-run
uv run photoflow enrich apply

# 5. Immich: trigger an external-library rescan. Moved files (steps 1–2) appear as removed + new assets.
```

Redo the interrupted/in-progress library's `enrich scan` after the fixes if you want C5's
resilience and the blacklist behaviour for it; already-persisted faces/tags are kept (enrich_state).

## 5. Handoff prompt for the fresh session (copy verbatim)

> Implement the approved Tier 1 + 2 fixes for photoflow. Start by reading
> `docs/plans/2026-08-22-impl-master.md` (constraints in §0 are non-negotiable — especially: never run
> photoflow against the repo's `photoflow_work/`, there is a live enrich scan on it), then the three
> lane plans it links. Use superpowers:using-git-worktrees to create worktree(s) off `feature/enrich`,
> then superpowers:subagent-driven-development: one fresh subagent per task, model per the table in
> §1, review diff + tests between tasks, commit per task, full `uv run pytest` + ruff green before each
> commit. Merge lanes A → B → C per §2, run a fresh opus code review per lane, and leave the final
> branch unmerged for me. Report at the end: commits, test counts, anything you skipped and why.
