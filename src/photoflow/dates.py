"""Date resolution cascade: EXIF -> filename -> folder year -> mtime."""

from __future__ import annotations

import re
from datetime import datetime
from pathlib import Path

from photoflow.config import MAX_YEAR

MIN_YEAR_DEFAULT = 1990

EXIF_DATE_RE = re.compile(r"(\d{4}):(\d{2}):(\d{2})[ T](\d{2}):(\d{2}):(\d{2})")
FNAME_FULL_RE = re.compile(r"((?:19|20)\d{2})(\d{2})(\d{2})[_\-. ]?(\d{2})(\d{2})(\d{2})")
FNAME_WA_RE = re.compile(r"IMG-((?:19|20)\d{2})(\d{2})(\d{2})-WA", re.I)
FNAME_DATE_RE = re.compile(r"((?:19|20)\d{2})[-_.]([01]?\d)[-_.]([0-3]?\d)")
FOLDER_YEAR_RE = re.compile(r"\b((?:19|20)\d{2})\b")


def _valid(y, m, d, hh=0, mm=0, ss=0, *, min_year: int = MIN_YEAR_DEFAULT) -> datetime | None:
    try:
        dt = datetime(y, m, d, hh, mm, ss)
    except ValueError:
        return None
    return dt if min_year <= y <= MAX_YEAR else None


def parse_exif_date(raw: str | None, min_year: int = MIN_YEAR_DEFAULT) -> datetime | None:
    if not raw:
        return None
    m = EXIF_DATE_RE.search(str(raw))
    return _valid(*(int(g) for g in m.groups()), min_year=min_year) if m else None


def date_from_filename(name: str, min_year: int = MIN_YEAR_DEFAULT) -> datetime | None:
    m = FNAME_FULL_RE.search(name)
    if m:
        dt = _valid(*(int(g) for g in m.groups()), min_year=min_year)
        if dt:
            return dt
    m = FNAME_WA_RE.search(name)
    if m:
        dt = _valid(int(m[1]), int(m[2]), int(m[3]), min_year=min_year)
        if dt:
            return dt
    m = FNAME_DATE_RE.search(name)
    if m:
        return _valid(int(m[1]), int(m[2]), int(m[3]), min_year=min_year)
    return None


def year_from_folder(rel_path: str, min_year: int = MIN_YEAR_DEFAULT) -> int | None:
    for part in reversed(Path(rel_path).parts[:-1]):
        m = FOLDER_YEAR_RE.search(part)
        if m and min_year <= int(m[1]) <= MAX_YEAR:
            return int(m[1])
    return None


def resolve_date(row, min_year: int = MIN_YEAR_DEFAULT) -> tuple[str | None, str, str]:
    """Return (iso_date_or_None, source, confidence)."""
    dt = parse_exif_date(row["exif_date"], min_year)
    if dt:
        return dt.isoformat(), "exif", "high"
    dt = date_from_filename(Path(row["source_path"]).name, min_year)
    if dt:
        return dt.isoformat(), "filename", "medium"
    year = year_from_folder(row["rel_path"] or "", min_year)
    if year:
        return datetime(year, 1, 1).isoformat(), "folder", "low"
    if row["mtime"]:
        dt = datetime.fromtimestamp(row["mtime"])
        if min_year <= dt.year <= MAX_YEAR:
            return dt.isoformat(), "mtime", "low"
    return None, "none", "none"
