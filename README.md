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
- **Content tags** — CLIP/SigLIP zero-shot tagging over a built-in family-photo vocabulary
  ("beach", "birthday cake", "dog") with calibrated per-tag scores. Optionally
  [RAM++](https://github.com/xinyu1205/recognize-anything) (Recognize Anything Plus) for
  richer tags — but RAM++ is unmaintained and pins an old `transformers` that won't run on
  Python 3.14, so SigLIP is the practical tagger here (see RAM++ note below).
- **Verify only the edges** — the interactive `enrich_review.html` auto-accepts the
  confident bulk and surfaces only the marginal cases: low-confidence cluster members get a
  ⚠ flag, and uncertain tags are grouped *by tag* (one tag, many candidate photos) so you
  confirm "is this really a *boat*?" across the whole library at once. One click blacklists
  a junk tag (e.g. RAM's ubiquitous "person") everywhere.

### Setup

```
uv sync --extra enrich            # or: pip install -e .[enrich]
```

This pulls InsightFace + onnxruntime + scikit-learn + torch + open-clip-torch +
transformers — the **CLIP/SigLIP tagger works out of the box**, no extra step. The default
tagger is **SigLIP 2** (`ViT-SO400M-16-SigLIP2-384`), which gives calibrated per-tag scores.
Just run the workflow below.

**RAM++ (optional, advanced).** RAM++ gives richer tags but is unmaintained: it pins
`transformers==4.25.x`, whose APIs were removed in modern transformers, and that old version
won't run on Python 3.14. So on a 3.14 setup RAM++ won't load and enrich falls back to
SigLIP automatically. If you really want RAM++, run it in a **separate Python 3.11/3.12
environment** with a compatible transformers, install the git package + 3 GB checkpoint, and
set `enrich_tagger = "ram"`:

```
uv pip install "ram @ git+https://github.com/xinyu1205/recognize-anything.git"
#   checkpoint auto-downloads on first run (3 GB), or pre-place ram_plus_swin_large_14m.pth
# NOTE: `uv sync` reconciles the venv to declared deps and will REMOVE this manually
#       pip-installed `ram` package — reinstall it after any `uv sync`.
```

**GPU note.** `uv sync --extra enrich` installs the **CPU** build of torch, so CLIP/SigLIP
runs on CPU out of the box (~5–6 min over the calibration set). To use an NVIDIA GPU, install
a matching CUDA build — verified on a GTX 1080 Ti (Pascal `sm_61`) with CUDA 12.6:

```
uv pip install "torch==2.12.0+cu126" "torchvision==0.27.0+cu126" \
    --index-url https://download.pytorch.org/whl/cu126
# Pick the cuXXX wheel matching your driver. cu126 still ships Pascal sm_61 kernels;
# cu128 (CUDA 12.8 / Blackwell-era) dropped them, so it won't run on a 1080 Ti.
# Like the `ram` package, `uv sync` reverts torch to the CPU build — reinstall after any sync.
```

InsightFace runs on **CPU by default** because a GTX 1080-class (Pascal) card on Python 3.14
hits a known onnxruntime-gpu crash
([#27588](https://github.com/microsoft/onnxruntime/issues/27588)); CPU face detection is
slower but reliable. Set `face_device = "cuda"` in `photoflow.toml` once you're on a working
CUDA stack.

### Workflow

```
uv run photoflow enrich scan      # detect faces + tag content (GPU/CPU, incremental)
uv run photoflow enrich cluster   # group unassigned faces into per-person clusters
uv run photoflow enrich review    # open photoflow_work/enrich_review.html
# ... name clusters, eject any ⚠ mis-grouped faces, "not interested" on don't-care clusters,
#     confirm edge tags, Save both CSVs ...
uv run photoflow enrich apply [--dry-run]   # write XMP into the library files / sidecars
uv run photoflow enrich status
```

`enrich scan`/`apply` are incremental and idempotent — run them again after importing more
photos and only the new files are processed.

In the **review page**: naming a cluster labels everyone in it; **eject** removes a single
mis-grouped face (it stays eligible for re-grouping); **not interested** dismisses a whole
cluster you don't care to tag — on the next `apply` those faces are marked *ignored* and never
resurface in re-cluster or review again.

### Improving face grouping (re-cluster loop)

Clustering is unsupervised, so it can split one person across separate bursts and leave
infrequent faces unassigned. Once you've named some people, turn that into supervision and
re-group the rest — each round gets stronger as you name more:

```
uv run photoflow enrich apply               # bake named clusters into durable person_ids
uv run photoflow enrich assign [--dry-run]  # Layer 0: auto-assign unassigned faces that are
                                            #   near a named person's centroid (mops up
                                            #   burst-fragments + noise of known people)
uv run photoflow enrich cluster             # Layer 1/2: regroup only the still-unnamed faces
uv run photoflow enrich review              # confirm, name more, repeat
```

`enrich assign` only touches unassigned, non-ignored faces, so confirmed names and
"not interested" faces are never disturbed. Tune its bar with `--min-sim` (default
`enrich_auto_assign_threshold = 0.6`).

**Picking `--min-sim`:** every run writes a static `assign_review_sim<val>.html` to the workdir
that shows each *proposed* face grouped under the person it would join (strongest match first,
cosine score under each) next to a strip of that person's known faces. Generate a few at
different thresholds and open them side by side — pick the value just above where wrong faces
start to appear. The counts alone won't tell you that; the page will.

```
uv run photoflow enrich assign --dry-run --min-sim 0.6   # -> assign_review_sim0.60.html
uv run photoflow enrich assign --dry-run --min-sim 0.5   # -> assign_review_sim0.50.html
uv run photoflow enrich assign --dry-run --min-sim 0.45  # -> assign_review_sim0.45.html
```

**Fixing duplicate / misspelled names.** Typing a name before it's in the autocomplete (or with
different casing — `Yancey Arrington` vs `Yancey arrington`) creates a *separate* person, which
splits that person's faces and weakens their assign centroid. Fold them back together:

```
uv run photoflow enrich merge "Deirdre Hough" "Deidre Hough" "Deirdre hough"
```

The first name is the one to keep (created if it doesn't exist yet); the rest are repointed into
it and deleted. Re-run `enrich apply` afterwards — it rewrites the people + face-region tags with
the canonical name. (One caveat: `dc:Subject`/`IPTC:Keywords` are union-only, so a misspelling
already written into *previously applied* files lingers as a plain keyword until cleaned out.)

To fight per-burst over-splitting, raise `enrich_cluster_selection_epsilon` (Layer 1) in
`photoflow.toml` — it fuses clusters closer than that cosine distance into one person. Start
small (`0.1`–`0.3`) and verify it never merges two *distinct* named people. To recover people
who appear in only a few photos (Layer 2), lower `enrich_min_cluster_size` (e.g. `3`). Your
already-named faces are ground truth — sweep these knobs and check no two named people merge.

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

### Calibrating content tags

A calibration suite checks the tagger against real photos with known labels:

```
uv run pytest tests/test_enrich_calibration.py -s
```

It pulls a representative subset of **Open Images** (human-verified labels, ~6 photos per
vocab tag, downloaded on demand and cached — see `tests/calibration_data/`) and measures
**recall@8** — does the right tag land in each photo's top-8? On `ViT-SO400M-16-SigLIP2-384`
that's **0.98**, so the tagger ranks tags well on real, cluttered photos. You can also drop
your own labelled photos in `tests/calibration_data/` with a `manifest.csv`.

**Why SigLIP 2?** A 15-model bake-off (`tests/calibration_data/run_bakeoff.py`) benchmarked
the strongest open_clip zero-shot models — SigLIP2 variants, DFN5B, MetaCLIP2, EVA02, LAION
ViT-H/bigG, CLIPA, ConvNeXt — on recall **and** a precision metric (per-tag present-vs-absent
AUC over verified negatives). `ViT-SO400M-16-SigLIP2-384` won outright; every non-SigLIP model
scored measurably lower. So the model isn't the bottleneck — the vocabulary and thresholds are.

Two calibration facts worth knowing. (1) SigLIP 2's sigmoid scores are **low and vary ~100×
by tag** — a prominent subject (cat, cake) scores ~0.10 but a correct-but-low-scale tag (car,
flowers, snow) scores ~0.002–0.01 *even when ranked #1 for the photo*. `scan` applies a tag
iff its absolute score ≥ `tag_score_review`, so the floor must be low or those correct tags
are silently dropped — at the old 0.008 floor only ~0.3 tags/photo survived. The retuned
`tag_score_accept` (0.02) / `tag_score_review` (0.0015) roughly double recall; the auto band
stays ~0.81-precise and the review step + blacklist filter the rest. (2) The vocabulary is
pruned of tags the model can't do (`child` scored below chance and is covered by face
clustering; `books` scored higher on non-book photos). Retune with
`tests/calibration_data/tune_thresholds.py` / `compare_selection.py` if you change `clip_model`
or the vocab.

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
