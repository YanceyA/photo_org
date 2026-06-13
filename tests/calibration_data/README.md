# Calibration photos (local, not committed)

Drop your own labelled photos here to calibrate the SigLIP tagger against your library's
style and tune the score thresholds. Nothing in this folder is committed except this README
and `.gitignore` — the repo stays asset-free.

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
