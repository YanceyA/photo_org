"""File classification and domain vocabulary."""

from __future__ import annotations

from photoflow.config import Config

ROLES = frozenset({"keep", "exact_dupe", "raw_jpeg_pair", "live_pair", "burst", "review"})
DURABLE_STATUSES = frozenset({"copied", "error", "skipped_manual"})  # HANDOFF §2.4


def classify(ext: str, cfg: Config) -> str:
    if ext in cfg.image_ext:
        return "image"
    if ext in cfg.raw_ext:
        return "raw"
    if ext in cfg.video_ext:
        return "video"
    if ext in cfg.sidecar_ext:
        return "sidecar"
    return "other"
