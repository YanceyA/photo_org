"""Config dataclass + photoflow.toml loader (tomllib)."""

from __future__ import annotations

import sys
import tomllib
from dataclasses import dataclass, fields
from datetime import datetime
from pathlib import Path

# Always derived from the clock, never configurable (HANDOFF §4).
MAX_YEAR = datetime.now().year + 1

_EXT_FIELDS = frozenset({"image_ext", "raw_ext", "video_ext", "sidecar_ext"})


@dataclass(frozen=True)
class Config:
    near_dupe_threshold: int = 5  # max pHash hamming distance to flag for review
    burst_window_s: int = 10  # frames within this window + same camera = burst
    min_year: int = 1990
    slug_max: int = 40
    exiftool_batch: int = 200
    image_ext: frozenset[str] = frozenset(
        {".jpg", ".jpeg", ".png", ".heic", ".heif", ".tif", ".tiff", ".bmp", ".gif", ".webp"}
    )
    raw_ext: frozenset[str] = frozenset(
        {".cr2", ".cr3", ".nef", ".arw", ".dng", ".orf", ".rw2", ".raf", ".pef", ".srw", ".x3f"}
    )
    video_ext: frozenset[str] = frozenset(
        {".mov", ".mp4", ".m4v", ".avi", ".mts", ".m2ts", ".3gp", ".wmv", ".mpg", ".mpeg"}
    )
    sidecar_ext: frozenset[str] = frozenset({".xmp", ".aae", ".thm"})

    # --- enrich subsystem (all optional; defaults give a working CPU-faces / RAM-tags run) ---
    # auto = prefer RAM++ but fall back to CLIP/SigLIP if it can't load. RAM++ needs an old
    # transformers (~4.25) that won't run on Python 3.14, so 'auto' uses SigLIP here. Set
    # 'clip' to skip RAM entirely, or 'ram' to require it.
    enrich_tagger: str = "auto"  # ram | clip | auto
    enrich_device: str = "auto"  # torch device for RAM/CLIP: auto | cuda | cpu
    face_device: str = "auto"  # onnxruntime providers for InsightFace: auto | cuda | cpu
    # auto => CPU on this rig: Pascal (1080 Ti) + Py3.14 + onnxruntime-gpu>=1.26 crashes (#27588)
    enrich_batch: int = 16
    enrich_face_min_score: float = 0.55  # InsightFace det_score floor
    enrich_min_cluster_size: int = 5  # HDBSCAN: min faces to call a cluster a person
    enrich_min_samples: int = 0  # HDBSCAN min_samples; 0 => None (= min_cluster_size)
    enrich_cluster_prob_floor: float = 0.5  # member prob below this = edge case for review
    enrich_assign_threshold: float = 0.5  # cosine sim to auto-suggest a new face to a named person
    face_crop_pad: float = 0.3  # padding around bbox when writing the review thumbnail
    # SigLIP2 sigmoid probs are low-scaled (logit_bias ~ -16) AND tag-dependent: on the Open
    # Images calibration set prominent subjects score ~0.10 (cat, cake) while scenes score
    # ~0.001-0.03 (beach, forest) even when correctly top-ranked. So thresholds are low and
    # inclusive - the review step + blacklist do the real filtering. Retune via the calibration
    # report (tests/test_enrich_calibration.py) if you change clip_model.
    tag_score_accept: float = 0.05  # score >= => auto-accept
    tag_score_review: float = 0.008  # [review, accept) => edge case; below => dropped
    clip_prompt: str = "a photo of a {}."  # single SigLIP prompt; ensembling dilutes the score
    ram_checkpoint: str = ""  # path to ram_plus_swin_large_14m.pth ("" => workdir/models/...)
    ram_image_size: int = 384
    clip_model: str = "ViT-SO400M-16-SigLIP2-384"  # SigLIP2 SO400M@384 => calibrated sigmoid scores
    clip_pretrained: str = "webli"
    write_mwg_regions: bool = True  # write MWG face-region rectangles (digiKam/Lightroom/Immich)
    write_iptc_keywords: bool = True  # mirror keywords to IPTC + lr:HierarchicalSubject (Immich)
    people_keyword_prefix: str = "People"  # hierarchical prefix: People|<name>; "" disables


def load_config(workdir: Path) -> Config:
    """Load workdir/photoflow.toml if present; unknown keys are fatal."""
    toml_path = workdir / "photoflow.toml"
    if not toml_path.exists():
        return Config()
    with open(toml_path, "rb") as f:
        data = tomllib.load(f)
    valid = {f.name for f in fields(Config)}
    for k in data:
        if k not in valid:
            sys.exit(f"photoflow.toml: unknown key '{k}' (valid: {', '.join(sorted(valid))})")
    for k in _EXT_FIELDS & data.keys():
        data[k] = frozenset(data[k])
    return Config(**data)
