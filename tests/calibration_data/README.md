# Calibration photos (local, not committed)

Calibrate the SigLIP tagger against real photos with known labels. Nothing in this folder is
committed except this README, the `.gitignore`, the Open Images **manifest** + **builder**
script — the photos themselves are downloaded/cached locally, so the repo stays asset-free.

## Open Images subset (built-in, downloaded on demand)

`openimages_manifest.csv` lists a representative subset of [Open Images
V7](https://storage.googleapis.com/openimages/web/index.html) — K human-verified images per
vocabulary tag (Cat, Beach, Birthday cake, Dog, …). The calibration test downloads just those
images from the public CVDF mirror into `cache/` (gitignored, reused across runs) and checks
that the tagger ranks the right tag in each photo's top-8 (an aggregate recall gate).

Rebuild or resize the subset (e.g. 10 images/tag) with:

```
uv run python tests/calibration_data/build_openimages_manifest.py 10 40
```

## Your own photos (optional drop-in)

## How to use

1. Copy a handful of representative photos into this folder (jpg/png/heic).
2. Create `manifest.csv` here with two columns:

   ```csv
   file,expected_tags
   beach_2019.jpg,beach
   maya_birthday.jpg,birthday cake;child
   rex_in_yard.jpg,dog;backyard
   ```

   `expected_tags` is `;`-separated and each must be a tag from the vocabulary in
   `src/photoflow/enrich/tagger.py` (`FAMILY_VOCAB`). The test asserts at least one expected
   tag lands in the photo's top-8.

3. Run the suite (with `-s` to see the per-photo score report):

   ```
   uv run pytest tests/test_enrich_calibration.py -s
   ```

The `CALIBRATION` report prints each photo's score for its expected tags plus the top-3
overall, so you can set `tag_score_accept` / `tag_score_review` in `photoflow.toml`. As a
reference, on `ViT-SO400M-16-SigLIP2-384` a clear full-frame subject scores ~0.20 and an
unrelated tag ~0.00, so the defaults are `accept=0.10`, `review=0.035`.
