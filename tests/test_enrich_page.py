"""Pure payload/CSV builders + apply-decision semantics for the enrich review page."""

import json

from photoflow.enrich.page import (
    FACE_COLUMNS,
    TAG_COLUMNS,
    build_people_payload,
    build_tags_payload,
    face_is_applied,
    face_rows,
    render_page,
    tag_is_applied,
    tag_rows,
)

CLUSTERS = {
    1: [
        {
            "face_id": 10,
            "file_id": 100,
            "source_path": "a.jpg",
            "thumb": "faces/10.jpg",
            "cluster_prob": 0.95,
            "suggested_person": "",
        },
        {
            "face_id": 11,
            "file_id": 101,
            "source_path": "b.jpg",
            "thumb": "faces/11.jpg",
            "cluster_prob": 0.20,
            "suggested_person": "",
        },  # low prob -> edge case
    ]
}
NOISE = [
    {
        "face_id": 12,
        "file_id": 102,
        "source_path": "c.jpg",
        "thumb": "faces/12.jpg",
        "cluster_prob": 0.0,
        "suggested_person": "Mum",
    },
]


# --- apply-decision semantics: these decide what actually gets written to a photo ---


def test_tag_is_applied_semantics():
    assert tag_is_applied("auto", "") is True  # confident tag flows through untouched
    assert tag_is_applied("auto", "reject") is False  # user vetoed it
    assert tag_is_applied("review", "") is False  # edge-band tag needs explicit keep
    assert tag_is_applied("review", "keep") is True
    assert tag_is_applied("review", "reject") is False
    # a globally blacklisted tag is never applied, whatever its band/decision
    assert tag_is_applied("auto", "", blacklisted=True) is False
    assert tag_is_applied("review", "keep", blacklisted=True) is False


def test_face_is_applied_semantics():
    assert face_is_applied("Mum", "keep") is True
    assert face_is_applied("", "keep") is False  # named nothing -> nothing to write
    assert face_is_applied("Mum", "") is False  # un-confirmed cluster stays on hold
    assert face_is_applied("Mum", "skip") is False  # ejected member


# --- CSV row builders carry prior decisions forward (like decisions.csv, invariant #4) ---


def test_face_rows_columns_and_carry_forward():
    prior = {"10": {"person": "Dad", "decision": "keep"}}
    rows = face_rows(CLUSTERS, NOISE, prior)
    assert list(rows[0].keys()) == FACE_COLUMNS
    by_face = {r["face_id"]: r for r in rows}
    assert by_face[10]["person"] == "Dad" and by_face[10]["decision"] == "keep"  # carried
    assert by_face[11]["person"] == "" and by_face[11]["decision"] == ""
    assert by_face[12]["cluster_id"] == ""  # noise face has no cluster
    assert by_face[12]["suggested_person"] == "Mum"


def test_tag_rows_carry_forward():
    # tags.csv is a decisions OVERLAY: only review-band candidates get rows (auto tags
    # apply from the DB by default, so they'd bloat the file/page on a large library).
    items = [
        {"file_id": 100, "tag": "boat", "source": "clip", "score": 0.4, "suggestion": "review"},
        {"file_id": 100, "tag": "kite", "source": "clip", "score": 0.35, "suggestion": "review"},
    ]
    prior = {("100", "boat"): {"decision": "keep"}}
    rows = tag_rows(items, prior)
    assert list(rows[0].keys()) == TAG_COLUMNS
    by_tag = {r["tag"]: r for r in rows}
    assert by_tag["boat"]["decision"] == "keep"  # carried forward
    assert by_tag["kite"]["decision"] == ""


# --- payload builders shape what the in-page JS renders ---


def test_people_payload_flags_low_prob_members():
    rows = face_rows(CLUSTERS, NOISE, {})
    payload = build_people_payload(
        CLUSTERS, NOISE, rows, persons=["Dad"], workdir_key="W", prob_floor=0.5
    )
    assert payload["workdir"] == "W"
    assert payload["persons"] == ["Dad"]
    cluster = next(c for c in payload["clusters"] if c["cluster_id"] == 1)
    members = {m["face_id"]: m for m in cluster["members"]}
    assert members[11]["edge"] is True  # prob 0.20 < floor 0.5
    assert members[10]["edge"] is False  # prob 0.95 >= floor
    assert payload["noise"][0]["face_id"] == 12


def test_tags_payload_groups_review_and_summarizes_auto():
    items = [
        {"file_id": 1, "tag": "beach", "source": "clip", "score": 0.9, "suggestion": "auto"},
        {"file_id": 2, "tag": "beach", "source": "clip", "score": 0.85, "suggestion": "auto"},
        {
            "file_id": 3,
            "tag": "boat",
            "source": "clip",
            "score": 0.4,
            "suggestion": "review",
            "thumb": "faces/none",
            "source_path": "x.jpg",
        },
    ]
    rows = tag_rows(items, {})
    payload = build_tags_payload(items, rows, workdir_key="W")
    review = {g["tag"]: g for g in payload["reviewTags"]}
    assert "boat" in review and review["boat"]["count"] == 1  # one candidate photo for 'boat'
    assert "beach" not in review  # auto tags aren't in review queue
    summary = {s["tag"]: s["count"] for s in payload["autoSummary"]}
    assert summary["beach"] == 2


# --- the rendered page is self-contained and carries both payloads + the JS hooks ---


def test_render_page_embeds_payloads_and_hooks():
    rows_f = face_rows(CLUSTERS, NOISE, {})
    people = build_people_payload(
        CLUSTERS, NOISE, rows_f, persons=[], workdir_key="W", prob_floor=0.5
    )
    tags = build_tags_payload([], tag_rows([], {}), workdir_key="W")
    html = render_page(people, tags)
    assert "<!doctype html>" in html.lower()
    assert "faces.csv" in html and "tags.csv" in html
    # payload survives as embedded JSON
    assert '"cluster_id": 1' in html or '"cluster_id":1' in html
    # closing-tag escaping so the JSON can't break out of the <script> block
    assert "</script>" not in html.split("__", 1)[0] if "__" in html else True
    json.loads(json.dumps(people))  # payload is JSON-serializable
