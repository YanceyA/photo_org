"""Unit tests for review_page pure helpers (no exiftool, no Pillow needed)."""

import csv
import json
import re

from photoflow.review_page import (
    CSV_COLUMNS,
    build_payload,
    decision_rows,
    render_page,
    write_decisions_csv,
)


def g(**kw):
    base = dict(
        id=1,
        source_path="C:/src/a.jpg",
        width=4000,
        height=3000,
        size=2_000_000,
        ext="jpg",
        kind="image",
        camera="X100",
        date_taken="2024:01:01 10:00:00",
    )
    base.update(kw)
    return base


GROUPS = {
    7: [
        g(id=1),
        g(id=2, source_path="C:/src/b, with comma.jpg", width=1600, height=1200, size=300_000),
    ]
}


def test_decision_rows_suggests_most_pixels():
    rows = decision_rows(GROUPS, {})
    assert [r["suggestion"] for r in rows] == ["keep", "keep?"]
    assert rows[0]["resolution"] == "4000x3000"
    assert rows[1]["size_kb"] == 293
    assert all(r["decision"] == "" for r in rows)


def test_decision_rows_carry_forward_by_file_id():
    prior = {"2": {"decision": "skip", "merge_from_file_id": ""}}
    rows = decision_rows(GROUPS, prior)
    assert rows[1]["decision"] == "skip"
    assert rows[0]["decision"] == ""


def test_csv_round_trip(tmp_path):
    rows = decision_rows(GROUPS, {"1": {"decision": "keep", "merge_from_file_id": "2"}})
    p = tmp_path / "decisions.csv"
    write_decisions_csv(p, rows)
    with open(p, newline="", encoding="utf-8") as f:
        back = list(csv.DictReader(f))
    assert list(back[0].keys()) == CSV_COLUMNS
    assert [{c: str(r[c]) for c in CSV_COLUMNS} for r in rows] == back


def test_payload_marks_best_suggested_and_decisions():
    rows = decision_rows(GROUPS, {"1": {"decision": "keep", "merge_from_file_id": "2"}})
    p = build_payload(GROUPS, rows, "C:/work", thumbs_ok={1})
    assert p["workdir"] == "C:/work"
    (grp,) = p["groups"]
    assert grp["gid"] == 7
    f1, f2 = grp["files"]
    assert f1["suggested"] and f1["bestRes"] and f1["bestSize"]
    assert not (f2["suggested"] or f2["bestRes"] or f2["bestSize"])
    assert f1["decision"] == "keep" and f1["merge"] == "2"
    assert f2["decision"] == "" and f2["merge"] == ""
    assert f1["thumb"] == "thumbs/1.jpg" and f2["thumb"] is None
    assert f1["w"] == 4000 and f1["size"] == 2_000_000
    assert f1["ext"] == "jpg" and f1["kind"] == "image"
    assert f1["camera"] == "X100" and f1["date"] == "2024:01:01 10:00:00"
    # the exact CSV cell values ride along so the JS can re-serialize byte-compatibly
    assert f1["csv"] == {"resolution": "4000x3000", "size_kb": 1953, "suggestion": "keep"}


def test_payload_relative_path_gets_no_uri():
    groups = {1: [g(id=5, source_path="not/absolute.jpg")]}
    rows = decision_rows(groups, {})
    p = build_payload(groups, rows, "w", thumbs_ok=set())
    assert p["groups"][0]["files"][0]["uri"] is None


def test_payload_absolute_path_gets_uri(tmp_path):
    groups = {1: [g(id=5, source_path=str(tmp_path / "a.jpg"))]}
    rows = decision_rows(groups, {})
    p = build_payload(groups, rows, "w", thumbs_ok=set())
    assert p["groups"][0]["files"][0]["uri"].startswith("file://")


def _extract_data(html_text: str) -> str:
    m = re.search(r'<script id="data" type="application/json">(.*?)</script>', html_text, re.S)
    assert m, "data block missing"
    return m.group(1)


def test_render_page_embeds_parseable_json():
    rows = decision_rows(GROUPS, {})
    payload = build_payload(GROUPS, rows, "C:/work", set())
    html_text = render_page(payload)
    assert json.loads(_extract_data(html_text)) == payload


