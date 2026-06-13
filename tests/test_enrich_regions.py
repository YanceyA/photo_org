"""Pure geometry + exiftool argfile builders for MWG face regions and keyword union."""

import pytest

from photoflow.enrich.regions import (
    keyword_argfile_lines,
    normalized_region,
    region_argfile_lines,
)


def test_normalized_region_center_and_size():
    # bbox (x1,y1,x2,y2) = top-left + bottom-right, pixels, in a 1000x1000 image.
    cx, cy, w, h = normalized_region((100, 100, 300, 400), 1000, 1000)
    # MWG RegionAreaX/Y is the CENTER of the box, normalized.
    assert cx == pytest.approx(0.2)  # (100+300)/2 / 1000
    assert cy == pytest.approx(0.25)  # (100+400)/2 / 1000
    assert w == pytest.approx(0.2)  # (300-100)/1000
    assert h == pytest.approx(0.3)  # (400-100)/1000


def test_normalized_region_clamps_out_of_bounds():
    # A detector can return a box poking past the image edge; clamp before normalizing.
    cx, cy, w, h = normalized_region((-10, -10, 100, 100), 1000, 1000)
    assert cx == pytest.approx(0.05) and cy == pytest.approx(0.05)
    assert w == pytest.approx(0.1) and h == pytest.approx(0.1)
    for v in (cx, cy, w, h):
        assert 0.0 <= v <= 1.0


def test_region_argfile_lines_two_faces_positional_pairing():
    lines = region_argfile_lines(
        4032, 3024, [("Mum", (100, 100, 300, 400)), ("Dad", (500, 200, 700, 500))]
    )
    # AppliedToDimensions scalars written exactly once.
    assert lines.count("-XMP-mwg-rs:RegionAppliedToDimensionsW=4032") == 1
    assert lines.count("-XMP-mwg-rs:RegionAppliedToDimensionsH=3024") == 1
    assert lines.count("-XMP-mwg-rs:RegionAppliedToDimensionsUnit=pixel") == 1
    # One RegionName per face, and every face emits the COMPLETE Area sub-tag set so the
    # parallel exiftool bags stay positionally aligned (the one fragility of this form).
    assert lines.count("-XMP-mwg-rs:RegionName=Mum") == 1
    assert lines.count("-XMP-mwg-rs:RegionName=Dad") == 1
    assert lines.count("-XMP-mwg-rs:RegionType=Face") == 2
    for sub in ("RegionAreaX", "RegionAreaY", "RegionAreaW", "RegionAreaH"):
        assert sum(f"-XMP-mwg-rs:{sub}=" in ln for ln in lines) == 2
    assert lines.count("-XMP-mwg-rs:RegionAreaUnit=normalized") == 2


def test_region_argfile_lines_empty_is_empty():
    assert region_argfile_lines(800, 600, []) == []


def test_keyword_argfile_lines_union_and_clear():
    lines = keyword_argfile_lines(existing={"Holiday 2015"}, tags={"beach", "dog"}, people={"Mum"})
    # dc:Subject is cleared first (idempotent replace), then the sorted union is written.
    assert lines[0] == "-XMP-dc:Subject="
    subjects = [
        ln.split("=", 1)[1]
        for ln in lines
        if ln.startswith("-XMP-dc:Subject=") and ln != "-XMP-dc:Subject="
    ]
    assert subjects == sorted({"Holiday 2015", "beach", "dog", "Mum"})
    # people get PersonInImage + a People|<name> hierarchical entry; tags do not.
    assert "-XMP-iptcExt:PersonInImage=Mum" in lines
    assert "-XMP-lr:HierarchicalSubject=People|Mum" in lines
    assert not any("PersonInImage=beach" in ln for ln in lines)


def test_keyword_argfile_lines_idempotent_union():
    # Re-applying when the file already carries our tags/people yields the same subject set.
    first = keyword_argfile_lines(existing=set(), tags={"beach"}, people={"Mum"})
    applied = {
        ln.split("=", 1)[1]
        for ln in first
        if ln.startswith("-XMP-dc:Subject=") and ln != "-XMP-dc:Subject="
    }
    second = keyword_argfile_lines(existing=applied, tags={"beach"}, people={"Mum"})
    second_subjects = {
        ln.split("=", 1)[1]
        for ln in second
        if ln.startswith("-XMP-dc:Subject=") and ln != "-XMP-dc:Subject="
    }
    assert second_subjects == applied == {"beach", "Mum"}


def test_keyword_argfile_lines_iptc_toggle():
    no_iptc = keyword_argfile_lines(existing=set(), tags={"beach"}, people=set(), iptc=False)
    assert not any(ln.startswith("-IPTC:Keywords") for ln in no_iptc)
    with_iptc = keyword_argfile_lines(existing=set(), tags={"beach"}, people=set(), iptc=True)
    assert "-IPTC:Keywords=beach" in with_iptc
