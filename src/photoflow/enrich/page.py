"""Pure helpers + HTML/JS template for the interactive enrich review page.

Mirrors review_page.py: the caller (enrich/review.py) hands in plain dict rows queried
from the DB, and this module returns the CSV rows + a self-contained HTML string. No I/O,
no models - testable without GPU or exiftool.

The page renders LAZILY (IntersectionObserver-batched group fill, loading="lazy" thumbnails,
content-visibility on groups) so a 40k-photo library with tens of thousands of faces + review
tags stays responsive instead of building ~57k <img> at once (which froze and OOM'd the tab).
CSV serialization reads the in-memory state objects, not the DOM, so lazy rendering is safe.

Two decision OVERLAYS are produced (kept small so the page stays light on big libraries):
  * faces.csv - one row per face shown for confirmation; naming a cluster fills `person`
    + sets decision=keep for its members; ejecting an edge-case member sets decision=skip.
  * tags.csv  - one row per REVIEW-band tag (auto tags apply from the DB by default); the
    page can also append global-blacklist rows (file_id='*', decision='reject').
"""

from __future__ import annotations

import csv
import json
from html import escape
from pathlib import Path

FACE_COLUMNS = [
    "cluster_id",
    "face_id",
    "file_id",
    "source_path",
    "cluster_prob",
    "suggested_person",
    "person",
    "decision",
]
TAG_COLUMNS = ["file_id", "tag", "source", "score", "suggestion", "decision"]


# --------------------------------------------------------------------------- apply semantics


def tag_is_applied(suggestion: str, decision: str, blacklisted: bool = False) -> bool:
    """Whether a content tag is written to the photo.

    Auto-band tags flow through unless rejected; review-band tags need an explicit keep;
    a globally blacklisted tag is never written.
    """
    if blacklisted or decision == "reject":
        return False
    if decision == "keep":
        return True
    return suggestion == "auto"


def face_is_applied(person: str, decision: str) -> bool:
    """Whether a face's person name is written: a confirmed (keep) face with a name."""
    return decision == "keep" and bool((person or "").strip())


# --------------------------------------------------------------------------- CSV row builders


def _members(clusters: dict, noise: list) -> list[tuple[object, dict]]:
    out: list[tuple[object, dict]] = []
    for cid, members in clusters.items():
        for m in members:
            out.append((cid, m))
    for m in noise:
        out.append(("", m))
    return out


def face_rows(clusters: dict, noise: list, prior: dict) -> list[dict]:
    """faces.csv rows, carrying forward person/decision by face_id (invariant #4)."""
    rows = []
    for cid, m in _members(clusters, noise):
        old = prior.get(str(m["face_id"]), {})
        rows.append(
            {
                "cluster_id": cid if cid != "" else "",
                "face_id": m["face_id"],
                "file_id": m["file_id"],
                "source_path": m["source_path"],
                "cluster_prob": round(float(m.get("cluster_prob") or 0.0), 4),
                "suggested_person": m.get("suggested_person", "") or "",
                "person": old.get("person", ""),
                "decision": old.get("decision", ""),
            }
        )
    return rows


def tag_rows(review_items: list, prior: dict) -> list[dict]:
    """tags.csv overlay rows (review-band only), carrying forward decision by (file_id, tag)."""
    rows = []
    for it in review_items:
        key = (str(it["file_id"]), it["tag"])
        old = prior.get(key, {})
        rows.append(
            {
                "file_id": it["file_id"],
                "tag": it["tag"],
                "source": it.get("source", ""),
                "score": "" if it.get("score") is None else round(float(it["score"]), 4),
                "suggestion": it.get("suggestion", "review"),
                "decision": old.get("decision", ""),
            }
        )
    return rows


def write_faces_csv(path, rows: list[dict]) -> None:
    _write_csv(path, FACE_COLUMNS, rows)


def write_tags_csv(path, rows: list[dict]) -> None:
    _write_csv(path, TAG_COLUMNS, rows)


def _write_csv(path, columns, rows) -> None:
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=columns)
        w.writeheader()
        w.writerows(rows)


# --------------------------------------------------------------------------- payload builders


def _file_uri(p: str) -> str | None:
    try:
        return Path(p).as_uri()
    except (ValueError, OSError):
        return None


