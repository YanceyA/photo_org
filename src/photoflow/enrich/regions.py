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

from photoflow.exiftool import KeywordSets

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
    existing,
    tags: Iterable[str],
    people: Iterable[str],
    *,
    prefix: str = "People",
    iptc: bool = True,
    owned_people: Iterable[str] = (),
) -> list[str]:
    """Idempotent read-union-replace argfile lines for keywords + people.

    `existing` is what the file carries right now (a KeywordSets from read_keywords, or a
    plain set of dc:Subject values for callers that only have those). dc:Subject is a pure
    UNION so user keywords and photoflow's provenance folder keywords are never lost.

    The two people-shaped lists are trickier, because other tools write into them too:
      * lr:HierarchicalSubject - everything NOT under `<prefix>|` is foreign (Places|Paris
        from digiKam) and is preserved verbatim; only our `<prefix>|` branch is replaced.
      * Iptc4xmpExt:PersonInImage - names photoflow OWNS (`owned_people`, i.e. every row in
        the persons table) are ours to replace; any other name was written by another tool
        and survives.
    Each list is cleared (`-TAG=`) then rewritten so re-applying yields the same set instead
    of duplicating entries. A list whose resulting set is identical to what's already there
    is left completely untouched, so a tags-only file never gets a stray clear-and-rewrite
    of somebody else's hierarchy.
    """
    if isinstance(existing, KeywordSets):
        ex_subject = set(existing.subject)
        ex_hier = set(existing.hierarchical)
        ex_persons = set(existing.persons)
    else:  # back-compat: a bare iterable of dc:Subject values
        ex_subject, ex_hier, ex_persons = set(existing or ()), set(), set()

    people = sorted(set(people))
    owned = set(owned_people)
    subjects = sorted(ex_subject | set(tags) | set(people))

    lines: list[str] = ["-XMP-dc:Subject="]
    lines += [f"-XMP-dc:Subject={s}" for s in subjects]

    if iptc:
        lines.append("-IPTC:Keywords=")
        lines += [f"-IPTC:Keywords={s}" for s in subjects]

    if prefix:
        new_hier = {h for h in ex_hier if not h.startswith(f"{prefix}|")}
        new_hier |= {f"{prefix}|{p}" for p in people}
    else:
        new_hier = set(ex_hier)
    lines += _replace_list_lines("-XMP-lr:HierarchicalSubject", ex_hier, new_hier)

    new_persons = (ex_persons - owned) | set(people)
    lines += _replace_list_lines("-XMP-iptcExt:PersonInImage", ex_persons, new_persons)
    return lines


def _replace_list_lines(tag: str, before: set[str], after: set[str]) -> list[str]:
    """Clear-then-rewrite lines for one list tag; nothing at all when there's no change to make."""
    if after == before:  # a tags-only file must not touch somebody else's list at all
        return []
    if not after:  # everything in it was ours and is gone -> clear it for real
        return [f"{tag}="]
    return [f"{tag}="] + [f"{tag}={v}" for v in sorted(after)]


def keyword_remove_argfile_lines(
    values: Iterable[str], *, iptc: bool = True, people_prefix: str = "People"
) -> list[str]:
    """exiftool '-=' lines that delete each value from the keyword + people lists.

    apply unions keywords (never drops), so a person name that was renamed/merged lingers in
    dc:Subject / IPTC:Keywords on already-applied files. '-=' removes that exact list value and
    is a NO-OP when it's absent, so this strips only the named stale values and never disturbs
    other keywords. Returns just the tag lines; the caller appends -overwrite_original/target.
    """
    lines: list[str] = []
    for v in values:
        lines.append(f"-XMP-dc:Subject-={v}")
        if iptc:
            lines.append(f"-IPTC:Keywords-={v}")
        lines.append(f"-XMP-iptcExt:PersonInImage-={v}")
        if people_prefix:
            lines.append(f"-XMP-lr:HierarchicalSubject-={people_prefix}|{v}")
    return lines
