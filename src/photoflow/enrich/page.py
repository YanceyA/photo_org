"""Pure helpers + HTML/JS template for the interactive enrich review page.

Mirrors review_page.py: the caller (enrich/review.py) hands in plain dict rows queried
from the DB, and this module returns the CSV rows + a self-contained HTML string. No I/O,
no models - testable without GPU or exiftool.

Two decision OVERLAYS are produced (kept small so the page stays light on big libraries):
  * faces.csv - one row per face shown for confirmation; naming a cluster fills `person`
    + sets decision=keep for its members; ejecting an edge-case member sets decision=skip.
  * tags.csv  - one row per REVIEW-band tag (auto tags apply from the DB by default); the
    page can also append global-blacklist rows (file_id='*', decision='reject').
"""

from __future__ import annotations

import csv
import json
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
  <p class="hint">Type a name on a cluster to label everyone in it at once. Faces flagged
  <span class="flag">&#9888; check</span> are low-confidence &mdash; click <b>eject</b> to remove
  a face that doesn't belong. Then <b>Save faces.csv</b> and run <code>photoflow enrich apply</code>.</p>
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
  const img='<img src="'+esc(m.thumb)+'" alt="">';
  return m.uri? '<a href="'+esc(m.uri)+'" target="_blank" title="open original">'+img+'</a>' : img;
}

function persist(){
  try { localStorage.setItem(LSKEY, JSON.stringify({faceState, tagState, blacklist:[...blacklist]})); }
  catch(e){ if(!storageWarned){storageWarned=true; document.getElementById("savemsg").textContent="selections won't survive closing the tab (storage blocked)";} }
}

// ---------------- People ----------------
function clusterNamed(c){ return c.members.some(m=>faceState[m.face_id].decision==="keep" && faceState[m.face_id].person); }
function nameCluster(cid,val){
  const c = PEOPLE.clusters.find(x=>x.cluster_id===cid); if(!c) return;
  val = val.trim();
  for(const m of c.members){ const st=faceState[m.face_id];
    if(val){ st.person=val; if(st.decision!=="skip") st.decision="keep"; }
    else { st.person=""; if(st.decision==="keep") st.decision=""; }
  }
  dirty.people=true; persist(); renderPeople();
}
function ejectFace(fid){ const st=faceState[fid]; st.decision = st.decision==="skip"?"keep":"skip"; dirty.people=true; persist(); renderPeople(); }
function assignNoise(fid,val){ const st=faceState[fid]; val=val.trim(); st.person=val; st.decision=val?"keep":""; dirty.people=true; persist(); renderPeople(); }

function renderPeople(){
  const root=document.getElementById("clusters"); root.innerHTML="";
  const dl='<datalist id="persons">'+PEOPLE.persons.map(p=>'<option value="'+esc(p)+'">').join("")+'</datalist>';
  let named=0;
  for(const c of PEOPLE.clusters){
    const isNamed=clusterNamed(c); if(isNamed) named++;
    const needsAttn = c.members.some(m=>m.edge) || !isNamed;
    if(attnOnly && !needsAttn) continue;
    const cur = (c.members.map(m=>faceState[m.face_id].person).find(Boolean))||"";
    let faces="";
    for(const m of c.members){ const st=faceState[m.face_id];
      faces+='<div class="face'+(m.edge?" edge":"")+(st.decision==="skip"?" eject":"")+'">'+thumbHtml(m)
        +'<div class="p">'+(m.edge?'<span class="flag">&#9888;</span> ':"")+Math.round(m.prob*100)+'%</div>'
        +'<button class="ej" onclick="pf.eject('+m.face_id+')">'+(st.decision==="skip"?"undo":"eject")+'</button></div>';
    }
    const div=document.createElement("div"); div.className="cluster"+(isNamed?" named":"");
    div.innerHTML='<h3>cluster '+c.cluster_id+' &middot; '+c.size+' faces '
      +'<input class="name" list="persons" placeholder="name this person…" value="'+esc(cur)+'" '
      +'onchange="pf.name('+c.cluster_id+',this.value)"></h3><div class="faces">'+faces+'</div>';
    root.appendChild(div);
  }
  root.insertAdjacentHTML("beforeend",dl);
  // noise pool
  const nz=document.getElementById("noise"); nz.innerHTML="";
  for(const m of PEOPLE.noise){ const st=faceState[m.face_id];
    nz.insertAdjacentHTML("beforeend",'<div class="face">'+thumbHtml(m)
      +'<input class="name" list="persons" style="width:88px" placeholder="name…" value="'+esc(st.person)+'" '
      +'onchange="pf.assign('+m.face_id+',this.value)"></div>');
  }
  document.getElementById("people-progress").textContent="named "+named+" / "+PEOPLE.clusters.length+" clusters";
  mark("people");
}

// ---------------- Tags ----------------
function toggleTagPhoto(fid,tag){ const k=fid+"|"+tag; tagState[k]=tagState[k]==="keep"?"":"keep"; dirty.tags=true; persist(); renderTags(); }
function toggleBlacklist(tag){ if(blacklist.has(tag)) blacklist.delete(tag); else blacklist.add(tag); dirty.tags=true; persist(); renderTags(); }
function renderTags(){
  const sum=document.getElementById("autosummary"); sum.innerHTML="";
  for(const s of TAGS.autoSummary)
    sum.insertAdjacentHTML("beforeend",'<span class="chip'+(blacklist.has(s.tag)?" bl":"")+'" onclick="pf.bl('+JSON.stringify(s.tag)+')">'+esc(s.tag)+' ('+s.count+')</span>');
  const root=document.getElementById("reviewtags"); root.innerHTML="";
  for(const g of TAGS.reviewTags){
    let photos="";
    for(const p of g.photos){ const d=tagState[p.file_id+"|"+g.tag]||"";
      photos+='<div class="tp '+(d==="keep"?"keep":"")+'" onclick="pf.tp('+p.file_id+','+JSON.stringify(g.tag)+')">'
        +(p.thumb?'<img src="'+esc(p.thumb)+'">':'<div class="s" style="height:84px;line-height:84px">no img</div>')
        +'<div class="s">'+(p.score!=null?Math.round(p.score*100)+"%":"")+'</div></div>';
    }
    root.insertAdjacentHTML("beforeend",'<div class="tag"><h3>'+esc(g.tag)+' &middot; '+g.count+' photo(s)'
      +(blacklist.has(g.tag)?' <span class="flag">blacklisted</span>':"")+'</h3><div class="photos">'+photos+'</div></div>');
  }
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
}
document.getElementById("attn").onchange=(e)=>{attnOnly=e.target.checked; renderPeople();};
window.pf={name:nameCluster, eject:ejectFace, assign:assignNoise, tp:toggleTagPhoto, bl:toggleBlacklist, facesCsv, tagsCsv};
window.ui={tab, save};
renderPeople(); renderTags();
</script></body></html>"""
