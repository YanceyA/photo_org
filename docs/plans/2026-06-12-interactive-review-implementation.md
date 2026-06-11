# Interactive Review Page Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Turn the static `review.html` into a clickable editor for `decisions.csv` (click keepers, rest auto-skip, donate-metadata toggle, richer comparison cards, save back via browser file picker).

**Architecture:** New pure module `src/photoflow/review_page.py` holds the CSV-row builder, JSON payload builder, and the HTML/JS template (single template string, vanilla JS, no dependencies). `review.py` shrinks to orchestration: query DB → build rows → write CSV → make thumbs → write HTML. `apply.py` is untouched; the CSV stays the source of truth. Design doc: `docs/plans/2026-06-12-interactive-review-design.md`.

**Tech Stack:** Python 3.11+, stdlib `csv`/`json`, vanilla JS (localStorage + File System Access API with download fallback), pytest (new tests are pure — no `@pytest.mark.exiftool`).

**Invariants to preserve:** decisions.csv carry-forward by file_id (invariant #4); CSV format byte-compatible (same 8 columns, same header, `\r\n` line endings — Python `csv` default); blank decision = hold.

**Working directory:** the worktree root (`.worktrees/interactive-review`). Run commands from there.

---

### Task 1: Pure CSV helpers in `review_page.py`

**Files:**
- Create: `src/photoflow/review_page.py`
- Create: `tests/test_review_page.py`

**Step 1: Write the failing tests**

Create `tests/test_review_page.py`:

```python
"""Unit tests for review_page pure helpers (no exiftool, no Pillow needed)."""

import csv

from photoflow.review_page import CSV_COLUMNS, decision_rows, write_decisions_csv


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
```

**Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_review_page.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'photoflow.review_page'`

**Step 3: Write minimal implementation**

Create `src/photoflow/review_page.py`:

```python
"""Pure helpers + HTML/JS template for the interactive review page.

No I/O beyond what the caller hands in: cmd_review feeds plain dict-like rows
and writes the returned strings. Testable without exiftool or Pillow.
"""

from __future__ import annotations

import csv
import json
from pathlib import Path

CSV_COLUMNS = [
    "group_id",
    "file_id",
    "source_path",
    "resolution",
    "size_kb",
    "suggestion",
    "decision",
    "merge_from_file_id",
]


def suggested_keeper_id(members) -> int:
    best = max(members, key=lambda m: (m["width"] or 0) * (m["height"] or 0))
    return best["id"]


def decision_rows(groups, prior: dict[str, dict]) -> list[dict]:
    """decisions.csv rows; carries forward decision/merge by file_id (invariant #4)."""
    rows = []
    for gid, members in groups.items():
        best_id = suggested_keeper_id(members)
        for m in members:
            old = prior.get(str(m["id"]), {})
            rows.append(
                {
                    "group_id": gid,
                    "file_id": m["id"],
                    "source_path": m["source_path"],
                    "resolution": f"{m['width']}x{m['height']}",
                    "size_kb": round((m["size"] or 0) / 1024),
                    "suggestion": "keep" if m["id"] == best_id else "keep?",
                    "decision": old.get("decision", ""),
                    "merge_from_file_id": old.get("merge_from_file_id", ""),
                }
            )
    return rows


def write_decisions_csv(path, rows: list[dict]) -> None:
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=CSV_COLUMNS)
        w.writeheader()
        w.writerows(rows)
```

(`json` and `Path` imports are used in Tasks 2–3.)

**Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_review_page.py -v`
Expected: 3 PASS (ruff may flag the unused imports — fine to add them in Task 2 instead if it does)

**Step 5: Commit**

```bash
git add src/photoflow/review_page.py tests/test_review_page.py
git commit -m "feat: extract pure decisions.csv builders into review_page module"
```

---

### Task 2: `build_payload()` — the JSON the page runs on

**Files:**
- Modify: `src/photoflow/review_page.py`
- Modify: `tests/test_review_page.py`