def build_people_payload(
    clusters: dict,
    noise: list,
    rows: list[dict],
    persons: list[str],
    workdir_key: str,
    prob_floor: float,
) -> dict:
    """Everything the People tab JS needs. Low-probability members are flagged `edge` so the
    'needs attention' filter can surface only the marginal faces."""
    by_face = {r["face_id"]: r for r in rows}
    out_clusters = []
    for cid, members in clusters.items():
        mlist = []
        for m in members:
            r = by_face.get(m["face_id"], {})
            prob = float(m.get("cluster_prob") or 0.0)
            mlist.append(
                {
                    "face_id": m["face_id"],
                    "file_id": m["file_id"],
                    "thumb": m.get("thumb"),
                    "uri": _file_uri(m["source_path"]),
                    "path": m["source_path"],
                    "prob": round(prob, 3),
                    "edge": prob < prob_floor,
                    "suggested_person": m.get("suggested_person", "") or "",
                    "person": r.get("person", ""),
                    "decision": r.get("decision", ""),
                }
            )
        mlist.sort(key=lambda x: x["prob"], reverse=True)
        out_clusters.append({"cluster_id": cid, "size": len(mlist), "members": mlist})
    out_clusters.sort(key=lambda c: c["size"], reverse=True)

    noise_out = []
    for m in noise:
        r = by_face.get(m["face_id"], {})
        noise_out.append(
            {
                "face_id": m["face_id"],
                "file_id": m["file_id"],
                "thumb": m.get("thumb"),
                "uri": _file_uri(m["source_path"]),
                "path": m["source_path"],
                "suggested_person": m.get("suggested_person", "") or "",
                "person": r.get("person", ""),
                "decision": r.get("decision", ""),
            }
        )
    return {
        "workdir": workdir_key,
        "persons": sorted(persons),
        "clusters": out_clusters,
        "noise": noise_out,
    }


def build_tags_payload(items: list, rows: list[dict], workdir_key: str) -> dict:
    """People tab's sibling: review-band tags grouped BY TAG (one tag, many candidate
    photos) + a per-tag count summary of the auto-accepted bulk (for the global blacklist)."""
    by_key = {(str(r["file_id"]), r["tag"]): r for r in rows}
    review_groups: dict[str, dict] = {}
    auto_counts: dict[str, int] = {}
    for it in items:
        tag = it["tag"]
        if it.get("suggestion") == "review":
            g = review_groups.setdefault(tag, {"tag": tag, "count": 0, "photos": []})
            g["count"] += 1
            r = by_key.get((str(it["file_id"]), tag), {})
            g["photos"].append(
                {
                    "file_id": it["file_id"],
                    "thumb": it.get("thumb"),
                    "uri": _file_uri(it.get("source_path", "")),
                    "path": it.get("source_path", ""),
                    "score": None if it.get("score") is None else round(float(it["score"]), 3),
                    "decision": r.get("decision", ""),
                }
            )
        else:
            auto_counts[tag] = auto_counts.get(tag, 0) + 1
    review = sorted(review_groups.values(), key=lambda g: g["count"], reverse=True)
    summary = sorted(
        ({"tag": t, "count": c} for t, c in auto_counts.items()),
        key=lambda s: s["count"],
        reverse=True,
    )
    return {"workdir": workdir_key, "reviewTags": review, "autoSummary": summary}


def _assign_thumb(item: dict) -> str:
    thumb = item.get("thumb")
    if not thumb:
        return '<span class="noimg">no img</span>'
    img = f'<img loading="lazy" decoding="async" src="{escape(str(thumb))}" alt="">'
    uri = item.get("uri")
    return (
        f'<a href="{escape(str(uri))}" target="_blank" title="open original">{img}</a>'
        if uri
        else img
    )


