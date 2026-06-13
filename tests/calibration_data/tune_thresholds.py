"""Full calibration report for one open_clip tagger model: per-tag recall + precision, a
global precision/recall sweep, and suggested tag_score_accept / tag_score_review.

Different models live on different score scales (SigLIP sigmoid ~0.0-0.1, plain CLIP cosine
~0.1-0.3), and different vocabs separate better/worse, so re-run this whenever clip_model or
FAMILY_VOCAB changes. It scores the manifest (recall) + precision set (present/absent) once.

  per-tag    recall@8 on the manifest, present-vs-absent AUC, and mean present/absent score
             (sorted worst-AUC first -- these are the vocab tags the model can't separate)
  global     precision/recall of 'present-tag score >= t' swept over thresholds
  suggested  accept = lowest t whose precision stays >= --accept-precision over a window
             review = lowest t keeping >= --review-recall of present tags   (clamped <= accept)

  uv run python tests/calibration_data/tune_thresholds.py "<model>" "<pretrained>"
"""

from __future__ import annotations

import csv
import sys
from collections import defaultdict
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parents[1] / "src"))

MANIFEST = HERE / "openimages_manifest.csv"
PRECISION = HERE / "openimages_precision.csv"
CACHE = HERE / "cache"


def _auc(pos: list[float], neg: list[float]) -> float:
    c = sum((1.0 if sp > sn else 0.5 if sp == sn else 0.0) for sp in pos for sn in neg)
    return c / (len(pos) * len(neg))


def _mean(xs: list[float]) -> float:
    return sum(xs) / len(xs) if xs else 0.0


def main(model: str, pretrained: str) -> None:
    from dataclasses import replace

    from PIL import Image

    from photoflow.config import Config
    from photoflow.enrich.tagger import ClipTagger

    cfg = replace(Config(), clip_model=model, clip_pretrained=pretrained)
    tagger = ClipTagger(cfg)
    print(f"model={model}/{pretrained}  is_siglip={tagger.is_siglip}  device={tagger.device}")

    with open(MANIFEST, encoding="utf-8") as f:
        man = [(r["image_id"], r["expected_tag"]) for r in csv.DictReader(f)]
    prec_pos: dict[str, list[str]] = defaultdict(list)
    prec_neg: dict[str, list[str]] = defaultdict(list)
    with open(PRECISION, encoding="utf-8") as f:
        for r in csv.DictReader(f):
            (prec_pos if r["present"] == "1" else prec_neg)[r["vocab_tag"]].append(r["image_id"])

    all_ids = {iid for iid, _ in man} | {
        i for d in (prec_pos, prec_neg) for v in d.values() for i in v
    }
    scored: dict[str, dict[str, float]] = {}
    for iid in all_ids:
        p = CACHE / f"{iid}.jpg"
        if p.exists():
            scored[iid] = dict(tagger.tag(Image.open(p).convert("RGB")))

    # per-tag recall@8 (manifest)
    rec_hits: dict[str, list[int]] = defaultdict(list)
    for iid, exp in man:
        sc = scored.get(iid)
        if sc:
            top8 = [t for t, _ in sorted(sc.items(), key=lambda x: x[1], reverse=True)[:8]]
            rec_hits[exp].append(int(exp in top8))

    # per-tag AUC + mean present/absent score (precision set)
    print(f"\n{'tag':16}{'rec@8':>7}{'AUC':>7}{'mPres':>8}{'mAbs':>8}")
    print("-" * 46)
    rows = []
    for tag in sorted(set(prec_pos) | set(prec_neg)):
        pos = [scored[i][tag] for i in prec_pos.get(tag, []) if i in scored and tag in scored[i]]
        neg = [scored[i][tag] for i in prec_neg.get(tag, []) if i in scored and tag in scored[i]]
        rec = _mean([float(h) for h in rec_hits.get(tag, [])])
        auc = _auc(pos, neg) if pos and neg else float("nan")
        rows.append((tag, rec, auc, _mean(pos), _mean(neg)))
    for tag, rec, auc, mp, ma in sorted(rows, key=lambda r: r[2] if r[2] == r[2] else 9):
        print(f"{tag:16}{rec:7.2f}{auc:7.2f}{mp:8.4f}{ma:8.4f}")

    # global PR sweep over pooled (score, present)
    pairs = sorted(
        (
            (scored[i][tag], pr)
            for tag in set(prec_pos) | set(prec_neg)
            for pr, ids in ((1, prec_pos.get(tag, [])), (0, prec_neg.get(tag, [])))
            for i in ids
            if i in scored and tag in scored[i]
        ),
        reverse=True,
    )
    n_pos = sum(pr for _, pr in pairs)
    cand = sorted({round(s, 4) for s, _ in pairs}, reverse=True)
    sweep = []
    for t in cand[:: max(1, len(cand) // 40)]:
        tp = sum(1 for s, pr in pairs if s >= t and pr == 1)
        fp = sum(1 for s, pr in pairs if s >= t and pr == 0)
        prec = tp / (tp + fp) if (tp + fp) else 1.0
        rec = tp / n_pos if n_pos else 0.0
        sweep.append((t, prec, rec, 2 * prec * rec / (prec + rec) if (prec + rec) else 0.0))
    print(f"\n{'thresh':>9}{'prec':>7}{'recall':>8}{'F1':>7}")
    for t, prec, rec, f1 in sweep:
        print(f"{t:9.4f}{prec:7.2f}{rec:8.2f}{f1:7.2f}")

    best = max(sweep, key=lambda r: r[3])
    print(
        f"\n{len(pairs)} pairs ({n_pos} present, {len(pairs) - n_pos} absent).  "
        f"F1-opt thresh {best[0]:.4f}: P={best[1]:.2f} R={best[2]:.2f} F1={best[3]:.2f}"
    )


if __name__ == "__main__":
    main(sys.argv[1], sys.argv[2])
