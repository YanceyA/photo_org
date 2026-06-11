# Interactive review page — design

**Date:** 2026-06-12
**Status:** validated with user, ready for implementation planning

## Problem

The review step currently means eyeballing a static `review.html` while hand-editing
`decisions.csv` row by row. For libraries with many near-dupe groups this is slow and
error-prone. The comparison cards also lack the facts needed to choose (pixel
dimensions, disk size, file type).

## Decision summary (user-validated)

| Question | Decision |
|---|---|
| Save model | Static HTML + Save button. No server. localStorage as crash insurance, File System Access API (Chrome/Edge) to write `decisions.csv` back into the workdir, download fallback elsewhere. |
| Click model | Click marks keepers (multiple allowed). Once a group has ≥1 keeper, unmarked members become `skip`. Zero keepers = group on hold (blank rows, exactly today's semantics). |
| merge_from_file_id | Supported: a small "donate metadata" toggle on skipped cards sets `merge_from_file_id` on the group's keeper row(s). Auto-clears when keepers change. Multiple keepers → donor applies to all (no donor-target picker; YAGNI). |
| Card contents | Thumbnail; `W×H · MP · size · TYPE` (RAW/video badge); camera model; date taken; source path. Best resolution and largest size in a group get a highlight; suggested keeper (most pixels, same heuristic as the CSV `suggestion` column) starts with a dashed outline. |
| Thumbnail click | Opens the original source file full-size in a new tab (`file://` link). Selection happens via a Keep button / card border, not the image. |
| Header | Sticky: `Decided N / M groups`, hide-decided-groups toggle, Save button with dirty indicator. |

## Architecture

`photoflow review` behavior is unchanged in its contract:

1. Regenerates `decisions.csv` with carry-forward by file_id (**invariant #4 untouched**).
2. Generates thumbnails as today.
3. Writes `review.html` — now containing:
   - a JSON blob per review file: `file_id, group_id, source_path, width, height,
     size, ext, kind, camera, date_taken, suggestion, decision, merge_from_file_id`
     (decision/merge from the just-written CSV, so carry-forward reaches the page);
   - a vanilla-JS app (single template string in `review.py`, no dependencies, no build step).

`apply.py` is untouched — it keeps reading `decisions.csv`. The CSV remains the
source of truth; the HTML is an editor for it.

## State model (in-page)

- Initial state = embedded CSV decisions, overlaid with `localStorage` (key scoped to
  the workdir path) so a closed tab loses nothing.
- Every click writes localStorage immediately.
- **Save** serializes state to a CSV byte-compatible with today's format: same header
  and columns, `keep`/`skip` filled, undecided groups blank. Uses
  `showSaveFilePicker` (file handle reused for re-saves) with `<a download>` fallback.
- Partial saves are fine: blank rows hold, `apply` reports them as held, repeat later.

## Edge cases

- **Stale page after re-plan:** save only emits rows for embedded file_ids; `review`
  regeneration carries forward by file_id, so a stale save cannot corrupt state.
  Decisions for files no longer in review are dropped on regeneration — same as today.
- **localStorage vs CSV conflict:** CSV is baseline, localStorage overlays. After a
  save + regenerate, they converge.
- **No Pillow / unreadable preview:** "(no preview)" card, stats still shown,
  still selectable.
- **Donate toggle validity:** only shown on skipped cards in groups with ≥1 keeper;
  cleared whenever the group's keeper set changes.

## Testing

- Pure functions with pytest coverage: CSV row writer, JSON blob builder, and a
  round-trip test (prior decisions → blob → simulated save output → identical CSV).
- HTML/JS verified by opening the page; optional Playwright pass post-build to
  confirm click → save → `apply` consumes the result.

## Out of scope (YAGNI)

- Local server / live saving (`--serve`)
- Donor-target picker for multi-keeper groups
- Side-by-side zoom/diff view