def render_assign_review(min_sim: float, total: int, persons: list[dict]) -> str:
    """Static review for `enrich assign --dry-run`: each PROPOSED face shown under the person it
    would be assigned to (strongest match first), with its cosine score, alongside a strip of
    that person's already-named faces. No JS: generate one per --min-sim and compare to find the
    threshold just above where wrong faces start appearing. `persons` is pre-sorted by count."""
    no_refs = '<span class="lbl">(no thumbnails)</span>'
    sections = []
    for p in persons:
        refs = "".join(f'<span class="ref">{_assign_thumb(r)}</span>' for r in p["refs"])
        cands = "".join(
            f'<span class="cand">{_assign_thumb(c)}<span class="sim">{c["sim"]:.2f}</span></span>'
            for c in p["candidates"]
        )
        sections.append(
            f'<section class="person"><h2>{escape(p["name"])}'
            f'<span class="meta">{p["count"]} proposed &middot; weakest {p["weakest"]:.2f}</span></h2>'
            f'<div class="known"><span class="lbl">known</span>{refs or no_refs}</div>'
            f'<div class="cands">{cands}</div></section>'
        )
    body = (
        "".join(sections) or '<p class="lbl" style="margin:24px">No faces reach this threshold.</p>'
    )
    return (
        ASSIGN_TEMPLATE.replace("__MINSIM__", f"{min_sim:.2f}")
        .replace("__TOTAL__", str(total))
        .replace("__NPEOPLE__", str(len(persons)))
        .replace("__BODY__", body)
    )


ASSIGN_TEMPLATE = """<!doctype html>
<html><head><meta charset="utf-8"><title>enrich assign review (sim &gt;= __MINSIM__)</title>
<style>
:root{color-scheme:dark}
body{font-family:system-ui,sans-serif;background:#111;color:#ddd;margin:0}
header{position:sticky;top:0;background:#1a1a1a;border-bottom:1px solid #333;padding:10px 16px;z-index:10}
header h1{font-size:16px;margin:0}
.hint{font-size:12px;color:#789;margin:6px 0 0}
.person{border-top:1px solid #333;margin:0;padding:10px 16px}
.person h2{font-size:15px;color:#9ab;margin:4px 0 8px;display:flex;gap:10px;align-items:baseline}
.meta{font-size:12px;color:#789;font-weight:normal}
.known{display:flex;flex-wrap:wrap;gap:6px;align-items:center;background:#161c16;border:1px solid #2a4a2a;border-radius:8px;padding:6px;margin-bottom:8px}
.lbl{font-size:11px;color:#7a7;text-transform:uppercase;letter-spacing:.05em;margin-right:4px}
.cands{display:flex;flex-wrap:wrap;gap:8px}
.ref img{width:64px;height:64px;object-fit:cover;border-radius:4px;border:2px solid #3c3}
.cand{width:84px;text-align:center}
.cand img{width:80px;height:80px;object-fit:cover;border-radius:4px;border:2px solid #444}
.sim{font-size:11px;color:#9ab}
.noimg{display:inline-block;width:80px;height:80px;line-height:80px;text-align:center;color:#678;font-size:10px;background:#191919;border-radius:4px}
</style></head><body>
<header><h1>enrich assign &mdash; proposed matches at cosine &gt;= __MINSIM__</h1>
<p class="hint">__TOTAL__ faces would be assigned across __NPEOPLE__ people. Each person shows your
<b>known</b> faces (green) then the proposed matches, <b>strongest first</b>, with the cosine
score under each. Scan for faces that aren't this person; re-run with a higher
<code>--min-sim</code> just above where wrong faces start to appear.</p></header>
__BODY__
</body></html>"""


def render_page(people: dict, tags: dict) -> str:
    pdata = json.dumps(people, ensure_ascii=False).replace("</", "<\\/")
    tdata = json.dumps(tags, ensure_ascii=False).replace("</", "<\\/")
    return PAGE_TEMPLATE.replace("__PEOPLE__", pdata).replace("__TAGS__", tdata)


