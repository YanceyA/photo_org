# photoflow `enrich` — design

**Status:** implemented (feature/enrich)
**Date:** 2026-06-13
**Branch:** `feature/enrich`

## Goal

Add a non-destructive **enrichment** stage that scans the *organized library* photoflow
already produced, adds two kinds of metadata, lets a human verify only the marginal
cases, and writes the results back into the files as portable XMP:

1. **People** — InsightFace face detection + 512-d embeddings → HDBSCAN clusters → human
   names each cluster once → every photo of that person inherits the name.
2. **Content tags** — RAM++ (Recognize Anything Plus) tagger, with CLIP zero-shot as an
   automatic fallback → "beach", "birthday cake", "dog".

Output is written as `dc:subject` keywords (read by Immich, digiKam, PhotoPrism,
Lightroom, Windows) + IPTC `Keywords` mirror + **MWG face regions** (named rectangles
read by digiKam / Lightroom / PhotoPrism). The library's own files are the durable,
portable store; the SQLite tables are a disposable working cache.

## Decisions (locked with user 2026-06-13)

- **Tagger:** RAM++ primary, CLIP zero-shot automatic fallback. Both behind an optional
  `[enrich]` extra; RAM++ checkpoint + `ram` git package are a documented extra step.
- **Where it runs:** on the **copied library** (`status='copied'`, `dest_path` set). Reads
  the keepers, writes XMP into the library files / sidecars in place. Re-runnable;
  enriches today's library without re-applying.
- **People format:** `dc:subject` keywords **+ MWG regions**. This softens invariant #6
  ("XMP stays generic Dublin Core") — regions are an additive, standards-based extension,
  keywords remain the portable baseline.

## Non-negotiables preserved

