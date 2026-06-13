# Calibration data + tagger tuning tools

Calibrate the SigLIP tagger against real photos with known labels. Committed here: this
README, `.gitignore`, the two Open Images label files (`openimages_manifest.csv`,
`openimages_precision.csv`) and the builder/tuning scripts. The photos themselves download to
`cache/` (gitignored) on demand, so the repo stays asset-free.

## The two label sets

- **`openimages_manifest.csv`** — recall set: ~6 human-verified-**present** [Open Images
  V7](https://storage.googleapis.com/openimages/web/index.html) images per vocab tag. The
  calibration test downloads these and gates on **recall@8** (does the right tag land in each
  photo's top-8). Rebuild: `uv run python build_openimages_manifest.py [K] [MAX_CANDIDATES]`.
- **`openimages_precision.csv`** — precision set: the manifest's present images **plus**
  verified-**absent** images per tag. Powers a scale-free **per-tag present-vs-absent AUC**
  (does the model score a tag higher where it's present than where it's verified absent?),
  which compares SigLIP-sigmoid and plain-CLIP-cosine fairly. Rebuild:
  `uv run python build_openimages_precision.py [K_NEG]`.

Both builders derive their tag list from `FAMILY_VOCAB`, so regenerate them after editing the
vocab.

## Tuning tools (need the [enrich] stack; fast on GPU, ~minutes on CPU)

- **`run_bakeoff.py`** + `model_bakeoff.py` — benchmark a curated set of strong open_clip
  models (recall@{1,3,5,8}, MRR, precision AUC) in isolated processes. A 15-model run found
  `ViT-SO400M-16-SigLIP2-384` is the best model here; no swap beats it.
- **`tune_thresholds.py "<model>" "<pretrained>"`** — full per-tag report (recall@8, AUC, mean
  present/absent score) + a global precision/recall sweep. Shows which tags the model can't
  separate and where to put thresholds.
- **`compare_selection.py "<model>" "<pretrained>"`** — compares tag-*selection* rules (global
  cutoff vs per-image top-k vs relative-to-max) by applied-tag precision/recall. This is what
  showed the old 0.008 floor applied only ~0.3 tags/photo, and that simply lowering the global
  floor (to `tag_score_review = 0.0015`) ~doubles recall without a mechanism redesign.

## Your own photos (optional drop-in)

1. Copy representative photos into this folder (jpg/png/heic).
2. Create `manifest.csv` with two columns (`expected_tags` is `;`-separated; each must be a
   tag in `FAMILY_VOCAB`):

   ```csv
   file,expected_tags
   beach_2019.jpg,beach
   maya_birthday.jpg,birthday cake;baby
   rex_in_yard.jpg,dog;backyard
   ```

   The test asserts at least one expected tag lands in the photo's top-8.
3. Run the suite (`-s` prints the per-photo `CALIBRATION` score report):

   ```
   uv run pytest tests/test_enrich_calibration.py -s
   ```

## Reference numbers (`ViT-SO400M-16-SigLIP2-384`/`webli`)

recall@8 ≈ **0.98**. Scores are low and tag-dependent: a clear full-frame subject (cat, cake)
scores ~0.10–0.20, a correct-but-low-scale tag (car, flowers, snow) ~0.002–0.01. Hence the
defaults `tag_score_accept = 0.02`, `tag_score_review = 0.0015` — re-tune with the tools above
if you change `clip_model` or the vocab.