# PAGE_TEMPLATE is NOT a raw string: backslashes destined for the JS layer are doubled
# here (e.g. "\\r\\n" reaches the browser as \r\n). Same conventions as review_page.py.
PAGE_TEMPLATE = """<!doctype html>
<html><head><meta charset="utf-8"><title>photoflow enrich review</title>
<style>
:root{color-scheme:dark}
body{font-family:system-ui,sans-serif;background:#111;color:#ddd;margin:0}
header{position:sticky;top:0;background:#1a1a1a;border-bottom:1px solid #333;
  padding:10px 16px;display:flex;gap:14px;align-items:center;z-index:10;flex-wrap:wrap}
header h1{font-size:16px;margin:0}
.tabbtn{background:#222;border:1px solid #444;color:#bbb}
.tabbtn.active{border-color:#7a7;color:#7a7}
button{background:#2a2a2a;color:#ddd;border:1px solid #555;border-radius:6px;
  padding:6px 12px;cursor:pointer;font-size:13px}
button:hover{border-color:#888}
.save.dirty{border-color:#e6b450;color:#e6b450}
.save.saved{border-color:#5c5;color:#5c5}
.muted{font-size:12px;color:#789}
.hint{font-size:12px;color:#789;margin:8px 16px}
.pane{display:none}
.pane.active{display:block}
.cluster{border:1px solid #444;margin:14px;padding:10px;border-radius:8px}
.cluster.named{border-color:#2a4a2a;background:#161c16}
.cluster h3{margin:2px 0 8px;font-size:14px;color:#9ab;display:flex;gap:10px;align-items:center;flex-wrap:wrap}
.cluster input.name{background:#222;border:1px solid #555;color:#fff;border-radius:5px;padding:4px 8px;font-size:13px}
.faces{display:flex;flex-wrap:wrap;gap:8px}
.face{border:2px solid #333;border-radius:8px;padding:4px;width:104px;text-align:center;background:#191919}
.face.edge{border-color:#c84}
.face.eject{opacity:.35}
.face img{width:96px;height:96px;object-fit:cover;border-radius:4px;cursor:zoom-in}
.noimg{display:inline-block;width:96px;height:96px;line-height:96px;color:#678;font-size:10px;text-align:center}
.face .p{font-size:10px;color:#9ab}
.face .ej{font-size:10px;padding:1px 5px;margin-top:2px}
.face.eject .ej{border-color:#c44;color:#c44}
.flag{font-size:10px;color:#e84}
.tag{border:1px solid #444;margin:12px 16px;padding:10px;border-radius:8px}
.tag h3{margin:0 0 6px;font-size:14px;color:#9ab}
.tag .photos{display:flex;flex-wrap:wrap;gap:8px}
.tp{border:2px solid #333;border-radius:6px;padding:3px;width:92px;text-align:center;background:#191919}
.tp.keep{border-color:#3c3;background:#1c241c}
.tp.reject{opacity:.35}
.tp img{width:84px;height:84px;object-fit:cover;border-radius:3px}
.tp .s{font-size:10px;color:#9ab}
.summary{margin:12px 16px}
.chip{display:inline-block;border:1px solid #555;border-radius:12px;padding:2px 10px;margin:3px;font-size:12px;cursor:pointer}
.chip.bl{border-color:#c44;color:#c44;text-decoration:line-through}
.filterbar{margin:8px 16px;font-size:12px;color:#9ab}
/* off-screen clusters/tag-groups skip layout+paint; size is remembered after first render,
   so a 40k-photo library scrolls smoothly instead of laying out ~57k thumbnails at once */
.cluster,.tag{content-visibility:auto;contain-intrinsic-size:auto 360px}
.blnote{color:#e84;font-size:12px;margin-left:6px}
.dismiss{font-size:11px;padding:3px 8px;color:#b99;border-color:#664}
.cluster.dismissed{opacity:.5;border-color:#403030}
.cluster.dismissed .faces,.cluster.dismissed input.name{display:none}
.cluster.dismissed .dismiss{color:#7a7}
.cluster:not(.dismissed) .ignored{display:none}
.ignored{color:#a77;font-size:12px}
.sentinel{height:1px}
</style></head><body>
<header>
  <h1>photoflow enrich</h1>
  <button class="tabbtn active" id="tab-people" onclick="ui.tab('people')">People</button>
  <button class="tabbtn" id="tab-tags" onclick="ui.tab('tags')">Tags</button>
  <span style="flex:1"></span>
  <button class="save" id="save-people" onclick="ui.save('people')">Save faces.csv</button>
  <button class="save" id="save-tags" onclick="ui.save('tags')">Save tags.csv</button>
  <span id="savemsg" class="muted"></span>
</header>

<section class="pane active" id="pane-people">
  <p class="hint">Type a name on a cluster to label everyone in it at once &mdash; names you've
  used <b>autocomplete</b>, so type one like "Yancey Arrington" once and pick it next time. Faces
  flagged <span class="flag">&#9888; check</span> are low-confidence &mdash; click <b>eject</b> to
  remove a face that doesn't belong, or <b>not interested</b> to ignore a whole cluster you don't
  care to tag. Then <b>Save faces.csv</b> and run <code>photoflow enrich apply</code>.</p>
  <div class="filterbar"><label><input type="checkbox" id="attn"> show only clusters needing attention</label>
    &nbsp;<span id="people-progress"></span></div>
  <div id="clusters"></div>
  <h3 class="muted" style="margin:14px 16px 4px">unknown / unclustered faces</h3>
  <div id="noise" class="faces" style="margin:0 16px 24px"></div>
</section>

<section class="pane" id="pane-tags">
  <p class="hint">These tags were uncertain &mdash; click photos where the tag is <b>right</b> to keep
  them. Confident tags are applied automatically; click a chip below to <b>blacklist</b> a junk tag
  everywhere. Then <b>Save tags.csv</b>.</p>
  <div class="summary"><b>auto-applied tags</b> (click to blacklist globally):<div id="autosummary"></div></div>
  <div id="reviewtags"></div>
</section>

<script id="people-data" type="application/json">__PEOPLE__</script>
<script id="tags-data" type="application/json">__TAGS__</script>
<script>
"use strict";
const PEOPLE = JSON.parse(document.getElementById("people-data").textContent);
const TAGS = JSON.parse(document.getElementById("tags-data").textContent);
const LSKEY = "photoflow-enrich:" + PEOPLE.workdir;
const FACE_COLS = ["cluster_id","face_id","file_id","source_path","cluster_prob","suggested_person","person","decision"];
const TAG_COLS = ["file_id","tag","source","score","suggestion","decision"];

// face state: face_id -> {person, decision}; cluster name mirrors into members.
const faceState = {}, faceMeta = {};
for (const c of PEOPLE.clusters) for (const m of c.members) { faceState[m.face_id] = {person:m.person||"", decision:m.decision||""}; faceMeta[m.face_id]=m; }
for (const m of PEOPLE.noise) { faceState[m.face_id] = {person:m.person||"", decision:m.decision||""}; faceMeta[m.face_id]=m; }
// tag state: "file_id|tag" -> decision; plus a global blacklist set.
const tagState = {}, blacklist = new Set();
for (const g of TAGS.reviewTags) for (const p of g.photos) tagState[p.file_id+"|"+g.tag] = p.decision||"";

let dirty = {people:false, tags:false}, saved = {people:false, tags:false};
let handles = {people:null, tags:null}, attnOnly=false, storageWarned=false;

try {
  const s = JSON.parse(localStorage.getItem(LSKEY) || "null");
  if (s) {
    for (const [k,v] of Object.entries(s.faceState||{})) if (k in faceState) faceState[k]=v;
    for (const [k,v] of Object.entries(s.tagState||{})) if (k in tagState) tagState[k]=v;
    for (const t of (s.blacklist||[])) blacklist.add(t);
    if (s.faceState) dirty.people = true;
    if (s.tagState || s.blacklist) dirty.tags = true;
    if (dirty.people || dirty.tags) document.getElementById("savemsg").textContent = "restored unsaved selections";
  }
} catch (e) {}

function esc(s){const d=document.createElement("span");d.textContent=s==null?"":String(s);return d.innerHTML.replace(/"/g,"&quot;");}
function thumbHtml(m){
  if(!m.thumb) return '<div class="noimg">no img</div>';
  const img='<img loading="lazy" decoding="async" src="'+esc(m.thumb)+'" alt="">';
  return m.uri? '<a href="'+esc(m.uri)+'" target="_blank" title="open original">'+img+'</a>' : img;
}

function persist(){
  try { localStorage.setItem(LSKEY, JSON.stringify({faceState, tagState, blacklist:[...blacklist]})); }
  catch(e){ if(!storageWarned){storageWarned=true; document.getElementById("savemsg").textContent="selections won't survive closing the tab (storage blocked)";} }
}

// ---------------- lazy rendering (keeps huge libraries responsive) ----------------
// Append groups in small batches as a trailing sentinel nears the viewport, so a 40k-photo
// library never builds ~57k <img> at once (which froze the tab and exhausted memory). Paired
// with loading="lazy" thumbnails + content-visibility on groups, off-screen content is cheap.
let peopleRendered=false, tagsRendered=false;
function infinite(root, items, makeHtml, batch){
  const sentinel=document.createElement("div"); sentinel.className="sentinel"; root.appendChild(sentinel);
  let i=0, scheduled=false;
  const io=new IntersectionObserver(es=>{ if(es.some(e=>e.isIntersecting)) schedule(); }, {rootMargin:"1400px 0px"});
  function schedule(){ if(scheduled || i>=items.length) return; scheduled=true; requestAnimationFrame(()=>{ scheduled=false; pump(); }); }
  function pump(){
    if(i>=items.length){ io.disconnect(); sentinel.remove(); return; }
    // Build ONE batch then yield: lets layout (incl. content-visibility real heights) settle
    // before deciding if the viewport needs more, so a huge first group stops the fill and the
    // main thread never blocks. IntersectionObserver resumes the fill as the sentinel scrolls in.
    if(sentinel.getBoundingClientRect().top < innerHeight+1400){
      const end=Math.min(i+batch, items.length); let html="";
      for(; i<end; i++) html+=makeHtml(items[i], i);
      sentinel.insertAdjacentHTML("beforebegin", html);
      schedule();
    }
  }
  io.observe(sentinel); pump();
}

// ---------------- People ----------------
// Known names (DB persons + names carried in from faces.csv + everything typed this session)
// feed the <datalist>, so you type a name like "Yancey Arrington" once and pick it thereafter
// (the browser type-ahead is case-insensitive and narrows as you type).
const personOptions = (PEOPLE.persons || []).slice();
const knownLower = new Set(personOptions.map(n=>n.toLowerCase()));
function datalistHtml(){ return '<datalist id="persons">'+personOptions.map(p=>'<option value="'+esc(p)+'"></option>').join("")+'</datalist>'; }
function addPersonOption(name){ name=(name||"").trim(); if(!name) return; const k=name.toLowerCase(); if(knownLower.has(k)) return;
  knownLower.add(k); personOptions.push(name);
  const dl=document.getElementById("persons"); if(dl) dl.insertAdjacentHTML("beforeend",'<option value="'+esc(name)+'"></option>'); }
// seed from names already assigned (carried in via faces.csv or restored from localStorage),
// so suggestions survive a reload, not just live typing (datalist isn't built yet -> array only)
for(const k in faceState) addPersonOption(faceState[k].person);

function clusterNamed(c){ return c.members.some(m=>faceState[m.face_id].decision==="keep" && faceState[m.face_id].person); }
function clusterDismissed(c){ return c.members.length>0 && c.members.every(m=>faceState[m.face_id].decision==="skip"); }
function clusterAttn(c){ return !clusterDismissed(c) && (c.members.some(m=>m.edge) || !clusterNamed(c)); }
function faceHtml(m){ const st=faceState[m.face_id];
  return '<div class="face'+(m.edge?" edge":"")+(st.decision==="skip"?" eject":"")+'" data-fid="'+m.face_id+'">'+thumbHtml(m)
    +'<div class="p">'+(m.edge?'<span class="flag">&#9888;</span> ':"")+Math.round(m.prob*100)+'%</div>'
    +'<button class="ej" type="button">'+(st.decision==="skip"?"undo":"eject")+'</button></div>'; }
function noiseHtml(m){ const st=faceState[m.face_id];
  return '<div class="face" data-fid="'+m.face_id+'">'+thumbHtml(m)
    +'<input class="name" list="persons" style="width:88px" placeholder="name…" value="'+esc(st.person)+'"></div>'; }
function clusterHtml(c){
  const cur=(c.members.map(m=>faceState[m.face_id].person).find(Boolean))||"";
  const dism=clusterDismissed(c);
  return '<div class="cluster'+(clusterNamed(c)?" named":"")+(dism?" dismissed":"")+(clusterAttn(c)?" needs-attn":"")+'" data-cid="'+c.cluster_id+'">'
    +'<h3>cluster '+c.cluster_id+' &middot; '+c.size+' faces '
    +'<input class="name" list="persons" placeholder="name this person…" value="'+esc(cur)+'">'
    +'<button class="dismiss" type="button">'+(dism?"undo":"not interested")+'</button>'
    +'<span class="ignored">ignored</span></h3>'
    +'<div class="faces">'+c.members.map(faceHtml).join("")+'</div></div>'; }
function peopleClusters(){ return attnOnly ? PEOPLE.clusters.filter(clusterAttn) : PEOPLE.clusters; }
function fillClusters(){
  const root=document.getElementById("clusters");
  root.innerHTML=datalistHtml();
  infinite(root, peopleClusters(), clusterHtml, 12);
}
function renderPeople(){
  if(peopleRendered) return; peopleRendered=true;
  fillClusters();
  infinite(document.getElementById("noise"), PEOPLE.noise, noiseHtml, 80);
  updateProgress();
}
function updateProgress(){ let named=0, dism=0; for(const c of PEOPLE.clusters){ if(clusterNamed(c)) named++; else if(clusterDismissed(c)) dism++; }
  document.getElementById("people-progress").textContent="named "+named+" · ignored "+dism+" / "+PEOPLE.clusters.length+" clusters"; }

// ---------------- Tags ----------------
function renderAutoSummary(){
  document.getElementById("autosummary").innerHTML=TAGS.autoSummary.map(s=>
    '<span class="chip'+(blacklist.has(s.tag)?" bl":"")+'" data-tag="'+esc(s.tag)+'">'+esc(s.tag)+' ('+s.count+')</span>').join(""); }
function tagPhotoHtml(p,tag){ const d=tagState[p.file_id+"|"+tag]||"";
  return '<div class="tp'+(d==="keep"?" keep":"")+'" data-fid="'+p.file_id+'">'
    +(p.thumb?'<img loading="lazy" decoding="async" src="'+esc(p.thumb)+'">':'<div class="s" style="height:84px;line-height:84px">no img</div>')
    +'<div class="s">'+(p.score!=null?Math.round(p.score*100)+"%":"")+'</div></div>'; }
function tagGroupHtml(g){
  return '<div class="tag" data-tag="'+esc(g.tag)+'"><h3>'+esc(g.tag)+' &middot; '+g.count+' photo(s)'
    +'<span class="blnote">'+(blacklist.has(g.tag)?"blacklisted":"")+'</span></h3>'
    +'<div class="photos">'+g.photos.map(p=>tagPhotoHtml(p,g.tag)).join("")+'</div></div>'; }
function renderTags(){
  if(tagsRendered) return; tagsRendered=true;
  renderAutoSummary();
  // batch=1: the first groups are the largest (sorted by count), so render one at a time and
  // let the height check stop early instead of building several huge groups up front.
  infinite(document.getElementById("reviewtags"), TAGS.reviewTags, tagGroupHtml, 1);
  mark("tags");
}

// ---------------- CSV serialize ----------------
function csvField(v){ v=String(v==null?"":v); return /[",\\r\\n]/.test(v)?'"'+v.replace(/"/g,'""')+'"':v; }
function facesCsv(){
  const lines=[FACE_COLS.join(",")];
  const emit=(cid,m)=>{ const st=faceState[m.face_id];
    lines.push([cid,m.face_id,m.file_id,m.path,(m.prob!=null?m.prob:""),m.suggested_person||"",st.person||"",st.decision||""].map(csvField).join(",")); };
  for(const c of PEOPLE.clusters) for(const m of c.members) emit(c.cluster_id,m);
  for(const m of PEOPLE.noise) emit("",m);
  return lines.join("\\r\\n")+"\\r\\n";
}
function tagsCsv(){
  const lines=[TAG_COLS.join(",")];
  for(const g of TAGS.reviewTags) for(const p of g.photos){ const d=tagState[p.file_id+"|"+g.tag]||"";
    lines.push([p.file_id,g.tag,"",(p.score!=null?p.score:""),"review",d].map(csvField).join(",")); }
  // global blacklist rows: file_id='*' reject => apply drops this tag everywhere
  for(const t of blacklist) lines.push(["*",t,"","","auto","reject"].map(csvField).join(","));
  return lines.join("\\r\\n")+"\\r\\n";
}

// ---------------- save / tabs ----------------
function mark(which){
  const b=document.getElementById("save-"+which);
  b.classList.toggle("dirty",dirty[which]); b.classList.toggle("saved",!dirty[which]&&saved[which]);
}
async function save(which){
  const text = which==="people"?facesCsv():tagsCsv();
  const name = which==="people"?"faces.csv":"tags.csv";
  try{
    if(window.showSaveFilePicker){
      if(!handles[which]) handles[which]=await showSaveFilePicker({suggestedName:name,types:[{description:"CSV",accept:{"text/csv":[".csv"]}}]});
      const w=await handles[which].createWritable(); await w.write(text); await w.close();
    } else { const a=document.createElement("a"); a.href=URL.createObjectURL(new Blob([text],{type:"text/csv"})); a.download=name; a.click(); URL.revokeObjectURL(a.href); }
  } catch(e){ if(e.name==="AbortError") return; handles[which]=null; alert("save failed: "+e); return; }
  dirty[which]=false; saved[which]=true;
  try{ if(!dirty.people&&!dirty.tags) localStorage.removeItem(LSKEY); }catch(e){}
  document.getElementById("savemsg").textContent="saved "+name+" "+new Date().toLocaleTimeString();
  which==="people"?renderPeople():renderTags();
}
function tab(which){
  for(const t of ["people","tags"]){ document.getElementById("pane-"+t).classList.toggle("active",t===which); document.getElementById("tab-"+t).classList.toggle("active",t===which); }
  scrollTo(0,0);  // each pane starts at the top so the lazy fill is predictable
  if(which==="tags") renderTags(); else renderPeople();
}
// Event delegation: one listener per container handles every (lazily-built) child, so naming,
// ejecting, tag-keep and blacklist work no matter when a node is materialized by scrolling.
document.getElementById("clusters").addEventListener("click",e=>{
  // "not interested": skip every face in the cluster (won't be tagged) + collapse it away
  const dz=e.target.closest(".dismiss");
  if(dz){
    const cl=dz.closest(".cluster"), c=PEOPLE.clusters.find(x=>String(x.cluster_id)===cl.dataset.cid); if(!c) return;
    const on=!cl.classList.contains("dismissed");
    for(const m of c.members){ const st=faceState[m.face_id]; st.decision=on?"skip":""; if(on) st.person=""; }
    if(on){ const inp=cl.querySelector("input.name"); if(inp) inp.value=""; }
    cl.classList.toggle("dismissed",on); cl.classList.toggle("named",clusterNamed(c)); cl.classList.toggle("needs-attn",clusterAttn(c));
    dz.textContent=on?"undo":"not interested";
    dirty.people=true; persist(); updateProgress(); mark("people"); return;
  }
  const b=e.target.closest(".ej"); if(!b) return;
  const face=b.closest(".face"), st=faceState[face.dataset.fid];
  st.decision = st.decision==="skip"?"keep":"skip"; dirty.people=true; persist();
  const ej=st.decision==="skip"; face.classList.toggle("eject",ej); b.textContent=ej?"undo":"eject"; mark("people");
});
document.getElementById("clusters").addEventListener("change",e=>{
  const inp=e.target.closest("input.name"); if(!inp) return;
  const cl=inp.closest(".cluster"), c=PEOPLE.clusters.find(x=>String(x.cluster_id)===cl.dataset.cid); if(!c) return;
  const val=inp.value.trim();
  for(const m of c.members){ const st=faceState[m.face_id];
    if(val){ st.person=val; if(st.decision!=="skip") st.decision="keep"; }
    else { st.person=""; if(st.decision==="keep") st.decision=""; } }
  dirty.people=true; persist(); addPersonOption(val);
  cl.classList.toggle("named",clusterNamed(c)); cl.classList.toggle("dismissed",clusterDismissed(c)); cl.classList.toggle("needs-attn",clusterAttn(c));
  updateProgress(); mark("people");
});
document.getElementById("noise").addEventListener("change",e=>{
  const inp=e.target.closest("input.name"); if(!inp) return;
  const st=faceState[inp.closest(".face").dataset.fid], val=inp.value.trim();
  st.person=val; st.decision=val?"keep":""; dirty.people=true; persist(); addPersonOption(val); mark("people");
});
document.getElementById("reviewtags").addEventListener("click",e=>{
  const tp=e.target.closest(".tp"); if(!tp) return;
  const k=tp.dataset.fid+"|"+tp.closest(".tag").dataset.tag;
  tagState[k]=tagState[k]==="keep"?"":"keep"; dirty.tags=true; persist();
  tp.classList.toggle("keep",tagState[k]==="keep"); mark("tags");
});
document.getElementById("autosummary").addEventListener("click",e=>{
  const chip=e.target.closest(".chip"); if(!chip) return;
  const tag=chip.dataset.tag; if(blacklist.has(tag)) blacklist.delete(tag); else blacklist.add(tag);
  dirty.tags=true; persist(); chip.classList.toggle("bl",blacklist.has(tag));
  for(const el of document.querySelectorAll("#reviewtags .tag")) if(el.dataset.tag===tag){ const n=el.querySelector(".blnote"); if(n) n.textContent=blacklist.has(tag)?"blacklisted":""; break; }
  mark("tags");
});
document.getElementById("attn").onchange=e=>{ attnOnly=e.target.checked; fillClusters(); };
window.ui={tab, save};
renderPeople(); // People is the active tab; Tags renders lazily on first open
</script></body></html>"""