def test_render_page_contains_state_hardening():
    """Pins the JS-side hardening: storage failure tolerance, save retry,
    stale-overlay cleanup, decision normalization, and attribute-safe esc()."""
    rows = decision_rows(GROUPS, {})
    payload = build_payload(GROUPS, rows, "C:/work", set())
    page = render_page(payload)
    assert "localStorage.removeItem(LSKEY)" in page  # successful save clears the overlay
    assert "function norm(" in page  # decision vocabulary normalized on load
    assert "storage blocked" in page  # persist() warns instead of throwing
    assert "fileHandle = null;" in page  # failed save re-prompts the picker
    assert "if (saving) return;" in page  # concurrent save guard
    assert '.replace(/"/g, "&quot;")' in page  # esc() safe in double-quoted attributes
    assert 'alt=""' in page  # thumbs get alt; anchor only rendered when uri exists
    assert "<a>" not in page  # no dead href-less anchor around thumbnails


def test_render_page_escapes_script_close_in_paths():
    groups = {1: [g(id=9, source_path="C:/evil</script><b>x.jpg")]}
    rows = decision_rows(groups, {})
    payload = build_payload(groups, rows, "w", set())
    data = _extract_data(render_page(payload))
    assert "</script>" not in data  # escaped as <\/script>
    assert json.loads(data) == payload


def test_payload_flags_members_already_in_library():
    """A near-dupe group can pair a new file with an already-copied keeper whose
    'keep' was carried forward; the page needs to know which is which."""
    groups = {7: [g(id=1, status="copied"), g(id=2, status="review", width=10, height=10)]}
    rows = decision_rows(groups, {"1": {"decision": "keep", "merge_from_file_id": ""}})
    f1, f2 = build_payload(groups, rows, "w", set())["groups"][0]["files"]
    assert f1["inLibrary"] is True and f2["inLibrary"] is False
    # members without a status column (older callers / fixtures) are simply not flagged
    plain = build_payload(GROUPS, decision_rows(GROUPS, {}), "w", set())
    assert plain["groups"][0]["files"][0]["inLibrary"] is False


# --- behavioural test of the in-page JS, run under node with a tiny DOM shim ------------

_DOM_SHIM = r"""
const els = {};
function mkEl(key) {
  const el = {
    key, _text: "", _html: "", children: [], qs: {}, style: {}, _classes: new Set(),
    get textContent() { return this._text; },
    set textContent(v) {
      this._text = String(v);
      this._html = String(v).replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;");
    },
    get innerHTML() { return this._html; },
    set innerHTML(v) { this._html = String(v); },
    classList: {
      toggle(c, on) { if (on === undefined) on = !el._classes.has(c);
                      on ? el._classes.add(c) : el._classes.delete(c); },
      contains(c) { return el._classes.has(c); },
    },
    appendChild(c) { this.children.push(c); },
    querySelector(sel) { return this.qs[sel] || (this.qs[sel] = mkEl(key + " " + sel)); },
    scrollIntoView() { scrolled.push(key); },
    click() { if (this.onclick) this.onclick(); },
    blur() {},
    checked: false,
  };
  return el;
}
const scrolled = [], listeners = {};
const document = {
  getElementById(id) { return els[id] || (els[id] = mkEl(id)); },
  createElement(tag) { return mkEl(tag); },
  addEventListener(type, fn) { listeners[type] = fn; },
};
function key(k, target) {  // dispatch a keydown like the browser would
  listeners.keydown({ key: k, target: target || { tagName: "BODY" }, preventDefault() {} });
}
const storage = {};
const localStorage = {
  getItem: (k) => (k in storage ? storage[k] : null),
  setItem: (k, v) => { storage[k] = String(v); },
  removeItem: (k) => { delete storage[k]; },
};
const window = {};
document.getElementById("data").textContent = __DATAJSON__;
"""