**Step 1: Write the failing tests** (append to `tests/test_review_page.py`)

```python
from photoflow.review_page import build_payload


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
```

**Step 2: Run to verify failure**

Run: `uv run pytest tests/test_review_page.py -v`
Expected: new tests FAIL — `ImportError: cannot import name 'build_payload'`

**Step 3: Implement** (append to `review_page.py`)

```python
def _file_uri(p: str) -> str | None:
    try:
        return Path(p).as_uri()
    except ValueError:
        return None


def build_payload(groups, rows: list[dict], workdir_key: str, thumbs_ok: set) -> dict:
    """Everything the in-page JS needs: display fields, group-best flags, and the
    exact CSV cell values so the browser can re-serialize a byte-compatible CSV."""
    by_id = {r["file_id"]: r for r in rows}
    out = []
    for gid, members in groups.items():
        best_id = suggested_keeper_id(members)
        max_px = max((m["width"] or 0) * (m["height"] or 0) for m in members)
        max_size = max((m["size"] or 0) for m in members)
        files = []
        for m in members:
            r = by_id[m["id"]]
            px = (m["width"] or 0) * (m["height"] or 0)
            files.append(
                {
                    "id": m["id"],
                    "path": m["source_path"],
                    "uri": _file_uri(m["source_path"]),
                    "thumb": f"thumbs/{m['id']}.jpg" if m["id"] in thumbs_ok else None,
                    "w": m["width"],
                    "h": m["height"],
                    "size": m["size"] or 0,
                    "ext": m["ext"],
                    "kind": m["kind"],
                    "camera": m["camera"],
                    "date": m["date_taken"],
                    "suggested": m["id"] == best_id,
                    "bestRes": px == max_px and px > 0,
                    "bestSize": (m["size"] or 0) == max_size and max_size > 0,
                    "csv": {
                        "resolution": r["resolution"],
                        "size_kb": r["size_kb"],
                        "suggestion": r["suggestion"],
                    },
                    "decision": r["decision"],
                    "merge": str(r["merge_from_file_id"] or ""),
                }
            )
        out.append({"gid": gid, "files": files})
    return {"workdir": workdir_key, "groups": out}
```

**Step 4: Run tests**

Run: `uv run pytest tests/test_review_page.py -v`
Expected: 5 PASS

**Step 5: Commit**

```bash
git add src/photoflow/review_page.py tests/test_review_page.py
git commit -m "feat: build_payload for interactive review page"
```

---

### Task 3: `render_page()` — HTML/CSS/JS template

