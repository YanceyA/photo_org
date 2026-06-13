"""Pure helpers for MWG face regions + keyword/subject union -> exiftool argfile lines.

No I/O. Mirrors xmp.py's "build the exact exiftool argument lines, let the caller run
the process" split, so this is unit-testable without exiftool.

MWG (Metadata Working Group) face regions live in the XMP-mwg-rs namespace. Multiple
regions are written by REPEATING the flattened per-region tags once per face: exiftool
builds parallel rdf:Bag lists and pairs them positionally, so every region MUST emit the
SAME complete set of Area sub-tags or the bags desync and faces get the wrong box.
RegionAreaX/Y is the CENTER of the box (not top-left), normalized 0..1.
"""

from __future__ import annotations

from collections.abc import Iterable

Bbox = tuple[float, float, float, float]  # (x1, y1, x2, y2) pixel top-left + bottom-right


def normalized_region(bbox: Bbox, img_w: int, img_h: int) -> tuple[float, float, float, float]:
    """Pixel bbox -> MWG (center_x, center_y, width, height) normalized to [0, 1].

    Corners are clamped to the image before normalizing so a detector box that pokes past
    an edge can never produce an out-of-range region.
    """
    x1, y1, x2, y2 = bbox
    x1, x2 = sorted((x1, x2))
    y1, y2 = sorted((y1, y2))
    x1 = min(max(x1, 0.0), img_w)
    x2 = min(max(x2, 0.0), img_w)
    y1 = min(max(y1, 0.0), img_h)
    y2 = min(max(y2, 0.0), img_h)
    cx = (x1 + x2) / 2 / img_w
    cy = (y1 + y2) / 2 / img_h
    w = (x2 - x1) / img_w
    h = (y2 - y1) / img_h
    return cx, cy, w, h


def _fmt(v: float) -> str:
    return f"{v:.6f}"


def region_argfile_lines(img_w: int, img_h: int, regions: Iterable[tuple[str, Bbox]]) -> list[str]:
    """exiftool argfile lines writing N named MWG face regions. Empty regions -> no lines.

    Writing the region list REPLACES it (struct overwrite), so re-running is idempotent.
    """
    regions = list(regions)
    if not regions:
        return []
    lines = [
        f"-XMP-mwg-rs:RegionAppliedToDimensionsW={img_w}",
        f"-XMP-mwg-rs:RegionAppliedToDimensionsH={img_h}",
        "-XMP-mwg-rs:RegionAppliedToDimensionsUnit=pixel",
    ]
    for name, bbox in regions:
        cx, cy, w, h = normalized_region(bbox, img_w, img_h)
        lines += [
            f"-XMP-mwg-rs:RegionName={name}",
            "-XMP-mwg-rs:RegionType=Face",
            f"-XMP-mwg-rs:RegionAreaX={_fmt(cx)}",
            f"-XMP-mwg-rs:RegionAreaY={_fmt(cy)}",
            f"-XMP-mwg-rs:RegionAreaW={_fmt(w)}",
            f"-XMP-mwg-rs:RegionAreaH={_fmt(h)}",
            "-XMP-mwg-rs:RegionAreaUnit=normalized",
        ]
    return lines


def keyword_argfile_lines(
    existing: Iterable[str],
    tags: Iterable[str],
    people: Iterable[str],
    *,
    prefix: str = "People",
    iptc: bool = True,
) -> list[str]:
    """Idempotent read-union-replace argfile lines for keywords + people.

    `existing` is the file's CURRENT dc:subject (read just before applying) so user-added
    keywords and photoflow's provenance folder keywords are preserved: the written set is
    always a superset of what was there. Each list tag is cleared (`-TAG=`) then re-written
    so re-applying the same data yields the same set instead of duplicating entries.

    People are additionally written as Iptc4xmpExt:PersonInImage and (when `prefix`) as a
    `<prefix>|<name>` lr:HierarchicalSubject, which is what Immich/digiKam key on.
    """
    people = sorted(set(people))
    subjects = sorted(set(existing) | set(tags) | set(people))

    lines: list[str] = ["-XMP-dc:Subject="]
    lines += [f"-XMP-dc:Subject={s}" for s in subjects]

    if iptc:
        lines.append("-IPTC:Keywords=")
        lines += [f"-IPTC:Keywords={s}" for s in subjects]

    if people:
        lines.append("-XMP-iptcExt:PersonInImage=")
        lines += [f"-XMP-iptcExt:PersonInImage={p}" for p in people]
        if prefix:
            lines.append("-XMP-lr:HierarchicalSubject=")
            lines += [f"-XMP-lr:HierarchicalSubject={prefix}|{p}" for p in people]
    return lines
