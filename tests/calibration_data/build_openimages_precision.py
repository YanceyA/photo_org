"""Build a balanced precision set for the tagger bake-off.

The recall manifest (openimages_manifest.csv) only has POSITIVES, so recall@8 can't tell a
precise tagger from a noisy one. This builds openimages_precision.csv: for each mapped vocab
tag, the manifest's verified-PRESENT images (present=1) PLUS up to K_NEG verified-ABSENT
images (present=0) pulled from the full Open Images V7 validation human-label stream.

The bake-off then computes, per tag, a present-vs-absent ranking AUC: does the model score a
tag higher on images that contain it than on images verified NOT to? AUC is scale-free, so it
compares SigLIP-sigmoid and CLIP-cosine scores fairly. Only this text file is committed;
absent images download on demand into cache/ (gitignored), same as the manifest.

Run:  uv run python tests/calibration_data/build_openimages_precision.py [K_NEG]
"""

from __future__ import annotations

import csv
import sys
from collections import defaultdict
from pathlib import Path

import requests

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE.parents[1] / "src"))
from build_openimages_manifest import CLASS_URL, LABELS_URL, OI_TO_VOCAB, SPLIT  # noqa: E402

from photoflow.enrich.tagger import vocab_tags  # noqa: E402

MANIFEST = HERE / "openimages_manifest.csv"
OUT = HERE / "openimages_precision.csv"


def main(k_neg: int = 12) -> None:
    # Present rows come straight from the (stable) recall manifest; their images are cached.
    with open(MANIFEST, encoding="utf-8") as f:
        present = [(r["image_id"], r["expected_tag"]) for r in csv.DictReader(f)]
    present_ids = {iid for iid, _ in present}
    print(f"{len(present)} present rows from manifest ({len(present_ids)} images)")

    print(f"fetching class descriptions ... ({CLASS_URL.split('/')[-1]})")
    cdr = requests.get(CLASS_URL, timeout=60)
    cdr.raise_for_status()
    name_to_mid = {}
    for line in cdr.text.splitlines()[1:]:
        if "," in line:
            mid, name = line.split(",", 1)
            name_to_mid[name.strip().strip("'")] = mid.strip()
    valid = set(vocab_tags())  # only collect labels for tags still in FAMILY_VOCAB
    mid_to_tag = {
        name_to_mid[oi]: tag
        for oi, tag in OI_TO_VOCAB.items()
        if oi in name_to_mid and tag in valid
    }
    tags = sorted(set(mid_to_tag.values()))
    print(f"{len(mid_to_tag)} OI classes -> {len(tags)} vocab tags; collecting {k_neg} absent/tag")

    print(f"streaming validation labels (one-time, large) ... ({LABELS_URL.split('/')[-1]})")
    absent: dict[str, list[str]] = defaultdict(list)  # tag -> [image_id]
    used: set[str] = set(present_ids)
    with requests.get(LABELS_URL, stream=True, timeout=600) as r:
        r.raise_for_status()
        it = r.iter_lines(decode_unicode=True)
        next(it, None)  # header
        for line in it:
            if not line:
                continue
            parts = line.split(",")
            if len(parts) < 4:
                continue
            image_id, _src, mid, conf = parts[0], parts[1], parts[2], parts[3]
            tag = mid_to_tag.get(mid)
            if tag is None or image_id in used:
                continue
            try:
                if float(conf) != 0.0:  # want verified-ABSENT only
                    continue
            except ValueError:
                continue
            if len(absent[tag]) < k_neg:
                absent[tag].append(image_id)
                used.add(image_id)
            if all(len(absent[t]) >= k_neg for t in tags):
                break

    rows = [
        {"image_id": iid, "split": SPLIT, "vocab_tag": tag, "present": 1} for iid, tag in present
    ]
    for tag in tags:
        for iid in sorted(absent.get(tag, [])):
            rows.append({"image_id": iid, "split": SPLIT, "vocab_tag": tag, "present": 0})

    with open(OUT, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=["image_id", "split", "vocab_tag", "present"])
        w.writeheader()
        w.writerows(rows)

    n_pos = sum(1 for r in rows if r["present"] == 1)
    print("\nper-tag absent counts:")
    for tag in tags:
        n = len(absent.get(tag, []))
        print(f"  {tag:16} {n:2}" + ("  (none found!)" if n == 0 else ""))
    print(
        f"\nwrote {len(rows)} rows ({n_pos} present, {len(rows) - n_pos} absent) "
        f"over {len({r['image_id'] for r in rows})} images -> {OUT.name}"
    )


if __name__ == "__main__":
    main(int(sys.argv[1]) if len(sys.argv) > 1 else 12)
