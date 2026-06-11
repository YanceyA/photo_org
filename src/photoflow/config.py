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
