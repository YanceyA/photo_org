"""(Re)build the Open Images calibration manifest.

Downloads the Open Images V7 class descriptions + validation human-verified image-level
labels, maps a subset of Open Images classes to photoflow's FAMILY_VOCAB tags, and writes a
small, representative manifest of (image_id, expected_tag, oi_name) - K images per tag.

The images themselves are NOT stored in the repo: the calibration test downloads each id on
demand from the public CVDF mirror into cache/ (gitignored). Only this text manifest is
committed, so the repo stays asset-free.

Run:  uv run python tests/calibration_data/build_openimages_manifest.py [K] [MAX_CANDIDATES]
"""

from __future__ import annotations

import csv
import sys
from collections import defaultdict
from pathlib import Path

import requests

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))
from photoflow.enrich.tagger import vocab_tags  # noqa: E402

CLASS_URL = "https://storage.googleapis.com/openimages/v7/oidv7-class-descriptions.csv"
LABELS_URL = (
    "https://storage.googleapis.com/openimages/v7/oidv7-val-annotations-human-imagelabels.csv"
)
SPLIT = "validation"
OUT = Path(__file__).with_name("openimages_manifest.csv")

# Open Images DisplayName -> photoflow vocab tag (exact OI names; resolved to MIDs at runtime).
OI_TO_VOCAB = {
    "Cat": "cat",
    "Dog": "dog",
    "Bird": "bird",
    "Horse": "horse",
    "Fish": "fish",
    "Car": "car",
    "Boat": "boat",
    "Bicycle": "bicycle",
    "Flower": "flowers",
    "Christmas tree": "christmas tree",
    "Food": "food",
    "Birthday cake": "birthday cake",
    "Balloon": "balloons",
    "Fireworks": "fireworks",
    "Book": "books",
    "Beach": "beach",
    "Snow": "snow",
    "Kitchen": "kitchen",
    "Desert": "desert",
    "Sunset": "sunset",
    "Forest": "forest",
    "Garden": "garden",
    "Mountain": "mountains",
    "Lake": "lake",
    "Park": "park",
    "Wedding": "wedding",
    "Christmas": "christmas",
    "Baby": "baby",
    "Boy": "child",
    "Girl": "child",
    "Swimming pool": "swimming",
}


def main(k: int = 6, max_candidates: int = 30) -> None:
    valid = set(vocab_tags())
    oi_to_vocab = {oi: tag for oi, tag in OI_TO_VOCAB.items() if tag in valid}

    print(f"fetching class descriptions ... ({CLASS_URL.split('/')[-1]})")
    cdr = requests.get(CLASS_URL, timeout=60)
    cdr.raise_for_status()
    name_to_mid = {}
    for line in cdr.text.splitlines()[1:]:
        if "," in line:
            mid, name = line.split(",", 1)
            name_to_mid[name.strip().strip("'")] = mid.strip()
    mid_to_tag = {name_to_mid[oi]: tag for oi, tag in oi_to_vocab.items() if oi in name_to_mid}
    tag_oi = {name_to_mid[oi]: oi for oi in oi_to_vocab if oi in name_to_mid}
    n_tags = len(set(mid_to_tag.values()))
    print(f"mapped {len(mid_to_tag)} Open Images classes -> {n_tags} vocab tags")

    print(
        f"streaming validation labels (this is large; one-time) ... ({LABELS_URL.split('/')[-1]})"
    )
    cand: dict[str, list[tuple[str, str]]] = defaultdict(list)  # tag -> [(image_id, oi_name)]
    used: set[str] = set()
    with requests.get(LABELS_URL, stream=True, timeout=300) as r:
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
                if float(conf) < 1.0:
                    continue
            except ValueError:
                continue
            if len(cand[tag]) < max_candidates:
                cand[tag].append((image_id, tag_oi[mid]))
                used.add(image_id)
            if all(len(cand[t]) >= max_candidates for t in set(mid_to_tag.values())):
                break

    rows = []
    for tag in sorted(set(mid_to_tag.values())):
        picks = sorted(cand.get(tag, []))[:k]  # deterministic subset
        for image_id, oi_name in picks:
            rows.append(
                {"image_id": image_id, "split": SPLIT, "expected_tag": tag, "oi_name": oi_name}
            )
        print(f"  {tag:16} {len(picks)} images" + ("" if picks else "  (no positives found!)"))

    with open(OUT, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=["image_id", "split", "expected_tag", "oi_name"])
        w.writeheader()
        w.writerows(rows)
    print(f"\nwrote {len(rows)} rows across {len({r['expected_tag'] for r in rows})} tags -> {OUT}")


if __name__ == "__main__":
    k = int(sys.argv[1]) if len(sys.argv) > 1 else 6
    mx = int(sys.argv[2]) if len(sys.argv) > 2 else 30
    main(k, mx)
