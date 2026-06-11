# photoflow

Incremental, non-destructive photo library organizer. Consolidates messy folders
from old computers and phone backups into a single `YYYY/MM` library, dedupes by
content, preserves folder/filename context as XMP metadata, and logs every action.

**Sources are never modified or deleted.** Everything is copy-only.

## Setup (desktop)

1. Python 3.11+ (`python --version`)
2. ExifTool on PATH:
   - Windows: `winget install ExifTool.ExifTool` (or download from exiftool.org —
     rename `exiftool(-k).exe` to `exiftool.exe` and put it on PATH)
   - macOS: `brew install exiftool`
   - Linux: `apt install libimage-exiftool-perl`
3. Python deps: `pip install Pillow ImageHash pillow-heif`
   - All optional, but without Pillow/ImageHash you lose near-dupe flagging and
     review thumbnails (exact dedupe still works). pillow-heif enables HEIC
     thumbnails/phash for iPhone photos.

## Workflow

```
python photoflow.py scan  "D:/OldLaptopDump" "E:/PhoneBackup"
python photoflow.py plan
python photoflow.py review            # only if plan queued near-dupe groups
# ... open photoflow_work/review.html, edit photoflow_work/decisions.csv ...
python photoflow.py apply --out "D:/Photos-Organized" [--dry-run]
python photoflow.py status
```

Later, when you find another USB stick:

```
python photoflow.py scan "F:/USB2009"
python photoflow.py plan
python photoflow.py apply --out "D:/Photos-Organized"
```

Only content not already in the library gets copied; the rest is logged as
duplicates. Re-running any step is safe (idempotent).

## What it does

- **Exact dedupe** — BLAKE2 content hash. Byte-identical files (your many 1:1
  dupes) keep one copy; all duplicate source paths are recorded into the
  keeper's metadata so no folder context is lost.
- **Near-dupe review, never auto-delete** — perceptual hash (pHash, hamming
  distance <= 5 via a BK-tree) flags resized/re-encoded lookalikes into
  `review.html` + `decisions.csv`. You set `keep`/`skip` per row; blanks stay
  held. Decisions survive regeneration.
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

## Files it manages

Images: jpg jpeg png heic heif tif tiff bmp gif webp
RAW: cr2 cr3 nef arw dng orf rw2 raf pef srw x3f
Video: mov mp4 m4v avi mts m2ts 3gp wmv mpg mpeg (exact dedupe only — no
perceptual matching, per your "no resized videos" assumption)

## Tuning

Constants at the top of `photoflow.py`:

- `NEAR_DUPE_THRESHOLD` (5) — raise to catch more aggressive re-encodes/crops
  in review, lower if too many false neighbors are queued.
- `BURST_WINDOW_S` (10) — max gap between burst frames.
- `MIN_YEAR` (1990) — dates before this are treated as bogus.
- `SLUG_MAX` (40) — original-filename portion kept in new names.

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

## Later: Proxmox / viewer path

The manifest + pipeline are portable: move `photoflow.py` and the workdir into
an LXC, point it at an SMB/NFS share, and it behaves identically. The natural
next step for viewing is an Immich LXC pointed at the organized library as an
external/read-only source — it indexes the XMP keywords/descriptions this
pipeline writes and layers ML tagging, faces, and search on top without owning
your files.
