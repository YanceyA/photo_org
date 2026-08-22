"""Pure geometry + exiftool argfile builders for MWG face regions and keyword union."""

import pytest

from photoflow.enrich.regions import (
    keyword_argfile_lines,
    keyword_remove_argfile_lines,
    normalized_region,
    region_argfile_lines,
)
from photoflow.exiftool import KeywordSets, _tag_set


def test_normalized_region_center_and_size():
    # bbox (x1,y1,x2,y2) = top-left + bottom-right, pixels, in a 1000x1000 image.
    cx, cy, w, h = normalized_region((100, 100, 300, 400), 1000, 1000)
    # MWG RegionAreaX/Y is the CENTER of the box, normalized.
    assert cx == pytest.approx(0.2)  # (100+300)/2 / 1000
    assert cy == pytest.approx(0.25)  # (100+400)/2 / 1000
    assert w == pytest.approx(0.2)  # (300-100)/1000
    assert h == pytest.approx(0.3)  # (400-100)/1000


def test_keyword_remove_argfile_lines_strips_value_from_every_list():
    # After merging a misspelled person, the stale name lingers as a plain keyword (apply only
    # unions, never drops). exiftool '-=' deletes that exact value from each list and is a no-op
    # when absent, so it strips only the named values and never disturbs other keywords.
    lines = keyword_remove_argfile_lines(["Deidre Hough"], iptc=True, people_prefix="People")
    assert "-XMP-dc:Subject-=Deidre Hough" in lines
    assert "-IPTC:Keywords-=Deidre Hough" in lines
    assert "-XMP-iptcExt:PersonInImage-=Deidre Hough" in lines
    assert "-XMP-lr:HierarchicalSubject-=People|Deidre Hough" in lines


def test_keyword_remove_argfile_lines_honours_toggles():
    bare = keyword_remove_argfile_lines(["x"], iptc=False, people_prefix="")
    assert "-IPTC:Keywords-=x" not in bare  # iptc off
    assert not any("HierarchicalSubject" in line for line in bare)  # no prefix -> no hierarchy
    assert "-XMP-dc:Subject-=x" in bare and "-XMP-iptcExt:PersonInImage-=x" in bare


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


def test_keyword_lines_keep_foreign_hierarchy_and_replace_only_our_branch():
    # H11: a Places|Paris hierarchy added in digiKam must survive; only the People| branch
    # is ours to rewrite.
    existing = KeywordSets(
        subject={"Holiday"},
        hierarchical={"Places|Paris", "People|Stale"},
        persons=set(),
    )
    lines = keyword_argfile_lines(
        existing, tags=set(), people={"Mum"}, prefix="People", owned_people={"Mum", "Stale"}
    )
    hier = [
        ln.split("=", 1)[1]
        for ln in lines
        if ln.startswith("-XMP-lr:HierarchicalSubject=") and ln != "-XMP-lr:HierarchicalSubject="
    ]
    assert "Places|Paris" in hier  # foreign hierarchy preserved
    assert "People|Mum" in hier  # our branch rewritten
    assert "People|Stale" not in hier  # our branch REPLACED, not unioned
    assert lines.count("-XMP-lr:HierarchicalSubject=") == 1  # cleared exactly once


def test_keyword_lines_keep_foreign_person_and_drop_our_renamed_one():
    existing = KeywordSets(subject=set(), hierarchical=set(), persons={"Grandma", "Old Name"})
    lines = keyword_argfile_lines(
        existing, tags=set(), people={"Mum"}, owned_people={"Mum", "Old Name"}
    )
    persons = [
        ln.split("=", 1)[1]
        for ln in lines
        if ln.startswith("-XMP-iptcExt:PersonInImage=") and ln != "-XMP-iptcExt:PersonInImage="
    ]
    assert "Grandma" in persons  # foreign name preserved (photoflow doesn't know it)
    assert "Mum" in persons
    assert "Old Name" not in persons  # a name photoflow OWNS but no longer assigns is dropped


