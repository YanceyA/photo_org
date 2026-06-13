"""Calibration suite: does the configured CLIP/SigLIP tagger suggest the right tags on
KNOWN photos?

Two sources of labelled photos, both keeping the repo asset-free:
  1. Built-in: real photographs bundled with scikit-image (`skimage.data`), already an
     [enrich] dependency - deterministic, no network, no committed binaries.
  2. Optional drop-in: put your own photos under tests/calibration_data/ with a manifest.csv
     (columns: file, expected_tags  -- expected_tags is ;-separated). Skipped if absent, so
     you can calibrate against your own library's style and tune the thresholds.

Assertions use TOP-K ranking + the configured banding (auto/review) rather than raw score
magnitudes, which differ per model. The report test prints per-photo scores so you can pick
tag_score_accept / tag_score_review for a new model.

Marked @pytest.mark.enrich: needs the [enrich] stack + downloads the configured model once.
"""

from __future__ import annotations

import csv
from pathlib import Path

import pytest

from photoflow.config import Config
from photoflow.enrich.deps import HAVE_CLIP
from photoflow.enrich.tagger import classify_tag

pytestmark = pytest.mark.enrich

CALIB_DIR = Path(__file__).parent / "calibration_data"


@pytest.fixture(scope="module")
def tagger():
    if not HAVE_CLIP:
        pytest.skip("[enrich] CLIP stack not installed")
    from photoflow.enrich.tagger import ClipTagger

    return ClipTagger(Config())


def _skimage_image(loader_name: str):
    import skimage.data as d
    from PIL import Image

    return Image.fromarray(getattr(d, loader_name)()).convert("RGB")


def _topk(tagger, im, k):
    return [t for t, _s in sorted(tagger.tag(im), key=lambda x: x[1], reverse=True)[:k]]


# Built-in cases. skimage's bundled photos only overlap the family vocab on "cat" (chelsea),
# so that's the deterministic regression anchor; richer calibration comes from the drop-in.
SKIMAGE_CASES = [
    ("chelsea", "cat close-up", ["cat"], 3),
]


@pytest.mark.parametrize("loader,label,expected,k", SKIMAGE_CASES)
def test_skimage_known_photo_ranks_expected_tag(tagger, loader, label, expected, k):
    top = _topk(tagger, _skimage_image(loader), k)
    assert any(e in top for e in expected), f"{label}: none of {expected} in top-{k}: {top}"


@pytest.mark.parametrize("loader,label,expected,k", SKIMAGE_CASES)
def test_skimage_obvious_tag_auto_accepted(tagger, loader, label, expected, k):
    # Regression guard for tagger calibration: the obvious tag on an obvious photo must land
    # in the 'auto' band under the configured thresholds. (The embedding-mean-ensembling bug
    # scored 'cat' at ~0.088, below threshold -> dropped; single-prompt scores it ~0.20.)
    cfg = Config()
    scores = dict(tagger.tag(_skimage_image(loader)))
    bands = {
        e: classify_tag(scores.get(e, 0.0), cfg.tag_score_accept, cfg.tag_score_review)
        for e in expected
    }
    assert "auto" in bands.values(), (
        f"{label}: {expected} not auto-accepted; "
        f"scores={[(e, round(scores.get(e, 0.0), 3)) for e in expected]} "
        f"thresholds(accept={cfg.tag_score_accept}, review={cfg.tag_score_review})"
    )


# --- optional user drop-in calibration set --------------------------------------------------