def _run_page_js(page: str, probe_js: str) -> dict:
    """Execute the page's <script> under node against the DOM shim, then run probe_js
    (which must console.log one JSON object) and return that object."""
    import shutil
    import subprocess
    import tempfile

    node = shutil.which("node")
    if not node:
        import pytest

        pytest.skip("node not on PATH")
    data = _extract_data(page)
    m = re.search(r"<script>\s*(.*?)</script>", page, re.S)
    assert m, "page script missing"
    src = _DOM_SHIM.replace("__DATAJSON__", json.dumps(data)) + "\n" + m.group(1) + "\n" + probe_js
    with tempfile.TemporaryDirectory() as td:
        p = f"{td}/page.js"
        with open(p, "w", encoding="utf-8") as f:
            f.write(src)
        r = subprocess.run([node, p], capture_output=True, text=True, encoding="utf-8")
    assert r.returncode == 0, r.stderr
    return json.loads(r.stdout.strip().splitlines()[-1])


def test_page_js_group_with_carried_forward_keeper_is_not_decided():
    """Regression: after an incremental import, plan can pair a NEW file with an
    already-copied keeper whose 'keep' is carried forward from the last round.
    Such a group is still undecided (the new member has no decision) - it must not
    count as decided nor be hidden by "hide decided", otherwise the new member is
    never shown and apply holds it forever."""
    groups = {7: [g(id=1, status="copied"), g(id=2, status="review", width=10, height=10)]}
    rows = decision_rows(groups, {"1": {"decision": "keep", "merge_from_file_id": ""}})
    page = render_page(build_payload(groups, rows, "w", set()))
    out = _run_page_js(
        page,
        """
        const out = {};
        out.progress = document.getElementById("progress").textContent;
        out.decidedClass = document.getElementById("g7").classList.contains("decided");
        out.state1 = document.getElementById("f1").querySelector(".state").textContent;
        out.state2 = document.getElementById("f2").querySelector(".state").textContent;
        document.getElementById("hide").onchange({ target: { checked: true } });
        out.displayWhenHiding = document.getElementById("g7").style.display;
        // clicking Keep on the (already-kept) library member confirms it: blanks -> skip
        window.pf.keep(1);
        out.afterConfirm = { ...window.pf.dec };
        out.displayAfterConfirm = document.getElementById("g7").style.display;
        out.progressAfterConfirm = document.getElementById("progress").textContent;
        // clicking it again is the normal undo: zero keepers -> whole group back on hold
        window.pf.keep(1);
        out.afterUndo = { ...window.pf.dec };
        // keeping the new member instead auto-skips the library member
        window.pf.keep(2);
        out.afterKeep2 = { ...window.pf.dec };
        console.log(JSON.stringify(out));
        """,
    )
    assert out["progress"] == "decided 0 / 1 groups"
    assert out["decidedClass"] is False
    assert out["state1"] == "KEEP · in library"
    assert out["state2"] == "on hold"
    assert out["displayWhenHiding"] == ""  # still visible under "hide decided"
    assert out["afterConfirm"] == {"1": "keep", "2": "skip"}
    assert out["displayAfterConfirm"] == "none"
    assert out["progressAfterConfirm"] == "decided 1 / 1 groups"
    assert out["afterUndo"] == {"1": "", "2": ""}
    assert out["afterKeep2"] == {"1": "skip", "2": "keep"}


def test_page_js_fresh_group_click_semantics_unchanged():
    """Baseline behaviour for an ordinary (all-blank) group: keep one -> others skip;
    keep the other too -> both keep; click a keeper again -> it becomes skip while
    another keeper remains, and the group reads as decided throughout."""
    page = render_page(build_payload(GROUPS, decision_rows(GROUPS, {}), "w", set()))
    out = _run_page_js(
        page,
        """
        const out = {};
        out.p0 = document.getElementById("progress").textContent;
        window.pf.keep(1); out.a = { ...window.pf.dec };
        out.p1 = document.getElementById("progress").textContent;
        window.pf.keep(2); out.b = { ...window.pf.dec };
        window.pf.keep(1); out.c = { ...window.pf.dec };
        window.pf.keep(2); out.d = { ...window.pf.dec };
        out.p2 = document.getElementById("progress").textContent;
        console.log(JSON.stringify(out));
        """,
    )
    assert out["p0"] == "decided 0 / 1 groups"
    assert out["a"] == {"1": "keep", "2": "skip"} and out["p1"] == "decided 1 / 1 groups"
    assert out["b"] == {"1": "keep", "2": "keep"}
    assert out["c"] == {"1": "skip", "2": "keep"}
    assert out["d"] == {"1": "", "2": ""} and out["p2"] == "decided 0 / 1 groups"