- **Sources stay read-only.** enrich only ever reads `dest_path` (the copied library) and
  writes XMP into those copies / their sidecars. No source path is touched. (Invariant #1)
- **People are never auto-written without confirmation** — clustering only *proposes*;
  names are durable only after the human assigns them (mirrors invariant #2/#3 philosophy:
  only human-confirmed state is durable).
- **Embed vs sidecar split unchanged** — embed for jpg/jpeg/png/tif/tiff/heic/heif,
  `.xmp` sidecar for RAW/video. (Invariant #6)
- **enrich apply unions with existing keywords**, never clobbers photoflow's provenance
  `dc:description` / folder keywords written by `apply`.

## CLI surface

Nested subcommands under `enrich` (new; existing flat commands untouched):

```
photoflow enrich scan      # detect faces + embeddings + content tags (GPU, incremental)
photoflow enrich cluster   # (re)run HDBSCAN over unassigned faces; carry named persons forward
photoflow enrich review    # emit enrich_review.html + faces.csv + tags.csv
photoflow enrich apply     # write people + tags into library files (XMP embed/sidecar + MWG)
photoflow enrich status    # counts: faces, clusters, named persons, tags by status
```

`cli.py` gains an `enrich` subparser with its own `enrich_step` sub-subparser; dispatch
stays the dict-on-name pattern. Command modules keep the shared signature
`(conn, workdir, run_id, log_fh, args, cfg)`.

## Data model (additive; `CREATE TABLE IF NOT EXISTS`)

```sql
CREATE TABLE IF NOT EXISTS persons (
    id INTEGER PRIMARY KEY,
    name TEXT UNIQUE NOT NULL,
    created TEXT
);

CREATE TABLE IF NOT EXISTS faces (
    id INTEGER PRIMARY KEY,
    file_id INTEGER NOT NULL,
    bbox TEXT,                 -- json [x1,y1,x2,y2] pixel coords in the library image
    det_score REAL,
    embedding BLOB,            -- float32 512-d, L2-normalized, ndarray.tobytes()
    img_w INTEGER, img_h INTEGER,
    person_id INTEGER,         -- DURABLE assignment (NULL = unassigned)
    cluster_id INTEGER,        -- EPHEMERAL: last HDBSCAN run (NULL = noise/assigned)
    cluster_prob REAL,         -- HDBSCAN membership prob; low = edge case for review
    thumb TEXT,                -- relative path to face-crop thumbnail
    FOREIGN KEY(file_id) REFERENCES files(id),
    FOREIGN KEY(person_id) REFERENCES persons(id)
);
CREATE INDEX IF NOT EXISTS idx_faces_file ON faces(file_id);
CREATE INDEX IF NOT EXISTS idx_faces_cluster ON faces(cluster_id);
CREATE INDEX IF NOT EXISTS idx_faces_person ON faces(person_id);

CREATE TABLE IF NOT EXISTS tags (
    id INTEGER PRIMARY KEY,
    file_id INTEGER NOT NULL,
    tag TEXT NOT NULL,
    source TEXT,               -- 'ram' | 'clip'
    score REAL,                -- per-tag confidence (NULL if model gives none)
    status TEXT DEFAULT 'auto',-- auto (high-conf accept) | review (edge band) | rejected
    UNIQUE(file_id, tag),
    FOREIGN KEY(file_id) REFERENCES files(id)
);
CREATE INDEX IF NOT EXISTS idx_tags_file ON tags(file_id);
CREATE INDEX IF NOT EXISTS idx_tags_tag ON tags(tag);

CREATE TABLE IF NOT EXISTS enrich_state (   -- incremental skip, like scan's size+mtime rule
    file_id INTEGER PRIMARY KEY,
    faces_done INTEGER DEFAULT 0,
    tags_done INTEGER DEFAULT 0,
    applied INTEGER DEFAULT 0,
    ts TEXT
);
```

Durable vs ephemeral mirrors the existing manifest philosophy:
- **Durable:** `persons`, `faces.person_id` (human-confirmed), `faces.embedding` (expensive
  to recompute), `tags` with confirmed status.
- **Ephemeral:** `faces.cluster_id` / `cluster_prob` — recomputed every `enrich cluster`.

## Pipeline

### `enrich scan` (compute — GPU, incremental)

For each `files` row with `status='copied'`, a decodable still image
(`kind='image'`; RAW/video skipped for detection in v1), and no `enrich_state` row with
`faces_done` & `tags_done`:

1. Decode once (Pillow → RGB ndarray; BGR view for InsightFace).
2. **Faces:** `FaceAnalysis(buffalo_l)` → per face: normed embedding, bbox, det_score.
   Filter by `enrich_face_min_score`. Store + write a padded face-crop thumbnail.
3. **Tags:** RAM++ (or CLIP fallback) → tags (+ scores where available). Threshold into
   `auto` (≥ `tag_score_accept`), `review` (edge band), or dropped (< `tag_score_review`).
4. Mark `enrich_state` done. Commit every N files (like scan).

Batched on GPU where the model allows (RAM/CLIP batch; InsightFace is per-image). Device
auto-detected (CUDA provider when present, else CPU). Heavy deps gated like `HAVE_PIL`:
missing faces lib → skip faces with a NOTE; missing tagger → skip tags; both missing →
tell the user to install `photoflow[enrich]`.

### `enrich cluster` (group — re-runnable)

1. Pull all `faces` with `person_id IS NULL` (assigned faces stay put — durable).
2. *(optional)* Nearest-centroid suggest: an unassigned face within a tight cosine
   distance of an existing named person is pre-suggested that name (human still confirms).
3. **HDBSCAN** (`sklearn.cluster.HDBSCAN`) over L2-normalized embeddings, euclidean metric;
   `min_cluster_size`, `cluster_selection_method` from config. Assign `cluster_id` +
   `cluster_prob`; noise → `cluster_id = NULL`.
4. Representative (medoid) face per cluster for display.

Named persons carry forward automatically because their faces are excluded from
re-clustering. New photos' faces just join the next cluster run.

### `enrich review` (verify — the innovation surface)

Emit a single self-contained `enrich_review.html` + `faces.csv` + `tags.csv`, reusing the
proven review-page pattern (embedded JSON payload, File System Access API save, localStorage
crash-insurance). Two sections:

**People — cluster confirmation**
- Clusters as face-crop grids, sorted by size (cohesion). One **name input** per cluster
  (autocomplete from existing persons). Naming propagates to all member faces in-browser.
- **Edge-case-only focus:** a "needs attention" filter surfaces only low-cohesion clusters
  and low-`cluster_prob` members; the confident bulk can be confirmed in one click.
- Per-face **eject** (low-prob member that doesn't belong → unknown pool), cluster
  **ignore** (not a person), **merge** two clusters, and an **unknown/noise pool** at the
  bottom for optional manual assignment.
- **Propagation preview:** naming shows "+N photos will get 'Mum'".
- **Co-occurrence teaser:** once ≥2 people named, a small who-appears-with-whom readout —
  the seed of the photo-relations graph.

**Tags — RAM++/CLIP confirmation**
- High-confidence (`auto`) tags shown as an accepted summary count — no clicking needed.
- **Tag-centric edge review:** uncertain-band tags grouped **by tag** (one tag, many
  candidate photos) so "is this really a *boat*?" is answered across all photos at once.
- **Global blacklist:** one click rejects a junk tag everywhere (e.g. RAM's ubiquitous
  "person"). **Threshold slider** live-updates the accepted count.

Decision files (flat, apply-friendly, carry-forward by id like `decisions.csv`):
- `faces.csv`: `face_id, file_id, cluster_id, cluster_prob, suggested_person, person, decision`
  (`decision` ∈ keep/eject/ignore; `person` filled by naming).
- `tags.csv`: `file_id, tag, source, score, suggestion, decision` (keep/reject).

### `enrich apply` (writeback — portable, idempotent)

1. `faces.csv` → upsert `persons`, set durable `faces.person_id` for kept rows.
2. `tags.csv` → resolve accepted tags per file.
3. Per library file (`dest_path`), **read-modify-write union** (never clobber):
   - read current `XMP-dc:Subject` via the existing batched exiftool reader;
   - new `dc:subject` = existing ∪ content tags ∪ person names (sorted, deduped);
   - mirror to `IPTC:Keywords`; optional `lr:hierarchicalSubject` `People/<name>`;
   - **MWG regions** per face (name + normalized center/size from bbox + img dims);
   - embed for `EMBED_EXT`, else `.xmp` sidecar. Leave `dc:description` provenance intact.
4. Mark `enrich_state.applied`. Re-apply is idempotent (set-union, not append).

## Config additions (`src/photoflow/config.py`, all defaulted)

`enrich_tagger` (ram|clip|auto), `ram_checkpoint`, `ram_image_size`, `clip_model`,
`clip_pretrained`, `enrich_device` (auto|cuda|cpu), `enrich_batch`,
`enrich_face_min_score`, `enrich_min_cluster_size`, `enrich_cluster_prob_floor`,
`tag_score_accept`, `tag_score_review`, `face_crop_pad`, `write_mwg_regions`,
`write_iptc_keywords`, `people_keyword_prefix`. Unknown-key-fatal loader already accepts
new valid fields.

## Module map (new files under `src/photoflow/`)

- `enrich/__init__.py` — package
- `enrich/deps.py` — optional-import gates (`HAVE_INSIGHTFACE`, `HAVE_TORCH`, `HAVE_RAM`,
  `HAVE_OPENCLIP`, `HAVE_SKLEARN`) + device selection.
- `enrich/faces.py` — InsightFace wrapper (detect → embeddings/bbox/score), pure-ish.
- `enrich/tagger.py` — RAM++ loader + inference, CLIP fallback, score thresholding.
- `enrich/clustering.py` — **pure**: embeddings → labels+probs, medoid, carry-forward,
  nearest-person suggest. Fully unit-testable with synthetic gaussians (no GPU).
- `enrich/regions.py` — **pure**: bbox+dims → normalized MWG region; exiftool argfile lines
  for regions + subject union. Unit-testable like `xmp.py`.
- `enrich/scan.py`, `enrich/cluster.py`, `enrich/review.py`, `enrich/apply.py`,
  `enrich/status.py` — command modules (shared signature).
- `enrich/page.py` — **pure**: payload builders + HTML/JS template (testable like
  `review_page.py`).

## Dependencies

```toml
[project.optional-dependencies]
enrich = [
  "insightface", "onnxruntime", "scikit-learn", "numpy",
  "opencv-python-headless", "torch", "open-clip-torch",
]
```
RAM++ documented as a separate step (git package + checkpoint), with CLIP as the
always-installable tagger so `[enrich]` alone is fully functional. GPU users swap
`onnxruntime`→`onnxruntime-gpu` and install the CUDA torch build (documented).

## Testing (TDD; new `enrich` marker)

Pure-logic, no GPU/models — the bulk:
- `clustering.py`: synthetic gaussian embeddings → expected cluster count; carry-forward
  keeps named persons; nearest-person suggest threshold; medoid selection.
- `regions.py`: bbox+dims → MWG normalized geometry round-trip; argfile line builders;
  subject set-union dedupe.
- tag thresholding: score → auto/review/dropped bands; global-blacklist application.
- decision CSV round-trip + carry-forward by id (like `test_review_page`).
- `page.py`: payload builders + template contains required JS hooks.

Model-dependent: `@pytest.mark.enrich` (skip if libs absent), `@pytest.mark.exiftool` for a
real region-write round-trip. No binary assets — synthetic only; real-face detection tests
are smoke/skip.

## Resolved from research (2026-06-13 verification workflow, high confidence)

- **RAM++**: `from ram.models import ram_plus`; `from ram import inference_ram, get_transform`.
  Checkpoint `ram_plus_swin_large_14m.pth` (3.01 GB) from HF `xinyu1205/recognize-anything-plus-model`.
  `inference_ram` returns `(english, chinese)` tag strings split on `|` with **no per-tag
  score** → RAM tags are trusted as 'auto'; CLIP/SigLIP drives the score-banded review.
- **InsightFace**: `.normed_embedding` (raw `.embedding` is NOT normalized); BGR input;
  buffalo_l auto-downloads. **GPU broken on this rig** — Pascal sm_61 + Py3.14 forces
  onnxruntime-gpu ≥1.26, which crashes (#27588). → faces default to **CPU**; torch
  (RAM/CLIP) still uses CUDA.
- **HDBSCAN**: `sklearn.cluster.HDBSCAN` (built in since 1.3); L2-normalize + euclidean;
  `store_centers='medoid'`; `probabilities_` flags edge cases; `copy=True` silences a warning.
- **MWG regions**: repeat flattened `-XMP-mwg-rs:Region*` tags per face; `RegionAreaX/Y` is the
  box **center**, normalized; `RegionAreaUnit=normalized`, `AppliedToDimensionsUnit=pixel`;
  writing the list **replaces** it (idempotent). Verified end-to-end with real exiftool 13.59.
- **CLIP fallback**: open-clip-torch; **SigLIP** (`ViT-B-16-SigLIP`/`webli`) for calibrated
  sigmoid scores (NOT softmax — that forces single-label); prompt ensembling.
- **Portability correction**: Immich *does* now import MWG regions (opt-in, PR #6455) but
  prioritizes **IPTC:Keywords / lr:HierarchicalSubject** over plain `dc:subject` → the
  writeback mirrors to all three + `PersonInImage`, via read-union-replace.
