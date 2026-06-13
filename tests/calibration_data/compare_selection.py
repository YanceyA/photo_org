"""Compare tag-SELECTION rules (not just thresholds) on the precision set.

scan.py applies a tag iff its ABSOLUTE score >= tag_score_review. But SigLIP2 scores vary
~100x by tag (cat ~0.10, beach ~0.001), so a single global cutoff drops correct low-scale
tags (beach/car/flowers/snow) even when the model ranks them #1 for the image. This scores
the baseline once, then evaluates several selection rules by their APPLIED-tag precision /
recall over the OI-verified (image, tag, present) labels:

  precision = applied & present / applied & verified   (of verified tags we'd apply, share right)
  recall    = applied & present / present              (of verified-present tags, share applied)

Rules: global absolute cutoff; per-image top-k; per-image relative (score >= alpha * max);
and relative-with-floor. Restricted to the 30 verified tags, so it's a proxy, but a fair one.

  uv run python tests/calibration_data/compare_selection.py "<model>" "<pretrained>"
"""

from __future__ import annotations

import csv
import sys
from collections import defaultdict
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parents[1] / "src"))

PRECISION = HERE / "openimages_precision.csv"
CACHE = HERE / "cache"


def _prf(applied_correct: int, applied_verified: int, n_present: int) -> tuple[float, float, float]:
    p = applied_correct / applied_verified if applied_verified else 1.0
    r = applied_correct / n_present if n_present else 0.0
    f = 2 * p * r / (p + r) if (p + r) else 0.0
    return p, r, f


def main(model: str, pretrained: str) -> None:
    from dataclasses import replace

    from PIL import Image

    from photoflow.config import Config
    from photoflow.enrich.tagger import ClipTagger

    cfg = replace(Config(), clip_model=model, clip_pretrained=pretrained)
    tagger = ClipTagger(cfg)

    # verified labels per image, and score every referenced image once
    verified: dict[str, dict[str, int]] = defaultdict(dict)  # image_id -> {tag: present}
    with open(PRECISION, encoding="utf-8") as f:
        for r in csv.DictReader(f):
            verified[r["image_id"]][r["vocab_tag"]] = int(r["present"])
    scored: dict[str, dict[str, float]] = {}
    for iid in verified:
        p = CACHE / f"{iid}.jpg"
        if p.exists():
            scored[iid] = dict(tagger.tag(Image.open(p).convert("RGB")))

    imgs = [i for i in verified if i in scored]
    n_present = sum(1 for i in imgs for t, pr in verified[i].items() if pr == 1)
    print(f"model={model}/{pretrained}  {len(imgs)} images, {n_present} verified-present labels\n")

    def applied_sets(rule) -> dict[str, set[str]]:
        return {i: rule(scored[i]) for i in imgs}

    def evaluate(name: str, rule) -> None:
        applied = applied_sets(rule)
        ac = av = 0
        for i in imgs:
            for t, pr in verified[i].items():
                if t in applied[i]:
                    av += 1
                    ac += pr
        p, r, f = _prf(ac, av, n_present)
        # also: avg applied tags per image (all tags, not just verified) = noise proxy
        avg_applied = sum(len(applied[i]) for i in imgs) / len(imgs)
        print(f"{name:34} P={p:.2f} R={r:.2f} F1={f:.2f}  avg_tags/img={avg_applied:.1f}")

    def global_cut(thr):
        return lambda sc: {t for t, s in sc.items() if s >= thr}

    def topk(k):
        return lambda sc: {t for t, _ in sorted(sc.items(), key=lambda x: x[1], reverse=True)[:k]}

    def topk_floor(k, floor):
        def rule(sc):
            top = sorted(sc.items(), key=lambda x: x[1], reverse=True)[:k]
            return {t for t, s in top if s >= floor}

        return rule

    def relmax(alpha, floor):
        def rule(sc):
            m = max(sc.values()) if sc else 0.0
            return {t for t, s in sc.items() if s >= alpha * m and s >= floor}

        return rule

    print("-- current production rule --")
    evaluate(f"global>= {cfg.tag_score_review} (current)", global_cut(cfg.tag_score_review))
    print("\n-- lower global cutoffs --")
    for thr in (0.004, 0.002, 0.001):
        evaluate(f"global>= {thr}", global_cut(thr))
    print("\n-- per-image top-k --")
    for k in (3, 5, 8):
        evaluate(f"top-{k}", topk(k))
    print("\n-- per-image top-k with floor --")
    for k, fl in ((5, 0.001), (5, 0.0005), (8, 0.001)):
        evaluate(f"top-{k} & >= {fl}", topk_floor(k, fl))
    print("\n-- per-image relative (score >= alpha*max) with floor --")
    for a, fl in ((0.5, 0.0005), (0.3, 0.0005), (0.2, 0.0005), (0.3, 0.001), (0.1, 0.0005)):
        evaluate(f"rel>= {a}*max & >= {fl}", relmax(a, fl))


if __name__ == "__main__":
    main(sys.argv[1], sys.argv[2])