def _load_user_cases():
    manifest = CALIB_DIR / "manifest.csv"
    if not manifest.exists():
        return []
    cases = []
    with open(manifest, newline="", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            img = CALIB_DIR / row["file"]
            if img.exists():
                tags = [t.strip() for t in (row.get("expected_tags") or "").split(";") if t.strip()]
                cases.append((str(img), tags))
    return cases


USER_CASES = _load_user_cases()


@pytest.mark.skipif(not USER_CASES, reason="no tests/calibration_data/manifest.csv")
@pytest.mark.parametrize("img_path,expected", USER_CASES)
def test_user_known_photos_tagged(tagger, img_path, expected):
    from PIL import Image

    top = _topk(tagger, Image.open(img_path).convert("RGB"), k=8)
    assert any(e in top for e in expected), f"{img_path}: none of {expected} in top-8: {top}"


# --- Open Images subset: real photos with human-verified labels (downloaded on demand) ------
#
# A representative subset (K images per vocab tag) is listed in openimages_manifest.csv, built
# by build_openimages_manifest.py. Images download from the public CVDF mirror into cache/
# (gitignored) and are reused across runs. Skipped if the manifest is missing or offline.

OI_MANIFEST = CALIB_DIR / "openimages_manifest.csv"
OI_CACHE = CALIB_DIR / "cache"
OI_IMG_URL = "https://open-images-dataset.s3.amazonaws.com/{split}/{image_id}.jpg"
OI_RECALL_GATE = 0.85  # aggregate recall@8 must stay above this (observed 0.96 on the subset)


def _download_oi(image_id, split):
    import requests

    p = OI_CACHE / f"{image_id}.jpg"
    if p.exists():
        return p
    try:
        r = requests.get(OI_IMG_URL.format(split=split, image_id=image_id), timeout=30)
        if r.status_code == 200 and r.content[:2] == b"\xff\xd8":
            OI_CACHE.mkdir(exist_ok=True)
            p.write_bytes(r.content)
            return p
    except Exception:
        return None
    return None


@pytest.fixture(scope="module")
def oi_cases():
    if not OI_MANIFEST.exists():
        pytest.skip(
            "no openimages_manifest.csv (run tests/calibration_data/build_openimages_manifest.py)"
        )
    with open(OI_MANIFEST, encoding="utf-8") as f:
        rows = list(csv.DictReader(f))
    cases = [
        (p, r["expected_tag"])
        for r in rows
        if (p := _download_oi(r["image_id"], r["split"])) is not None
    ]
    if not cases:
        pytest.skip("could not download any Open Images calibration photos (offline?)")
    return cases


def test_openimages_recall_at_8(tagger, oi_cases):
    from collections import defaultdict

    from PIL import Image

    hits: dict[str, list[bool]] = defaultdict(list)
    for path, tag in oi_cases:
        top8 = _topk(tagger, Image.open(path).convert("RGB"), 8)
        hits[tag].append(tag in top8)
    flat = [h for hs in hits.values() for h in hs]
    recall = sum(flat) / len(flat)
    by_tag = "  ".join(f"{t}={sum(hs)}/{len(hs)}" for t, hs in sorted(hits.items()))
    print(f"\nOpen Images recall@8 = {recall:.2f} over {len(flat)} photos ({len(hits)} tags)")
    print(f"  {by_tag}")
    assert recall >= OI_RECALL_GATE, f"recall@8 {recall:.2f} < gate {OI_RECALL_GATE}\n  {by_tag}"


def test_calibration_report(tagger):
    """Not a gate - prints what the configured model scores on the known photos so you can
    tune tag_score_accept / tag_score_review. Run with `-s` to see it."""
    cfg = Config()
    print(
        f"\nCALIBRATION  model={cfg.clip_model}/{cfg.clip_pretrained}  "
        f"accept={cfg.tag_score_accept} review={cfg.tag_score_review}"
    )
    cases = [(_skimage_image(ld), lbl, exp) for ld, lbl, exp, _k in SKIMAGE_CASES]
    cases += [
        (__import__("PIL.Image", fromlist=["Image"]).open(p).convert("RGB"), p, exp)
        for p, exp in USER_CASES
    ]
    for im, label, expected in cases:
        sc = dict(tagger.tag(im))
        exp = "  ".join(f"{e}={sc.get(e, 0.0):.3f}" for e in expected)
        top = "  ".join(
            f"{t}={s:.3f}" for t, s in sorted(sc.items(), key=lambda x: x[1], reverse=True)[:3]
        )
        print(f"  {str(label)[:28]:28} expected[{exp}]  top[{top}]")
