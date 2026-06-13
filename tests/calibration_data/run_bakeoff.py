"""Drive the open_clip tagger bake-off.

Runs model_bakeoff.py once per candidate in an isolated process (clean GPU memory; a model
that OOMs / can't load fails alone), then prints a ranked table and saves bakeoff_results.json.
Candidates are strong open_clip zero-shot models curated from open_clip.list_pretrained();
all run fp32 (Pascal's fp16 is crippled). The ~bigG entries (~10 GB fp32) may OOM on an 11 GB
card -- that's a legitimate "too big for this GPU" result, recorded and skipped.

Run:  uv run python tests/calibration_data/run_bakeoff.py
"""

from __future__ import annotations

import csv
import json
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import requests

HERE = Path(__file__).resolve().parent
WORKER = HERE / "model_bakeoff.py"
MANIFEST = HERE / "openimages_manifest.csv"
PRECISION = HERE / "openimages_precision.csv"
CACHE = HERE / "cache"
OUT = HERE / "bakeoff_results.json"
IMG_URL = "https://open-images-dataset.s3.amazonaws.com/{split}/{image_id}.jpg"

# (model, pretrained) -- curated strong zero-shot candidates (all verified present in
# open_clip 3.3.0). fp32. bigG entries may OOM on 11 GB.
CANDIDATES = [
    ("ViT-SO400M-16-SigLIP2-384", "webli"),  # current baseline
    ("ViT-SO400M-16-SigLIP2-512", "webli"),  # SigLIP2 SO400M @512
    ("ViT-gopt-16-SigLIP2-384", "webli"),  # SigLIP2 giant
    ("ViT-L-16-SigLIP2-384", "webli"),  # SigLIP2 Large (smaller/faster)
    ("ViT-SO400M-14-SigLIP-384", "webli"),  # SigLIP v1 SO400M
    ("ViT-H-14", "dfn5b"),  # DFN5B ViT-H
    ("ViT-H-14-378-quickgelu", "dfn5b"),  # DFN5B ViT-H @378
    ("ViT-H-14-worldwide", "metaclip2_worldwide"),  # MetaCLIP2 ViT-H
    ("ViT-H-14-quickgelu", "metaclip_fullcc"),  # MetaCLIP v1 ViT-H
    ("EVA02-L-14-336", "merged2b_s6b_b61k"),  # EVA02-L @336
    ("ViT-H-14", "laion2b_s32b_b79k"),  # LAION ViT-H
    ("ViT-H-14-CLIPA-336", "datacomp1b"),  # CLIPA ViT-H @336
    ("convnext_large_d_320", "laion2b_s29b_b131k_ft_soup"),  # ConvNeXt-L
    ("ViT-bigG-14-worldwide", "metaclip2_worldwide"),  # MetaCLIP2 bigG (may OOM)
    ("ViT-bigG-14", "laion2b_s39b_b160k"),  # LAION bigG (may OOM)
]


def _refs() -> list[tuple[str, str]]:
    """(image_id, split) for every image referenced by the manifest + precision set."""
    refs: dict[str, str] = {}
    with open(MANIFEST, encoding="utf-8") as f:
        for r in csv.DictReader(f):
            refs[r["image_id"]] = r["split"]
    with open(PRECISION, encoding="utf-8") as f:
        for r in csv.DictReader(f):
            refs[r["image_id"]] = r["split"]
    return list(refs.items())


def _fetch(image_id: str, split: str) -> bool:
    p = CACHE / f"{image_id}.jpg"
    if p.exists():
        return True
    try:
        r = requests.get(IMG_URL.format(split=split, image_id=image_id), timeout=30)
        if r.status_code == 200 and r.content[:2] == b"\xff\xd8":
            CACHE.mkdir(exist_ok=True)
            p.write_bytes(r.content)
            return True
    except Exception:
        return False
    return False


def prefetch() -> None:
    refs = _refs()
    missing = [(i, s) for i, s in refs if not (CACHE / f"{i}.jpg").exists()]
    print(f"prefetch: {len(refs)} referenced, {len(missing)} missing -> downloading", flush=True)
    if not missing:
        return
    ok = 0
    with ThreadPoolExecutor(max_workers=16) as ex:
        for got in ex.map(lambda a: _fetch(*a), missing):
            ok += bool(got)
    print(
        f"prefetch: downloaded {ok}/{len(missing)} (failures stay uncached, skipped in scoring)",
        flush=True,
    )


def main() -> None:
    prefetch()
    results = []
    for i, (model, pre) in enumerate(CANDIDATES, 1):
        print(f"\n[{i}/{len(CANDIDATES)}] {model} / {pre}", flush=True)
        proc = None
        try:
            proc = subprocess.run(
                [sys.executable, str(WORKER), model, pre],
                capture_output=True,
                text=True,
                timeout=1800,
            )
            line = proc.stdout.strip().splitlines()[-1] if proc.stdout.strip() else ""
            r = json.loads(line)
        except subprocess.TimeoutExpired:
            r = {"model": model, "pretrained": pre, "ok": False, "err": "timeout(1800s)"}
        except Exception as e:
            tail = (proc.stderr if proc is not None else "") or str(e)
            r = {"model": model, "pretrained": pre, "ok": False, "err": str(tail)[-300:]}
        results.append(r)
        if r.get("ok"):
            rc = r["recall"]
            print(
                f"  R@8={rc.get('8')} R@5={rc.get('5')} R@1={rc.get('1')} mrr={r['mrr']} "
                f"auc={r['auc']} params={r['params_m']}M dev={r['device']} {r['score_s']}s",
                flush=True,
            )
        else:
            print(f"  FAILED: {r['err']}", flush=True)
        OUT.write_text(json.dumps(results, indent=2))  # incremental save

    ok = [r for r in results if r.get("ok")]
    # rank by a blend: recall@8 and precision AUC matter most
    ok.sort(key=lambda r: r["recall"].get("8", 0) + (r.get("auc") or 0), reverse=True)
    print("\n\n==== BAKE-OFF RESULTS (sorted by recall@8 + AUC) ====")
    hdr = (
        f"{'model':30}{'pretrained':16}{'R@1':>6}{'R@3':>6}{'R@5':>6}{'R@8':>6}"
        f"{'MRR':>6}{'AUC':>6}{'parM':>7}{'sec':>6}"
    )
    print(hdr)
    print("-" * len(hdr))
    for r in ok:
        rc = r["recall"]
        print(
            f"{r['model'][:29]:30}{r['pretrained'][:15]:16}"
            f"{rc.get('1', 0):6.2f}{rc.get('3', 0):6.2f}{rc.get('5', 0):6.2f}{rc.get('8', 0):6.2f}"
            f"{(r['mrr'] or 0):6.2f}{(r['auc'] or 0):6.2f}{r['params_m']:7.0f}{r['score_s']:6.0f}"
        )
    for r in results:
        if not r.get("ok"):
            print(f"FAILED  {r['model']}/{r['pretrained']}: {str(r['err'])[:140]}")
    print(f"\nsaved -> {OUT.name}")


if __name__ == "__main__":
    main()
