# photoflow

Incremental, non-destructive photo library organizer. Consolidates messy folders
from old computers and phone backups into a single `YYYY/MM` library, dedupes by
content, preserves folder/filename context as XMP metadata, and logs every action.

**Sources are never modified or deleted.** Everything is copy-only.

## Setup (desktop)

1. Python 3.11+ (`python --version`)
2. ExifTool on PATH:
   - Windows: `winget install OliverBetz.ExifTool` (or download from exiftool.org —
     rename `exiftool(-k).exe` to `exiftool.exe` and put it on PATH)
   - macOS: `brew install exiftool`
   - Linux: `apt install libimage-exiftool-perl`
3. Python deps: `uv sync` (installs the package plus dev/image deps). Without uv:
   `pip install -e .[images]` for the optional image extras.
   - The image extras (Pillow/ImageHash/pillow-heif) are optional, but without
     Pillow/ImageHash you lose near-dupe flagging and review thumbnails (exact
     dedupe still works). pillow-heif enables HEIC thumbnails/phash for iPhone
     photos.

## Workflow

```
uv run photoflow scan  "D:/OldLaptopDump" "E:/PhoneBackup"
uv run photoflow plan
uv run photoflow review               # only if plan queued near-dupe groups
# ... open photoflow_work/review.html in Chrome/Edge, click Keep on the photos
#     to keep, then "Save decisions.csv" (other browsers download the CSV;
#     hand-editing photoflow_work/decisions.csv still works) ...
uv run photoflow apply --out "D:/Photos-Organized" [--dry-run]
uv run photoflow status
```

(`python -m photoflow <cmd>` works too if the package is installed in your
active environment.)

Later, when you find another USB stick:

```
uv run photoflow scan "F:/USB2009"
uv run photoflow plan
uv run photoflow apply --out "D:/Photos-Organized"
```

Only content not already in the library gets copied; the rest is logged as
duplicates. Re-running any step is safe (idempotent).

## What it does

- **Exact dedupe** — BLAKE2 content hash. Byte-identical files (your many 1:1
  dupes) keep one copy; all duplicate source paths are recorded into the
  keeper's metadata so no folder context is lost.
- **Near-dupe review, never auto-delete** — perceptual hash (pHash, hamming
  distance <= 5 via a BK-tree) flags resized/re-encoded lookalikes into an
  interactive `review.html`: click Keep on the photo(s) to keep (the rest
  skip), then save straight back to `decisions.csv`. Untouched groups stay
  held; the CSV remains the source of truth, so hand-editing it still works.
  Decisions survive regeneration.
- **Bursts kept** — lookalike groups where every frame has EXIF time from the
  same camera within 10 s are treated as unique and kept silently.
- **RAW+JPEG pairs** — same stem in the same folder: both kept, tagged as a pair.
- **Live Photos** — image+video with the same stem: both kept; the video
  inherits the photo's date.
- **Date resolution cascade** — EXIF DateTimeOriginal/CreateDate -> filename
  patterns (`IMG_20190304_101112`, WhatsApp `IMG-...-WA`, `2019-03-04`) ->
  year in folder name -> file mtime. The source and confidence are recorded
  per file; dateless files land in `unknown-date/`.
- **Metadata merge tie-break** — in `decisions.csv`, set `merge_from_file_id`
  on a kept row to copy any *missing* tags (GPS, dates) from a skipped twin
  into the keeper (`exiftool -wm cg`, fill-only, never overwrites).
- **Provenance metadata** — original folder names become generic XMP
  `dc:subject` keywords and the original path(s) go into `dc:description`.
  Embedded for JPEG/PNG/TIFF/HEIC; written as `.xmp` sidecars for RAW and
  video. Generic Dublin Core fields, readable by digiKam, Immich, Lightroom,
  Apple Photos — retune later without re-copying.
- **Naming** — `YYYY/MM/YYYYMMDD_HHMMSS_<original-name-slug>_<hash8>.ext`.
  The hash suffix guarantees uniqueness and makes reruns collision-free.
- **Audit trail** — every action (scanned/copied/skipped/merged, with reasons)
  goes to the `actions` table in `photoflow_work/photoflow.db` and to
  `photoflow_work/logs/run_NNNN_<cmd>.jsonl`.

## Enrich: people + content tags (optional)

After you've built a library with `apply`, the **enrich** stage adds searchable people
and content tags *in place*, written as portable XMP that digiKam, Immich, Lightroom and
PhotoPrism read. It runs on the copied library only — sources are never touched.