def test_keyword_lines_leave_people_lists_alone_when_there_are_no_people():
    # A tags-only file must not touch PersonInImage/HierarchicalSubject at all.
    existing = KeywordSets(subject=set(), hierarchical={"Places|Paris"}, persons={"Grandma"})
    lines = keyword_argfile_lines(existing, tags={"beach"}, people=set(), owned_people={"Mum"})
    assert not any("HierarchicalSubject" in ln for ln in lines)
    assert not any("PersonInImage" in ln for ln in lines)


def test_keyword_lines_clear_when_our_last_entry_goes_away():
    # Every entry was ours and none is assigned any more -> emit the bare clear line so the
    # stale value actually leaves the file.
    existing = KeywordSets(subject=set(), hierarchical={"People|Old"}, persons={"Old"})
    lines = keyword_argfile_lines(existing, tags=set(), people=set(), owned_people={"Old"})
    assert lines.count("-XMP-lr:HierarchicalSubject=") == 1
    assert not any(ln.startswith("-XMP-lr:HierarchicalSubject=People") for ln in lines)
    assert lines.count("-XMP-iptcExt:PersonInImage=") == 1


def test_keyword_lines_still_accept_a_plain_set_of_subjects():
    # Back-compat: callers/tests that pass just the dc:Subject set keep working.
    lines = keyword_argfile_lines({"Holiday"}, tags={"beach"}, people={"Mum"})
    assert "-XMP-dc:Subject=Holiday" in lines and "-XMP-dc:Subject=beach" in lines
    assert "-XMP-iptcExt:PersonInImage=Mum" in lines


def test_keyword_lines_keep_a_foreign_people_entry_on_a_tags_only_apply():
    # digiKam's people root IS "People", so a People|Grandma it wrote is NOT ours just
    # because of the prefix - it names nobody photoflow knows. Both lists must agree.
    existing = KeywordSets(subject=set(), hierarchical={"People|Grandma"}, persons={"Grandma"})
    lines = keyword_argfile_lines(existing, tags={"beach"}, people=set(), owned_people={"Mum"})
    assert not any("HierarchicalSubject" in ln for ln in lines)
    assert not any("PersonInImage" in ln for ln in lines)


def test_keyword_lines_hierarchy_and_persons_agree_after_a_people_apply():
    # A foreign Grandma survives in BOTH lists; our own stale name leaves BOTH.
    existing = KeywordSets(
        subject=set(),
        hierarchical={"People|Grandma", "People|Old", "Places|Paris"},
        persons={"Grandma", "Old"},
    )
    lines = keyword_argfile_lines(existing, tags=set(), people={"Mum"}, owned_people={"Mum", "Old"})
    hier = {
        ln.split("=", 1)[1]
        for ln in lines
        if ln.startswith("-XMP-lr:HierarchicalSubject=") and ln != "-XMP-lr:HierarchicalSubject="
    }
    persons = {
        ln.split("=", 1)[1]
        for ln in lines
        if ln.startswith("-XMP-iptcExt:PersonInImage=") and ln != "-XMP-iptcExt:PersonInImage="
    }
    assert hier == {"People|Grandma", "People|Mum", "Places|Paris"}
    assert persons == {"Grandma", "Mum"}
    # the two people-shaped lists name exactly the same humans
    assert {h.split("|", 1)[1] for h in hier if h.startswith("People|")} == persons


def test_tag_set_reads_numeric_scalars_and_strips():
    # exiftool -j emits an unquoted number for a purely numeric keyword; core apply writes
    # folder-year keywords like 2019, so dropping these would DELETE them on enrich apply.
    assert _tag_set(2024) == {"2024"}
    assert _tag_set([2024, "beach"]) == {"2024", "beach"}
    assert _tag_set(" beach ") == {"beach"}
    assert _tag_set(None) == set()
    lines = keyword_argfile_lines(KeywordSets(subject=_tag_set(2024)), tags={"beach"}, people=set())
    assert "-XMP-dc:Subject=2024" in lines
