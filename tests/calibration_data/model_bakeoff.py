"""One-model bake-off worker: score the Open Images calibration set with a given open_clip
model via the PRODUCTION ClipTagger path, and print one JSON line of metrics.

Driven by run_bakeoff.py, which runs this once per candidate in its own process so each model
gets clean GPU memory and a model that OOMs / can't load fails alone instead of killing the
run. Uses ClipTagger(replace(Config(), clip_model=..., clip_pretrained=...)) so the numbers
reflect exactly what photoflow would produce in production (SigLIP sigmoid vs CLIP cosine).

Metrics (all rank-based / scale-free so SigLIP-sigmoid and CLIP-cosine compare fairly):
  recall@{1,3,5,8}  expected tag in top-k on the 180-image manifest      (higher better)
  mrr               mean reciprocal rank of the expected tag
  auc               mean per-tag present-vs-absent ranking AUC over the precision set
                    (does the model score a tag higher where it's verified present than
                     where it's verified absent? -- the precision side)            (higher better)
  top1_score_mean / expected_score_mean   raw-score summaries for threshold tuning

Usage: python model_bakeoff.py "<model>" "<pretrained>"
"""

from __future__ import annotations

import csv
import json
import sys
import time
from collections import defaultdict
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parents[1] / "src"))

MANIFEST = HERE / "openimages_manifest.csv"
PRECISION = HERE / "openimages_precision.csv"
CACHE = HERE / "cache"


def _auc(pos: list[float], neg: list[float]) -> float:
    """P(score(present) > score(absent)) over all present x absent pairs; ties = 0.5."""
    c = 0.0
    for sp in pos:
        for sn in neg:
            if sp > sn:
                c += 1.0
            elif sp == sn:
                c += 0.5
    return c / (len(pos) * len(neg))


def main(model: str, pretrained: str) -> None:
    result: dict = {"model": model, "pretrained": pretrained, "ok": False, "err": None}
    try:
        from dataclasses import replace

        from PIL import Image

        from photoflow.config import Config
        from photoflow.enrich.tagger import ClipTagger

        cfg = replace(Config(), clip_model=model, clip_pretrained=pretrained)
        t0 = time.perf_counter()
        tagger = ClipTagger(cfg)
        load_s = time.perf_counter() - t0
        params_m = sum(p.numel() for p in tagger.model.parameters()) / 1e6

        with open(MANIFEST, encoding="utf-8") as f:
            man = [(r["image_id"], r["expected_tag"]) for r in csv.DictReader(f)]
        prec_pos: dict[str, list[str]] = defaultdict(list)  # tag -> present image_ids
        prec_neg: dict[str, list[str]] = defaultdict(list)  # tag -> absent  image_ids
        with open(PRECISION, encoding="utf-8") as f:
            for r in csv.DictReader(f):
                (prec_pos if r["present"] == "1" else prec_neg)[r["vocab_tag"]].append(
                    r["image_id"]
                )

        all_ids = {iid for iid, _ in man}
        for d in (prec_pos, prec_neg):
            for ids in d.values():
                all_ids.update(ids)

        # Score every cached image once: image_id -> {tag: score}
        scores_by_img: dict[str, dict[str, float]] = {}
        t0 = time.perf_counter()
        for iid in all_ids:
            p = CACHE / f"{iid}.jpg"
            if p.exists():
                scores_by_img[iid] = dict(tagger.tag(Image.open(p).convert("RGB")))
        score_s = time.perf_counter() - t0

        # recall / MRR on the manifest
        recall = {1: 0, 3: 0, 5: 0, 8: 0}
        ranks: list[int] = []
        top1s: list[float] = []
        exp_scores: list[float] = []
        n = 0
        for iid, expected in man:
            sc = scores_by_img.get(iid)
            if not sc:
                continue
            n += 1
            ordered = sorted(sc.items(), key=lambda x: x[1], reverse=True)
            rank = next(
                (i + 1 for i, (t, _) in enumerate(ordered) if t == expected), len(ordered) + 1
            )
            ranks.append(rank)
            for k in recall:
                if rank <= k:
                    recall[k] += 1
            top1s.append(ordered[0][1])
            exp_scores.append(sc.get(expected, 0.0))

        # per-tag present-vs-absent AUC on the precision set
        per_tag_auc: dict[str, float] = {}
        for tag in set(prec_pos) | set(prec_neg):
            pos = [scores_by_img[i][tag] for i in prec_pos.get(tag, []) if i in scores_by_img]
            neg = [scores_by_img[i][tag] for i in prec_neg.get(tag, []) if i in scores_by_img]
            if pos and neg:
                per_tag_auc[tag] = _auc(pos, neg)

        result.update(
            ok=True,
            device=str(tagger.device),
            is_siglip=bool(tagger.is_siglip),
            params_m=round(params_m, 1),
            n_images=n,
            load_s=round(load_s, 1),
            score_s=round(score_s, 1),
            recall={str(k): round(v / n, 3) for k, v in recall.items()} if n else {},
            mrr=round(sum(1.0 / r for r in ranks) / len(ranks), 3) if ranks else None,
            auc=round(sum(per_tag_auc.values()) / len(per_tag_auc), 3) if per_tag_auc else None,
            n_auc_tags=len(per_tag_auc),
            worst_auc_tags=sorted(per_tag_auc.items(), key=lambda x: x[1])[:5],
            top1_score_mean=round(sum(top1s) / len(top1s), 4) if top1s else None,
            expected_score_mean=round(sum(exp_scores) / len(exp_scores), 4) if exp_scores else None,
        )
    except Exception as e:
        result["err"] = f"{type(e).__name__}: {str(e)[:300]}"
    print(json.dumps(result))


if __name__ == "__main__":
    main(sys.argv[1], sys.argv[2])