- **People** — [InsightFace](https://github.com/deepinsight/insightface) detects faces and
  embeds them; HDBSCAN groups them into per-person clusters. You name each cluster once and
  every photo of that person inherits the name. Confirmed names are durable: re-running
  never re-clusters an already-named face.
- **Content tags** — [RAM++](https://github.com/xinyu1205/recognize-anything) (Recognize
  Anything Plus) tags each photo ("beach", "birthday cake", "dog"). If RAM++ isn't
  installed, it falls back to CLIP/SigLIP zero-shot tagging over a built-in family-photo
  vocabulary.
- **Verify only the edges** — the interactive `enrich_review.html` auto-accepts the
  confident bulk and surfaces only the marginal cases: low-confidence cluster members get a
  ⚠ flag, and uncertain tags are grouped *by tag* (one tag, many candidate photos) so you
  confirm "is this really a *boat*?" across the whole library at once. One click blacklists
  a junk tag (e.g. RAM's ubiquitous "person") everywhere.

### Setup

```
uv sync --extra enrich            # or: pip install -e .[enrich]
```

This pulls InsightFace + onnxruntime + scikit-learn + torch + open-clip-torch (the
CLIP/SigLIP tagger works out of the box). RAM++ is a separate, optional step (it isn't on
PyPI and needs a 3 GB checkpoint):

```
uv pip install "ram @ git+https://github.com/xinyu1205/recognize-anything.git"
# the checkpoint auto-downloads on first run, or pre-place it:
#   huggingface-cli download xinyu1205/recognize-anything-plus-model ram_plus_swin_large_14m.pth
```

**GPU note:** RAM++/CLIP use the GPU via torch automatically. InsightFace runs on **CPU by
default** because a GTX 1080-class (Pascal) card on Python 3.14 hits a known onnxruntime-gpu
crash ([#27588](https://github.com/microsoft/onnxruntime/issues/27588)); CPU face detection
is slower but reliable. Set `face_device = "cuda"` in `photoflow.toml` once you're on a
working CUDA stack.

### Workflow

```
uv run photoflow enrich scan      # detect faces + tag content (GPU/CPU, incremental)
uv run photoflow enrich cluster   # group unassigned faces into per-person clusters
uv run photoflow enrich review    # open photoflow_work/enrich_review.html
# ... name clusters, eject any ⚠ mis-grouped faces, confirm edge tags, Save both CSVs ...
uv run photoflow enrich apply [--dry-run]   # write XMP into the library files / sidecars
uv run photoflow enrich status
```

`enrich scan`/`apply` are incremental and idempotent — run them again after importing more
photos and only the new files are processed.

### What gets written

Per file, unioned with (never clobbering) the provenance keywords `apply` already wrote:

- content tags + person names → `dc:subject`, mirrored to `IPTC:Keywords` and
  `lr:HierarchicalSubject` (`People|<name>`) so Immich and digiKam pick them up;
- person names also → `Iptc4xmpExt:PersonInImage`;
- **MWG face regions** (`XMP-mwg-rs`, named rectangles) so digiKam / Lightroom / Immich show
  the face boxes.

Embedded for JPEG/PNG/TIFF/HEIC; `.xmp` sidecars for RAW and video. Tune thresholds
(`enrich_min_cluster_size`, `tag_score_accept`, `face_device`, `enrich_tagger`, …) in
`photoflow.toml` — see `src/photoflow/config.py`.

## Files it manages

Images: jpg jpeg png heic heif tif tiff bmp gif webp
RAW: cr2 cr3 nef arw dng orf rw2 raf pef srw x3f
Video: mov mp4 m4v avi mts m2ts 3gp wmv mpg mpeg (exact dedupe only — no
perceptual matching, per your "no resized videos" assumption)

## Tuning

Drop a `photoflow.toml` in the workdir to override defaults:

```toml
near_dupe_threshold = 8
burst_window_s = 5
min_year = 1985
```

- `near_dupe_threshold` (5) — raise to catch more aggressive re-encodes/crops
  in review, lower if too many false neighbors are queued.
- `burst_window_s` (10) — max gap between burst frames.
- `min_year` (1990) — dates before this are treated as bogus.
- `slug_max` (40) — original-filename portion kept in new names.
- `exiftool_batch` (200) — files per exiftool invocation.
- `image_ext` / `raw_ext` / `video_ext` / `sidecar_ext` — the extension sets
  listed above.

## Performance notes (tens of thousands of files)

Scan is the slow step: full-content hashing is disk-bound (expect roughly the
time it takes to read the data once), pHash adds a decode per image, ExifTool
runs in batches of 200. A 50k-file library is typically an evening, and it
commits progress as it goes — you can re-run scan and it skips anything
already fingerprinted (matched by path+size+mtime).

## Safety model

- Sources: read-only by design; nothing in the code writes to source paths
  except the read for hashing/metadata.
- `apply --dry-run` prints the full copy plan without touching anything.
- `decisions.csv` blanks = held, not deleted. Nothing is ever deleted —
  "skip" just means "not copied into the organized library."

## Development

```
uv sync            # install package + dev deps
just test          # uv run pytest
just lint          # uv run ruff check src tests
just fmt           # uv run ruff format src tests
```

pre-commit is available (`uv run pre-commit install`). The integration suite
needs exiftool on PATH; pure-logic tests (dates, naming, bktree, hashing) run
without it and the exiftool-dependent tests skip when it's absent.

## Later: Proxmox / viewer path

The manifest + pipeline are portable: install the package (or copy the repo)
and move the workdir into an LXC, point it at an SMB/NFS share, and it behaves
identically. The natural
next step for viewing is an Immich LXC pointed at the organized library as an
external/read-only source — it indexes the XMP keywords/descriptions this
pipeline writes and layers ML tagging, faces, and search on top without owning
your files.