GROUPS3 = {
    7: [g(id=1), g(id=2, width=10, height=10)],
    8: [g(id=3, width=10, height=10), g(id=4)],  # suggested keeper is the 2nd card here
    9: [g(id=5), g(id=6, width=10, height=10)],
}


def test_page_js_fixed_card_geometry():
    """Keep buttons must sit at the same offset in every card: constant thumb box,
    single-line stats/meta, clamped path, fixed card width."""
    page = render_page(build_payload(GROUPS, decision_rows(GROUPS, {}), "w", set()))
    assert ".thumb{height:220px" in page
    assert ".f{" in page and "width:280px" in page
    assert "-webkit-line-clamp" in page  # path clamped to a fixed number of lines
    assert "white-space:nowrap" in page  # stats/meta never wrap
    assert 'class="num"' in page  # card index labels for the 1-9 hotkeys


def test_page_js_accept_suggested_and_cursor():
    """Enter / the per-group button keep the suggested member (others skip) and
    advance the cursor; digits keep the Nth card; arrows move; the cursor skips
    hidden groups under "hide decided"."""
    page = render_page(build_payload(GROUPS3, decision_rows(GROUPS3, {}), "w", set()))
    out = _run_page_js(
        page,
        """
        const out = {};
        out.cur0 = window.pf.cur();                       // first group on load
        key("Enter");                                     // accept 7 -> keeper 1
        out.afterEnter = { ...window.pf.dec }; out.cur1 = window.pf.cur();
        key("Enter");                                     // accept 8 -> keeper 4 (suggested is 2nd)
        out.afterEnter2 = { ...window.pf.dec }; out.cur2 = window.pf.cur();
        key("ArrowUp"); out.curUp = window.pf.cur();
        key("ArrowDown"); key("ArrowDown"); out.curDown2 = window.pf.cur();   // wraps? no: 7->8->9
        key("2");                                         // keep 2nd card of group 7 too (id 2)
        out.afterDigit = { ...window.pf.dec }; out.cur3 = window.pf.cur();
        out.curCls = document.getElementById("g7").classList.contains("cur");
        // hide decided: everything decided now -> no cursor
        key("h"); out.hidden = document.getElementById("hide").checked;
        out.curHidden = window.pf.cur();
        window.pf.keep(4);                       // click the lone keeper: group 8 back on hold
        out.afterUndo = { ...window.pf.dec };
        out.curAfterUndo = window.pf.cur();
        window.pf.accept(8);
        out.afterAccept8 = { ...window.pf.dec }; out.curAfterAccept8 = window.pf.cur();
        out.scrolled = scrolled.length > 0;
        console.log(JSON.stringify(out));
        """,
    )
    assert out["cur0"] == 7
    assert out["afterEnter"] == {"1": "keep", "2": "skip", "3": "", "4": "", "5": "", "6": ""}
    assert out["cur1"] == 8
    assert out["afterEnter2"]["3"] == "skip" and out["afterEnter2"]["4"] == "keep"
    assert out["cur2"] == 9
    assert out["curUp"] == 8 and out["curDown2"] == 7  # 8 -> 9 -> wraps to 7
    # digit acts on the cursor group (7 after the wrap): 2nd card is id 2 -> keep both
    assert out["afterDigit"]["2"] == "keep" and out["afterDigit"]["1"] == "keep"
    assert out["cur3"] == 7 and out["curCls"] is True
    assert out["hidden"] is True
    assert out["curHidden"] in (9, None)  # only group 9 is still undecided -> cursor there
    assert out["afterUndo"]["3"] == "" and out["afterUndo"]["4"] == ""  # group 8 back on hold
    assert out["afterAccept8"]["4"] == "keep" and out["afterAccept8"]["3"] == "skip"
    assert out["scrolled"] is True


def test_page_js_keys_ignored_in_inputs():
    page = render_page(build_payload(GROUPS, decision_rows(GROUPS, {}), "w", set()))
    out = _run_page_js(
        page,
        """
        key("Enter", { tagName: "INPUT" });
        console.log(JSON.stringify({ ...window.pf.dec }));
        """,
    )
    assert out == {"1": "", "2": ""}
