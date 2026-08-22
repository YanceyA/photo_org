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
# every field whose TOML value is a list but whose dataclass type is frozenset[str]
_FROZENSET_FIELDS = _EXT_FIELDS | {"exclude_dirs"}


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
        {
            ".cr2",
            ".cr3",
            ".crw",
            ".nef",
            ".nrw",
            ".arw",
            ".sr2",
            ".srf",
            ".dng",
            ".orf",
            ".rw2",
            ".raf",
            ".pef",
            ".srw",
            ".x3f",
            ".iiq",
            ".3fr",
            ".eip",
            ".erf",
            ".mrw",
            ".rwl",
            ".mef",
            ".kdc",
            ".dcr",
        }
    )
    video_ext: frozenset[str] = frozenset(
        {".mov", ".mp4", ".m4v", ".avi", ".mts", ".m2ts", ".3gp", ".wmv", ".mpg", ".mpeg"}
    )
    sidecar_ext: frozenset[str] = frozenset({".xmp", ".aae", ".thm"})
    # Directory names never descended into, matched case-insensitively against each path
    # component BELOW the source root (the root itself is always scanned, even if its own
    # name is listed). Caches, proxies, previews and recycle bins - ingesting them fills the
    # library with derivatives of files it already has.
    # An entry containing '*' or '?' is a glob (fnmatch), matched case-insensitively against
    # the whole directory name; everything else is an exact name. Lightroom preview bundles are
    # named "<Catalog Name> Previews.lrdata", so only a glob catches them.
    exclude_dirs: frozenset[str] = frozenset(
        {
            "CaptureOne",
            "Cache",
            "Proxies",
            "Thumbnails",
            "Trash",
            "$RECYCLE.BIN",
            "System Volume Information",
            "@eaDir",
            ".thumbnails",
            ".Trash",
            ".Trashes",
            "*.lrdata",  # "<Catalog> Previews.lrdata", "<Catalog> Smart Previews.lrdata", ...
            "Lightroom Settings",
            "__MACOSX",
        }
    )
    # Files smaller than this are skipped at walk time. 0 = off (default). 20000 is a
    # sensible value for sources littered with camera/app thumbnails. Sidecars are exempt -
    # they're tiny by nature, and dropping them would strand the media they describe.
    min_size_bytes: int = 0
    # Sidecars (.thm thumbnails, .aae edit lists, foreign .xmp) are metadata about a
    # photo, not a photo. Default False: they stay in the manifest for dedupe/audit but
    # are never copied. Set true to restore the pre-2026-08 behaviour.
    copy_sidecars: bool = False

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
    # lower (e.g. 3) to recover people who appear in only a few photos (Layer 2); higher to
    # demand denser evidence. Tune in photoflow.toml; calibrate against your named faces.
    enrich_min_samples: int = 0  # HDBSCAN min_samples; 0 => None (= min_cluster_size)
    # >0 merges clusters closer than this (cosine-equivalent) distance, fusing the per-burst
    # fragments of one person into a single cluster (Layer 1). Default 0 = today's behavior;
    # raise gradually (e.g. 0.1-0.3) and check it never merges two distinct named people.
    enrich_cluster_selection_epsilon: float = 0.0
    # HDBSCAN cluster selection: "eom" (default) prefers fewer/larger stable clusters;
    # "leaf" picks the finest sub-clusters, which breaks up a mega-cluster that lumped several
    # people together (at the cost of more, smaller groups to name). Usually paired with a
    # lower enrich_min_cluster_size for a final straggler-splitting pass.
    enrich_cluster_selection_method: str = "eom"
    enrich_cluster_prob_floor: float = 0.5  # member prob below this = edge case for review
    enrich_assign_threshold: float = 0.5  # cosine sim to auto-suggest a new face to a named person
    enrich_auto_assign_threshold: float = 0.6  # higher bar for `enrich assign` to COMMIT a label
    face_crop_pad: float = 0.3  # padding around bbox when writing the review thumbnail
    # SigLIP2 sigmoid scores are low and vary ~100x BY TAG: on the Open Images calibration set
    # prominent subjects score ~0.10 (cat, cake) while correct-but-low-scale tags score
    # ~0.002-0.01 (car, flowers, snow, forest, park) even when ranked #1 for the photo. scan.py
    # applies a tag iff its absolute score >= tag_score_review, so the review floor MUST be low
    # or those correct tags are silently dropped (at the old 0.008 floor only ~0.3 tags/photo
    # survived, recall ~0.34). These values were fit on the calibration set (see
    # tests/calibration_data/compare_selection.py): the low review floor ~doubles recall, the
    # review band + blacklist do the precision filtering, and accept auto-takes only the
    # high-confidence high-scale tags. A per-image top-k / relative rule was measured and did
    # NOT beat a simple low global floor, so the global cutoff is kept. Retune via the
    # calibration tools (tests/calibration_data/) if you change clip_model or FAMILY_VOCAB.
    tag_score_accept: float = 0.02  # score >= => auto-accept (high-confidence; ~0.81 precision)
    tag_score_review: float = 0.0015  # [review, accept) => human-review band; below => dropped
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
    for k in _FROZENSET_FIELDS & data.keys():
        data[k] = frozenset(data[k])
    return Config(**data)
