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
                    # already copied in an earlier round (its 'keep' is carried forward);
                    # rows without a status column (fixtures/older callers) are not flagged
                    "inLibrary": ("status" in m.keys() and m["status"] == "copied"),
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


def render_page(payload: dict) -> str:
    data = json.dumps(payload, ensure_ascii=False).replace("</", "<\\/")
    return PAGE_TEMPLATE.replace("__DATA__", data)


# PAGE_TEMPLATE is NOT a raw string: backslashes destined for the JS layer must be
# doubled here (e.g. "\\r\\n" in this source reaches the browser as \r\n).
# In-page refresh() is O(files) per click — fine for hundreds of review items,
# revisit if review queues hit thousands.
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
.g.cur{border-color:#5af;box-shadow:0 0 0 1px #5af}
.g h3{margin:2px 0 8px;font-size:14px;color:#9ab;display:flex;gap:10px;align-items:center;
  height:26px}
.acceptbtn{font-size:12px;padding:3px 10px}
.g.cur .acceptbtn{border-color:#5af}
.cards{display:flex;flex-wrap:wrap;gap:10px}
.f{border:2px solid #333;border-radius:8px;padding:8px;width:280px;box-sizing:border-box;
  text-align:center;background:#191919;position:relative}
.num{position:absolute;top:4px;left:6px;font-size:11px;color:#789;background:#111a;
  border-radius:4px;padding:0 5px}
.thumb{height:220px;display:flex;align-items:center;justify-content:center}
.f.suggested{border-style:dashed;border-color:#7a7}
.f.keep{border-style:solid;border-color:#3c3;background:#1c241c}
.f.skip{opacity:.45}
.f img{max-height:220px;max-width:264px;border-radius:4px;object-fit:contain}
.f a img{cursor:zoom-in}
.stats{font-size:12px;margin:6px 0;color:#bcd;white-space:nowrap;overflow:hidden;
  text-overflow:ellipsis}
.hl{color:#6e6;font-weight:600}
.badge{display:inline-block;padding:0 5px;border-radius:4px;font-size:11px;
  background:#444;color:#fff;margin-left:4px}
.badge.raw{background:#85a}
.badge.video{background:#58a}
.badge.lib{background:#2a4a2a;color:#8d8}
.path{font-size:11px;color:#789;word-break:break-all;max-width:264px;margin:4px 0;
  line-height:1.3em;height:3.9em;overflow:hidden;display:-webkit-box;
  -webkit-line-clamp:3;-webkit-box-orient:vertical}
.meta{font-size:11px;color:#9ab}
.f .meta{white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
kbd{font:11px ui-monospace,monospace;background:#2a2a2a;border:1px solid #555;
  border-radius:3px;padding:0 4px}
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
<p class="hint"><b>Keyboard:</b> <kbd>Enter</kbd>/<kbd>Space</kbd> keep the suggested
photo of the highlighted group and move on · <kbd>1</kbd>–<kbd>9</kbd> keep that card ·
<kbd>↑</kbd>/<kbd>↓</kbd> (or <kbd>k</kbd>/<kbd>j</kbd>) move · <kbd>h</kbd> hide decided ·
<kbd>s</kbd> save. Mouse: click <b>Keep</b> on the photo(s) to keep in each group — the rest
auto-skip — or <b>✓ keep suggested</b> in the group header. Click a keeper again to undo.
Untouched groups stay on hold. A member tagged <b>in library</b> was already kept in an
earlier round and a new look-alike has turned up next to it: click its <b>Keep</b> to
confirm (new one skips), or <b>Keep</b> the new one too. Click a thumbnail to open the
original full size. Selections survive closing the tab; <b>Save decisions.csv</b> writes
them back into your workdir (overwrite decisions.csv when the picker asks), then run
<code>photoflow apply</code>.</p>
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
let saving = false, storageWarned = false;
let cur = null;  // gid of the keyboard cursor (highlighted group)

function norm(d) {  // decision vocabulary: only keep/skip (any case) survive
  d = String(d == null ? "" : d).toLowerCase();
  return d === "keep" || d === "skip" ? d : "";
}

for (const g of DATA.groups) {
  byGid[g.gid] = g;
  donorOf[g.gid] = null;
  for (const f of g.files) {
    groupOf[f.id] = g.gid;
    dec[f.id] = norm(f.decision);
    if (dec[f.id] === "keep" && f.merge) donorOf[g.gid] = Number(f.merge);
  }
}
try {  // localStorage overlays the CSV baseline (crash insurance)
  const saved = JSON.parse(localStorage.getItem(LSKEY) || "null");
  if (saved) {
    let restored = false;
    for (const [id, d] of Object.entries(saved.dec || {}))
      if (id in dec) { dec[id] = norm(d); restored = true; }
    for (const [gid, d] of Object.entries(saved.donorOf || {}))
      if (gid in donorOf) { donorOf[gid] = d; restored = true; }
    if (restored) {  // overlay only ever holds unsaved work -> flag it
      dirty = true;
      document.getElementById("savemsg").textContent =
        "restored unsaved selections from last session";
    }
  }
} catch (e) { /* corrupt storage: fall back to CSV state */ }

function esc(s) {
  const d = document.createElement("span");
  d.textContent = s == null ? "" : String(s);
  return d.innerHTML.replace(/"/g, "&quot;");  // safe in double-quoted attributes too
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
    g.files.forEach((f, i) => {
      const img = f.thumb
        ? (f.uri
            ? '<a href="' + esc(f.uri) + '" target="_blank">' +
              '<img src="' + esc(f.thumb) + '" alt="" title="open full size"></a>'
            : '<img src="' + esc(f.thumb) + '" alt="">')
        : '<div class="meta">(no preview)</div>';
      const badge = (f.kind === "raw" ? '<span class="badge raw">RAW</span>'
        : f.kind === "video" ? '<span class="badge video">VIDEO</span>' : "") +
        (f.inLibrary ? '<span class="badge lib" ' +
          'title="already copied into the library in an earlier round">in library</span>' : "");
      cards += '<div class="f" id="f' + f.id + '">' +
        '<span class="num">' + (i + 1) + '</span>' +
        '<div class="thumb">' + img + '</div>' +
        '<div class="stats"><span class="' + (f.bestRes ? "hl" : "") + '">' +
        esc(f.w || "?") + "\\u00d7" + esc(f.h || "?") + " \\u00b7 " + fmtMp(f) +
        '</span> \\u00b7 <span class="' + (f.bestSize ? "hl" : "") + '">' +
        fmtSize(f.size) + "</span> \\u00b7 " +
        esc((f.ext || "").replace(/^\\./, "").toUpperCase()) + badge +
        '</div><div class="meta">' + esc(f.camera || "unknown camera") + " \\u00b7 " +
        esc(f.date || "no date") + '</div><div class="path" title="' + esc(f.path) + '">' +
        esc(f.path) + "</div>" +
        '<div class="actions">' +
        '<button class="keepbtn" onclick="pf.keep(' + f.id + ')">Keep</button>' +
        '<button class="donate" onclick="pf.donate(' + f.id + ')" ' +
        'title="copy missing metadata (GPS, dates) from this file into the keeper">' +
        "\\u2192 donate metadata</button></div>" +
        '<div class="state"></div></div>';
    });
    div.innerHTML = "<h3>group " + g.gid + " \\u00b7 " + g.files.length + " files " +
      '<button class="acceptbtn" onclick="pf.accept(' + g.gid + ')" ' +
      'title="keep the suggested photo, skip the rest (Enter)">\\u2713 keep suggested</button>' +
      '</h3><div class="cards">' + cards + "</div>";
    main.appendChild(div);
  }
}

function groupDecided(g) { return g.files.every((f) => dec[f.id]); }
function groupVisible(g) { return !(hideDecided && groupDecided(g)); }
function nextVisible(fromGid, dir) {  // next visible group in DATA order, wrapping; null if none
  const n = DATA.groups.length;
  let i = fromGid == null ? (dir > 0 ? -1 : n) : DATA.groups.findIndex((g) => g.gid === fromGid);
  for (let k = 0; k < n; k++) {
    i = (i + dir + n) % n;
    if (groupVisible(DATA.groups[i])) return DATA.groups[i].gid;
  }
  return null;
}
function setCursor(gid, scroll) {
  cur = gid;
  refresh();
  if (scroll && cur != null)
    document.getElementById("g" + cur).scrollIntoView({ block: "nearest" });
}
function acceptSuggested(gid) {  // keep the suggested member (rest skip), then advance
  const g = byGid[gid];
  if (!groupDecided(g)) {
    const s = g.files.find((f) => f.suggested) || g.files[0];
    clickKeep(s.id);  // handles both fresh groups and a carried-forward keeper (confirm)
  }
  setCursor(nextVisible(gid, +1), true);
}

function clickKeep(id) {
  const gid = groupOf[id], mem = byGid[gid].files;
  cur = gid;  // Enter continues from the group you last touched
  if (dec[id] === "keep" && mem.some((f) => !dec[f.id])) {
    // keeper carried forward from an earlier round, group still has undecided
    // members: re-clicking confirms it -> the rest auto-skip (same rule as a fresh click)
    mem.forEach((f) => { if (!dec[f.id]) dec[f.id] = "skip"; });
  } else if (dec[id] === "keep") {
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
  try {
    localStorage.setItem(LSKEY, JSON.stringify({ dec: dec, donorOf: donorOf }));
  } catch (e) {  // quota/private mode: clicks still work, just no crash insurance
    if (!storageWarned) {
      storageWarned = true;
      document.getElementById("savemsg").textContent =
        "selections won't survive closing the tab (storage blocked)";
    }
  }
}

function refresh() {
  let decided = 0;
  if (cur == null || !groupVisible(byGid[cur])) cur = nextVisible(cur, +1);  // skip hidden groups
  for (const g of DATA.groups) {
    const hasKeep = g.files.some((f) => dec[f.id] === "keep");
    const isDecided = groupDecided(g);
    if (isDecided) decided++;
    const gdiv = document.getElementById("g" + g.gid);
    gdiv.classList.toggle("decided", isDecided);
    gdiv.classList.toggle("cur", g.gid === cur);
    gdiv.style.display = groupVisible(g) ? "" : "none";
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
        : d === "keep" ? (f.inLibrary ? "KEEP \\u00b7 in library" : "KEEP")
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

document.addEventListener("keydown", (e) => {
  if (e.ctrlKey || e.metaKey || e.altKey) return;
  const tag = e.target && e.target.tagName;
  if (tag === "INPUT" || tag === "TEXTAREA" || tag === "SELECT") return;
  if (tag === "BUTTON") e.target.blur();  // don't also re-fire the focused button
  const k = e.key;
  if (k === "Enter" || k === " ") {
    e.preventDefault();
    if (cur != null) acceptSuggested(cur);
  } else if (k === "ArrowDown" || k === "j") {
    e.preventDefault();
    setCursor(nextVisible(cur, +1), true);
  } else if (k === "ArrowUp" || k === "k") {
    e.preventDefault();
    setCursor(nextVisible(cur, -1), true);
  } else if (/^[1-9]$/.test(k)) {
    if (cur == null) return;
    const f = byGid[cur].files[Number(k) - 1];
    if (f) { clickKeep(f.id); setCursor(cur, true); }
  } else if (k === "h") {
    const cb = document.getElementById("hide");
    cb.checked = !cb.checked;
    cb.onchange({ target: cb });
    setCursor(cur, true);
  } else if (k === "s") {
    e.preventDefault();
    document.getElementById("save").click();
  }
});

document.getElementById("save").onclick = async () => {
  if (saving) return;
  saving = true;
  try {
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
        URL.revokeObjectURL(a.href);
      }
    } catch (e) {
      if (e.name === "AbortError") return;
      fileHandle = null;  // stale/revoked handle: re-prompt the picker next click
      alert("save failed: " + e);
      return;
    }
    dirty = false;
    savedOnce = true;
    try { localStorage.removeItem(LSKEY); } catch (e) {}  // saved state lives in the CSV now
    document.getElementById("savemsg").textContent =
      "saved " + new Date().toLocaleTimeString();
    refresh();
  } finally {
    saving = false;
  }
};

window.pf = { keep: clickKeep, donate: clickDonate, accept: acceptSuggested,
              cur: () => cur, serializeCsv: serializeCsv, dec: dec, donorOf: donorOf };
build();
refresh();
</script></body></html>"""
