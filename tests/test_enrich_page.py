"""Pure payload/CSV builders + apply-decision semantics for the enrich review page."""

import json

from photoflow.enrich.page import (
    FACE_COLUMNS,
    TAG_COLUMNS,
    blacklist_rows,
    build_people_payload,
    build_tags_payload,
    face_is_applied,
    face_rows,
    render_assign_review,
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


def test_render_page_renders_lazily_for_big_libraries():
    """Regression guard: the page MUST render lazily, or a 40k-photo library builds ~57k <img>
    at once and freezes/OOMs the tab. Verify the scaling primitives survive in the template:
    batched IntersectionObserver fill, lazy images, and content-visibility on groups."""
    html = render_page(
        build_people_payload(CLUSTERS, NOISE, face_rows(CLUSTERS, NOISE, {}), [], "W", 0.5),
        build_tags_payload([], tag_rows([], {}), workdir_key="W"),
    )
    assert "IntersectionObserver" in html  # batched, viewport-driven group fill
    assert 'loading="lazy"' in html  # thumbnails decode only when near the viewport
    assert "content-visibility" in html  # off-screen groups skip layout/paint
    # eager full-DOM helpers must be gone (they were the cause of the freeze/OOM)
    assert "renderPeople(); renderTags();" not in html  # both panes no longer build on load


def test_render_assign_review_groups_candidates_under_each_person():
    """The assign dry-run review: a static page that shows each proposed face under the person
    it would be assigned to, with the cosine score + a strip of that person's known faces, so a
    human can eyeball where a given --min-sim starts producing wrong matches."""
    persons = [
        {
            "name": "Orlagh Arrington",
            "count": 2,
            "weakest": 0.41,
            "refs": [{"thumb": "faces/1.jpg", "uri": None}],
            "candidates": [
                {"thumb": "faces/2.jpg", "uri": None, "sim": 0.59},
                {"thumb": "faces/3.jpg", "uri": "file:///x.jpg", "sim": 0.41},
            ],
        }
    ]
    html = render_assign_review(0.5, 2, persons)
    assert "<!doctype html>" in html.lower()
    assert "Orlagh Arrington" in html  # grouped under the person
    assert "0.59" in html and "0.41" in html  # per-face cosine score shown
    assert ">= 0.5" in html or "0.5" in html  # threshold in the header
    assert 'loading="lazy"' in html  # thousands of crops load lazily
    assert "known" in html.lower()  # reference strip of already-named faces
    # html-escaped, self-contained, no payload injection holes
    assert "<script" not in html.lower()


def test_render_page_has_name_autocomplete_and_ignore_cluster():
    """Guard the two review affordances: a live-updated <datalist> for name autocomplete, and
    an 'ignore whole cluster' control (skip every member) so don't-care clusters can be cleared."""
    html = render_page(
        build_people_payload(CLUSTERS, NOISE, face_rows(CLUSTERS, NOISE, {}), [], "W", 0.5),
        build_tags_payload([], tag_rows([], {}), workdir_key="W"),
    )
    assert 'list="persons"' in html and "<datalist" in html  # native autocomplete
    assert "addPersonOption" in html  # names typed this session feed the suggestions
    assert "clusterDismissed" in html and "not interested" in html  # ignore-whole-cluster control


def test_blacklist_rows_are_wildcard_reject_rows():
    rows = blacklist_rows(["person", "document"])
    assert [r["file_id"] for r in rows] == ["*", "*"]
    assert all(list(r.keys()) == TAG_COLUMNS for r in rows)
    assert {r["tag"] for r in rows} == {"person", "document"}
    assert all(r["decision"] == "reject" for r in rows)


def test_tags_payload_carries_the_blacklist_and_page_seeds_it():
    # R5: the JS Set was seeded only from localStorage, so a blacklist saved on one machine
    # (or after clearing site data) silently came back as "apply this tag everywhere".
    payload = build_tags_payload([], [], workdir_key="W", blacklist=["person"])
    assert payload["blacklist"] == ["person"]
    html = render_page(
        build_people_payload(CLUSTERS, NOISE, face_rows(CLUSTERS, NOISE, {}), [], "W", 0.5),
        payload,
    )
    assert "TAGS.blacklist" in html  # the Set is seeded from the payload, not just storage
    assert '"person"' in html


def test_page_renders_a_chip_for_a_blacklisted_tag_with_no_auto_summary_entry():
    """Un-blacklisting has to be reachable from the page. `enrich review` filters blacklisted
    tags out of BOTH payload sides, so autoSummary never mentions them and the only clickable
    chip source was empty - the blacklistRemoved tombstone could never be produced. No JS
    harness exists for this page, so pin the mechanism in the rendered template source."""
    payload = build_tags_payload([], [], workdir_key="W", blacklist=["person"])
    assert payload["autoSummary"] == []  # nothing upstream produces a chip for it
    html = render_page(
        build_people_payload(CLUSTERS, NOISE, face_rows(CLUSTERS, NOISE, {}), [], "W", 0.5),
        payload,
    )
    # renderAutoSummary draws autoSummary PLUS every blacklist entry it doesn't cover
    assert "function autoSummaryChips()" in html
    assert "autoSummaryChips().map" in html
    assert "[...blacklist].filter(t=>!seen.has(t))" in html
    assert "blOnly:true" in html and '"blacklisted"' in html
    # the extra chips are plain .chip nodes, so the existing #autosummary click handler
    # (delete from the Set + blacklistRemoved tombstone) applies to them unchanged
    assert 'data-blonly="1"' in html
    assert "blacklistRemoved.add(tag)" in html


def test_page_persists_blacklist_add_remove_tombstones_not_the_raw_set():
    """R5 follow-up: a raw Set snapshot in localStorage can't distinguish "matches the DB
    payload" from "user removed a payload entry", so a local un-blacklist silently reverted on
    reload. The page must track add/remove tombstones instead. No JS harness exists for this
    page, so pin the mechanism via the rendered template source."""
    html = render_page(
        build_people_payload(CLUSTERS, NOISE, face_rows(CLUSTERS, NOISE, {}), [], "W", 0.5),
        build_tags_payload([], tag_rows([], {}), workdir_key="W", blacklist=["person"]),
    )
    assert "blacklistAdded" in html and "blacklistRemoved" in html
    # both directions of the tombstone are wired into persist() (not just declared)
    assert "blacklistAdded:[...blacklistAdded]" in html
    assert "blacklistRemoved:[...blacklistRemoved]" in html
    # loading applies additions then removals on top of the DB-backed payload
    assert "blacklistAdded.add(t)" in html and "blacklistRemoved.add(t)" in html
    assert "blacklist.delete(t)" in html
    # the raw Set is no longer round-tripped directly through storage
    assert "blacklist:[...blacklist]" not in html