The whole front-end. JSON goes in a `<script type="application/json">` block (with `</` escaped so a hostile filename can't close the tag); the JS app renders cards, manages state, persists to localStorage, and serializes a byte-compatible CSV.

**Files:**
- Modify: `src/photoflow/review_page.py`
- Modify: `tests/test_review_page.py`

**Step 1: Write the failing tests** (append)

```python
import json
import re

from photoflow.review_page import render_page


def _extract_data(html_text: str) -> str:
    m = re.search(
        r'<script id="data" type="application/json">(.*?)</script>', html_text, re.S
    )
    assert m, "data block missing"
    return m.group(1)


def test_render_page_embeds_parseable_json():
    rows = decision_rows(GROUPS, {})
    payload = build_payload(GROUPS, rows, "C:/work", set())
    html_text = render_page(payload)
    assert json.loads(_extract_data(html_text)) == payload


def test_render_page_escapes_script_close_in_paths():
    groups = {1: [g(id=9, source_path="C:/evil</script><b>x.jpg")]}
    rows = decision_rows(groups, {})
    payload = build_payload(groups, rows, "w", set())
    data = _extract_data(render_page(payload))
    assert "</script>" not in data  # escaped as <\/script>
    assert json.loads(data) == payload
```

**Step 2: Run to verify failure**

Run: `uv run pytest tests/test_review_page.py -v`
Expected: FAIL — `cannot import name 'render_page'`

**Step 3: Implement** (append to `review_page.py`; the template is one plain string — use `.replace("__DATA__", ...)`, never `.format()`, because the CSS/JS is full of braces)

```python
def render_page(payload: dict) -> str:
    data = json.dumps(payload, ensure_ascii=False).replace("</", "<\\/")
    return PAGE_TEMPLATE.replace("__DATA__", data)


PAGE_TEMPLATE = """<!doctype html>
<html><head><meta charset="utf-8"><title>photoflow review</title>
<style>
:root{color-scheme:dark}
body{font-family:system-ui,sans-serif;background:#111;color:#ddd;margin:0}
header{position:sticky;top:0;background:#1a1a1a;border-bottom:1px solid #333;
  padding:10px 16px;display:flex;gap:16px;align-items:center;z-index:10;flex-wrap:wrap}
header h1{font-size:16px;margin:0}
#progress{color:#9ab}
button{background:#2a2a2a;color:#ddd;border:1px solid #555;border-radius:6px;
  padding:6px 12px;cursor:pointer;font-size:13px}
button:hover{border-color:#888}
#save.dirty{border-color:#e6b450;color:#e6b450}
#save.saved{border-color:#5c5;color:#5c5}
.hint{font-size:12px;color:#789;margin:8px 16px}
.g{border:1px solid #444;margin:14px;padding:10px;border-radius:8px}
.g.decided{border-color:#2a4a2a}
.g h3{margin:2px 0 8px;font-size:14px;color:#9ab}
.cards{display:flex;flex-wrap:wrap;gap:10px}
.f{border:2px solid #333;border-radius:8px;padding:8px;max-width:280px;
  text-align:center;background:#191919}
.f.suggested{border-style:dashed;border-color:#7a7}
.f.keep{border-style:solid;border-color:#3c3;background:#1c241c}
.f.skip{opacity:.45}
.f img{max-height:220px;max-width:264px;border-radius:4px;cursor:zoom-in}
.stats{font-size:12px;margin:6px 0;color:#bcd}
.hl{color:#6e6;font-weight:600}
.badge{display:inline-block;padding:0 5px;border-radius:4px;font-size:11px;
  background:#444;color:#fff;margin-left:4px}
.badge.raw{background:#85a}
.badge.video{background:#58a}
.path{font-size:11px;color:#789;word-break:break-all;max-width:264px;margin:4px 0}
.meta{font-size:11px;color:#9ab}
.actions{margin-top:6px;display:flex;gap:6px;justify-content:center}
.keepbtn.on{border-color:#3c3;color:#3c3}
.donate{font-size:11px;padding:3px 8px}
.donate.on{border-color:#5af;color:#5af}
.state{font-size:11px;height:14px;margin-top:4px;color:#888}
</style></head><body>
<header>
  <h1>photoflow review</h1>
  <span id="progress"></span>
  <label class="meta"><input type="checkbox" id="hide"> hide decided</label>
  <button id="save">Save decisions.csv</button>
  <span id="savemsg" class="meta"></span>
</header>
<p class="hint">Click <b>Keep</b> on the photo(s) to keep in each group — the rest
auto-skip. Click a keeper again to undo. Untouched groups stay on hold. Click a
thumbnail to open the original full size. Selections survive closing the tab;
<b>Save decisions.csv</b> writes them back into your workdir (overwrite
decisions.csv when the picker asks), then run <code>photoflow apply</code>.</p>
<main id="groups"></main>
<script id="data" type="application/json">__DATA__</script>
<script>
"use strict";
const DATA = JSON.parse(document.getElementById("data").textContent);
const LSKEY = "photoflow-review:" + DATA.workdir;
const COLS = ["group_id","file_id","source_path","resolution","size_kb",
              "suggestion","decision","merge_from_file_id"];
const byGid = {}, groupOf = {}, dec = {}, donorOf = {};
let fileHandle = null, dirty = false, savedOnce = false, hideDecided = false;

for (const g of DATA.groups) {
  byGid[g.gid] = g;
  donorOf[g.gid] = null;
  for (const f of g.files) {
    groupOf[f.id] = g.gid;
    dec[f.id] = f.decision || "";
    if (f.decision === "keep" && f.merge) donorOf[g.gid] = Number(f.merge);
  }
}
try {  // localStorage overlays the CSV baseline (crash insurance)
  const saved = JSON.parse(localStorage.getItem(LSKEY) || "null");
  if (saved) {
    for (const [id, d] of Object.entries(saved.dec || {})) if (id in dec) dec[id] = d;
    for (const [gid, d] of Object.entries(saved.donorOf || {}))
      if (gid in donorOf) donorOf[gid] = d;
  }
} catch (e) { /* corrupt storage: fall back to CSV state */ }

function esc(s) {
  const d = document.createElement("span");
  d.textContent = s == null ? "" : String(s);
  return d.innerHTML;
}
function fmtSize(b) {
  return b >= 1048576 ? (b / 1048576).toFixed(1) + " MB" : Math.round(b / 1024) + " KB";
}
function fmtMp(f) {
  return f.w && f.h ? ((f.w * f.h) / 1e6).toFixed(1) + " MP" : "? MP";
}

function build() {
  const main = document.getElementById("groups");
  for (const g of DATA.groups) {
    const div = document.createElement("div");
    div.className = "g";
    div.id = "g" + g.gid;
    let cards = "";
    for (const f of g.files) {
      const img = f.thumb
        ? (f.uri ? '<a href="' + esc(f.uri) + '" target="_blank">' : "<a>") +
          '<img src="' + esc(f.thumb) + '" title="open full size"></a>'
        : '<div class="meta" style="height:120px;line-height:120px">(no preview)</div>';
      const badge = f.kind === "raw" ? '<span class="badge raw">RAW</span>'
        : f.kind === "video" ? '<span class="badge video">VIDEO</span>' : "";
      cards += '<div class="f" id="f' + f.id + '">' + img +
        '<div class="stats"><span class="' + (f.bestRes ? "hl" : "") + '">' +
        esc(f.w || "?") + "\\u00d7" + esc(f.h || "?") + " \\u00b7 " + fmtMp(f) +
        '</span> \\u00b7 <span class="' + (f.bestSize ? "hl" : "") + '">' +
        fmtSize(f.size) + "</span> \\u00b7 " + esc((f.ext || "").toUpperCase()) + badge +
        '</div><div class="meta">' + esc(f.camera || "unknown camera") + " \\u00b7 " +
        esc(f.date || "no date") + '</div><div class="path">' + esc(f.path) + "</div>" +
        '<div class="actions">' +
        '<button class="keepbtn" onclick="pf.keep(' + f.id + ')">Keep</button>' +
        '<button class="donate" onclick="pf.donate(' + f.id + ')" ' +
        'title="copy missing metadata (GPS, dates) from this file into the keeper">' +
        "\\u2192 donate metadata</button></div>" +
        '<div class="state"></div></div>';
    }
    div.innerHTML = "<h3>group " + g.gid + " \\u00b7 " + g.files.length +
      ' files</h3><div class="cards">' + cards + "</div>";
    main.appendChild(div);
  }
}

function clickKeep(id) {
  const gid = groupOf[id], mem = byGid[gid].files;
  if (dec[id] === "keep") {
    dec[id] = "";
    if (!mem.some((f) => dec[f.id] === "keep")) {
      mem.forEach((f) => (dec[f.id] = ""));  // zero keepers: whole group back to hold
      donorOf[gid] = null;
    } else {
      dec[id] = "skip";  // other keepers remain; this one becomes a skip
    }
  } else {
    dec[id] = "keep";
    mem.forEach((f) => { if (dec[f.id] !== "keep") dec[f.id] = "skip"; });
    if (donorOf[gid] === id) donorOf[gid] = null;
  }
  persist();
  refresh();
}

function clickDonate(id) {
  const gid = groupOf[id];
  if ((dec[id] || "") !== "skip") return;
  donorOf[gid] = donorOf[gid] === id ? null : id;
  persist();
  refresh();
}

function csvField(v) {
  v = String(v == null ? "" : v);
  return /[",\\r\\n]/.test(v) ? '"' + v.replace(/"/g, '""') + '"' : v;
}

function serializeCsv() {
  const lines = [COLS.join(",")];
  for (const g of DATA.groups) {
    const hasKeep = g.files.some((f) => dec[f.id] === "keep");
    const donor = donorOf[g.gid];
    const donorOk = hasKeep && donor != null &&
      g.files.some((f) => f.id === donor && dec[f.id] === "skip");
    for (const f of g.files) {
      const d = dec[f.id] || "";
      const merge = d === "keep" && donorOk ? donor : "";
      lines.push([g.gid, f.id, f.path, f.csv.resolution, f.csv.size_kb,
                  f.csv.suggestion, d, merge].map(csvField).join(","));
    }
  }
  return lines.join("\\r\\n") + "\\r\\n";
}

function persist() {
  dirty = true;
  localStorage.setItem(LSKEY, JSON.stringify({ dec: dec, donorOf: donorOf }));
}

function refresh() {
  let decided = 0;
  for (const g of DATA.groups) {
    const hasKeep = g.files.some((f) => dec[f.id] === "keep");
    const isDecided = g.files.some((f) => dec[f.id]);
    if (isDecided) decided++;
    const gdiv = document.getElementById("g" + g.gid);
    gdiv.classList.toggle("decided", isDecided);
    gdiv.style.display = hideDecided && isDecided ? "none" : "";
    for (const f of g.files) {
      const el = document.getElementById("f" + f.id), d = dec[f.id] || "";
      el.classList.toggle("keep", d === "keep");
      el.classList.toggle("skip", d === "skip");
      el.classList.toggle("suggested", !d && f.suggested);
      el.querySelector(".keepbtn").classList.toggle("on", d === "keep");
      const don = el.querySelector(".donate");
      don.style.display = d === "skip" && hasKeep ? "" : "none";
      don.classList.toggle("on", donorOf[g.gid] === f.id);
      el.querySelector(".state").textContent =
        d === "" ? (f.suggested ? "suggested keeper" : "on hold")
        : d === "keep" ? "KEEP"
        : donorOf[g.gid] === f.id ? "skip \\u00b7 donates metadata" : "skip";
    }
  }
  document.getElementById("progress").textContent =
    "decided " + decided + " / " + DATA.groups.length + " groups";
  const save = document.getElementById("save");
  save.classList.toggle("dirty", dirty);
  save.classList.toggle("saved", !dirty && savedOnce);
}

document.getElementById("hide").onchange = (e) => {
  hideDecided = e.target.checked;
  refresh();
};

document.getElementById("save").onclick = async () => {
  const csvText = serializeCsv();
  try {
    if (window.showSaveFilePicker) {
      if (!fileHandle)
        fileHandle = await showSaveFilePicker({
          suggestedName: "decisions.csv",
          types: [{ description: "CSV", accept: { "text/csv": [".csv"] } }],
        });
      const w = await fileHandle.createWritable();
      await w.write(csvText);
      await w.close();
    } else {
      const a = document.createElement("a");
      a.href = URL.createObjectURL(new Blob([csvText], { type: "text/csv" }));
      a.download = "decisions.csv";
      a.click();
    }
  } catch (e) {
    if (e.name === "AbortError") return;
    alert("save failed: " + e);
    return;
  }
  dirty = false;
  savedOnce = true;
  document.getElementById("savemsg").textContent =
    "saved " + new Date().toLocaleTimeString();
  refresh();
};

window.pf = { keep: clickKeep, donate: clickDonate, serializeCsv: serializeCsv,
              dec: dec, donorOf: donorOf };
build();
refresh();
</script></body></html>"""
```

Careful with the `\\u00d7`/`\\r\\n`-style escapes: in the Python source they must reach the browser as `×` / `\r\n` inside JS strings (the template is a normal — not raw — Python string, so `\\` collapses to `\`). After writing, sanity-check the generated page contains `lines.join("\r\n")`.

**Step 4: Run tests**

Run: `uv run pytest tests/test_review_page.py -v`
Expected: 7 PASS

**Step 5: Lint, then commit**

Run: `uv run ruff check src tests && uv run ruff format src tests`

```bash
git add src/photoflow/review_page.py tests/test_review_page.py
git commit -m "feat: interactive review page template (clickable keep/skip, save to CSV)"
```

---

### Task 4: Rewire `cmd_review` to use the new module

**Files:**
- Modify: `src/photoflow/review.py` (full rewrite below)

**Step 1: Replace `src/photoflow/review.py` with:**

```python
"""Review command: export interactive review.html + decisions.csv for near-dupe groups."""

from __future__ import annotations

import csv
from collections import defaultdict

from photoflow.audit import log_action
from photoflow.hashing import HAVE_PIL
from photoflow.review_page import build_payload, decision_rows, render_page, write_decisions_csv

if HAVE_PIL:
    from PIL import Image


def _read_prior(dec_path) -> dict[str, dict]:
    """Carry forward any decisions already made so regeneration never loses work."""
    prior: dict[str, dict] = {}
    if dec_path.exists():
        with open(dec_path, newline="", encoding="utf-8") as f:
            for row in csv.DictReader(f):
                if (row.get("decision") or "").strip():
                    prior[row["file_id"]] = row
    return prior


def _make_thumbs(groups, thumbs_dir) -> set[int]:
    ok: set[int] = set()
    for members in groups.values():
        for m in members:
            tp = thumbs_dir / f"{m['id']}.jpg"
            if HAVE_PIL and not tp.exists():
                try:
                    with Image.open(m["source_path"]) as im:
                        im.thumbnail((320, 320))
                        im.convert("RGB").save(tp, "JPEG", quality=80)
                except Exception:
                    pass
            if tp.exists():
                ok.add(m["id"])
    return ok


def cmd_review(conn, workdir, run_id, log_fh, args, cfg):
    rows = conn.execute(
        "SELECT * FROM files WHERE role='review' ORDER BY group_id, size DESC"
    ).fetchall()
    if not rows:
        print("nothing queued for review.")
        return
    thumbs = workdir / "thumbs"
    thumbs.mkdir(exist_ok=True)
    dec_path = workdir / "decisions.csv"
    html_path = workdir / "review.html"

    groups = defaultdict(list)
    for r in rows:
        groups[r["group_id"]].append(r)

    out_rows = decision_rows(groups, _read_prior(dec_path))
    write_decisions_csv(dec_path, out_rows)

    payload = build_payload(groups, out_rows, str(workdir.resolve()), _make_thumbs(groups, thumbs))
    html_path.write_text(render_page(payload), encoding="utf-8")

    log_action(
        conn, log_fh, run_id, 0, "review_exported", f"{len(groups)} groups, {len(rows)} files"
    )
    conn.commit()
    print(f"review.html  -> {html_path}")
    print(f"decisions.csv -> {dec_path}")
    print("Open the HTML, click keepers, Save decisions.csv, then run apply.")
```

(sqlite3.Row supports `m["key"]`, so the pure helpers work on DB rows and plain dicts alike.)

**Step 2: Run the full suite** (existing `test_review.py` carry-forward tests must still pass — they exercise the real CLI end to end)

Run: `uv run pytest`
Expected: all pass (67 baseline + 7 new = 74; exiftool-marked tests run if exiftool is on PATH)

**Step 3: Lint + format**

Run: `uv run ruff check src tests && uv run ruff format src tests`

**Step 4: Commit**

```bash
git add src/photoflow/review.py
git commit -m "feat: review command emits interactive HTML editor for decisions.csv"
```

---

### Task 5: Browser verification (Playwright MCP)

Verify the real page in a real browser: render, click, CSV byte-compatibility, localStorage persistence.

**Step 1: Build a demo workdir** — run from the worktree root:

```bash
uv run python - <<'EOF'
from pathlib import Path
from PIL import Image, ImageDraw
src = Path("demo_src"); src.mkdir(exist_ok=True)
im = Image.new("RGB", (1600, 1200), (200, 120, 40))
d = ImageDraw.Draw(im); d.ellipse((600, 400, 1000, 800), fill=(250, 220, 80))
im.save(src / "sunset_big.jpg", quality=92)
im.resize((800, 600)).save(src / "sunset_small.jpg", quality=85)
im2 = im.copy(); d2 = ImageDraw.Draw(im2); d2.rectangle((0, 0, 80, 80), fill=(0, 0, 0))
im2.resize((1024, 768)).save(src / "sunset_mid.jpg", quality=88)
EOF
uv run photoflow --workdir demo_work scan demo_src
uv run photoflow --workdir demo_work plan
uv run photoflow --workdir demo_work review
```

(Check `cli.py` for the exact `--workdir` flag spelling/position before running.) Expected: `review.html -> demo_work\review.html` with at least one group. If plan queues nothing for review (pHash distance too small/large), tweak the third image until a review group exists.

**Step 2: Verify byte-compatibility before any clicks.** Open `file:///C:/dev_projects/photo_org/.worktrees/interactive-review/demo_work/review.html` with Playwright MCP (`browser_navigate`), then `browser_evaluate`: `() => window.pf.serializeCsv()`. Compare with the bytes of `demo_work/decisions.csv` (read in binary, decode utf-8). They must be **identical** — this proves a no-op open-and-save cannot corrupt the CSV.

**Step 3: Verify click behavior.** `browser_evaluate`: `() => { pf.keep(<first file id>); return pf.serializeCsv(); }` — the returned CSV must show `keep` on that file and `skip` on the other group members. Snapshot the page: the kept card has class `keep`, others `skip`, header shows `decided 1 / N groups`.

**Step 4: Verify persistence.** Reload the page (`browser_navigate` again) and `browser_evaluate`: `() => pf.serializeCsv()` — selections from step 3 must survive (localStorage overlay).

**Step 5: Verify apply consumes it.** Write the serialized CSV from step 3 over `demo_work/decisions.csv`, then:

```bash
uv run photoflow --workdir demo_work apply --out demo_out --dry-run
```

Expected: the kept file appears as a `DRY` copy line; no errors.

**Step 6: Clean up and commit nothing** — delete `demo_src`, `demo_work`, `demo_out`. This task produces no source changes; if verification fails, fix the template in `review_page.py` (with a test where possible) before proceeding.

---

### Task 6: Docs + final check

**Files:**
- Modify: `README.md` (the review-step description: mention clicking keepers in review.html and the Save button; CSV editing still works as fallback)
- Modify: `CLAUDE.md` (module map: add `review_page.py` under pure logic — "payload + HTML/JS template for the interactive review page")

**Steps:**

1. Update both docs (keep it to a few lines each; the README's workflow section should read: open `review.html` in Chrome/Edge → click keepers → Save decisions.csv → `photoflow apply`).
2. Run: `uv run pytest` — expected all pass.
3. Run: `uv run ruff check src tests` — expected clean.
4. Commit:

```bash
git add README.md CLAUDE.md
git commit -m "docs: interactive review workflow"
```

Then use superpowers:finishing-a-development-branch to merge `feature/interactive-review` back to master.
