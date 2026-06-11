"""File classification and domain vocabulary."""

from __future__ import annotations

from photoflow.config import IMAGE_EXT, RAW_EXT, SIDECAR_EXT, VIDEO_EXT

ROLES = frozenset({"keep", "exact_dupe", "raw_jpeg_pair", "live_pair", "burst", "review"})
DURABLE_STATUSES = frozenset({"copied", "error", "skipped_manual"})  # HANDOFF §2.4


def classify(ext: str) -> str:
    if ext in IMAGE_EXT:
        return "image"
    if ext in RAW_EXT:
        return "raw"
    if ext in VIDEO_EXT:
        return "video"
    if ext in SIDECAR_EXT:
        return "sidecar"
    return "other"
