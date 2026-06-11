"""Central constants (TOML loader arrives in Phase 4)."""

from __future__ import annotations

from datetime import datetime

IMAGE_EXT = {".jpg", ".jpeg", ".png", ".heic", ".heif", ".tif", ".tiff", ".bmp", ".gif", ".webp"}
RAW_EXT = {".cr2", ".cr3", ".nef", ".arw", ".dng", ".orf", ".rw2", ".raf", ".pef", ".srw", ".x3f"}
VIDEO_EXT = {".mov", ".mp4", ".m4v", ".avi", ".mts", ".m2ts", ".3gp", ".wmv", ".mpg", ".mpeg"}
SIDECAR_EXT = {".xmp", ".aae", ".thm"}

NEAR_DUPE_THRESHOLD = 5  # max pHash hamming distance to flag for review
BURST_WINDOW_S = 10  # frames within this window + same camera = burst
MIN_YEAR, MAX_YEAR = 1990, datetime.now().year + 1
SLUG_MAX = 40
EXIFTOOL_BATCH = 200
