"""Destination naming: slugify source stems and build library paths."""

from __future__ import annotations

import re
from datetime import datetime
from pathlib import Path


def slugify(stem: str, slug_max: int = 40) -> str:
    s = re.sub(r"[^A-Za-z0-9]+", "-", stem).strip("-")
    return s[:slug_max] or "img"


def dest_for(row, out_root: Path, slug_max: int = 40) -> Path:
    h8 = row["content_hash"][:8]
    slug = slugify(Path(row["source_path"]).stem, slug_max)
    ext = row["ext"].lower()
    if row["date_taken"]:
        dt = datetime.fromisoformat(row["date_taken"])
        folder = out_root / f"{dt.year}" / f"{dt.month:02d}"
        if row["date_source"] in ("exif", "filename") and (dt.hour, dt.minute, dt.second) != (
            0,
            0,
            0,
        ):
            name = f"{dt:%Y%m%d_%H%M%S}_{slug}_{h8}{ext}"
        else:
            name = f"{dt:%Y%m%d}_{slug}_{h8}{ext}"
    else:
        folder = out_root / "unknown-date"
        name = f"{slug}_{h8}{ext}"
    return folder / name
