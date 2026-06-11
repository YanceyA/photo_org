#!/usr/bin/env python3
"""
photoflow.py - incremental, non-destructive photo library organizer.

Workflow:
  python photoflow.py scan <SRC> [SRC ...]   # fingerprint sources into the manifest
  python photoflow.py plan                   # resolve dates, group dupes, queue reviews
  python photoflow.py review                 # build review.html + decisions.csv
  python photoflow.py apply --out <DIR>      # copy keepers, write XMP, log every action
  python photoflow.py status                 # summary of the manifest

All state lives in --workdir (default ./photoflow_work). Source files are NEVER
modified or deleted. Re-running scan on new folders is safe and incremental:
anything whose content already exists in the manifest is marked a duplicate.

Dependencies: Python 3.11+, exiftool on PATH.
Optional (for near-dupe review flagging + thumbnails): Pillow, ImageHash, pillow-heif.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import html
import json
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import tempfile
from collections import defaultdict
from datetime import datetime, timedelta
from pathlib import Path

# ---------------------------------------------------------------- optional deps
try:
    from PIL import Image
    HAVE_PIL = True
except ImportError:
    HAVE_PIL = False

try:
    import imagehash
    HAVE_IMAGEHASH = HAVE_PIL
except ImportError:
    HAVE_IMAGEHASH = False

try:
    import pillow_heif
    pillow_heif.register_heif_opener()
    HAVE_HEIF = True
except ImportError:
    HAVE_HEIF = False

# ---------------------------------------------------------------- configuration
IMAGE_EXT = {".jpg", ".jpeg", ".png", ".heic", ".heif", ".tif", ".tiff",
             ".bmp", ".gif", ".webp"}
RAW_EXT = {".cr2", ".cr3", ".nef", ".arw", ".dng", ".orf", ".rw2", ".raf",
           ".pef", ".srw", ".x3f"}
VIDEO_EXT = {".mov", ".mp4", ".m4v", ".avi", ".mts", ".m2ts", ".3gp",
             ".wmv", ".mpg", ".mpeg"}
SIDECAR_EXT = {".xmp", ".aae", ".thm"}

NEAR_DUPE_THRESHOLD = 5      # max pHash hamming distance to flag for review
BURST_WINDOW_S = 10          # frames within this window + same camera = burst
MIN_YEAR, MAX_YEAR = 1990, datetime.now().year + 1
SLUG_MAX = 40
EXIFTOOL_BATCH = 200

EXIF_TAGS = ["-DateTimeOriginal", "-CreateDate", "-MediaCreateDate",
             "-Model", "-ImageWidth", "-ImageHeight"]


def classify(ext: str) -> str:
    if ext in IMAGE_EXT:
        return "image"
    if ext in RAW_EXT:
        return "raw"
    if ext in VIDEO_EXT:
        return "video"
    if ext in SIDECAR_EXT:
        return "sidecar"
    return "other"


# ---------------------------------------------------------------- database
SCHEMA = """
CREATE TABLE IF NOT EXISTS files (
    id INTEGER PRIMARY KEY,
    source_path TEXT UNIQUE NOT NULL,
    source_root TEXT,
    rel_path TEXT,
    size INTEGER,
    mtime REAL,
    ext TEXT,
    kind TEXT,
    content_hash TEXT,
    phash TEXT,
    width INTEGER,
    height INTEGER,
    exif_date TEXT,
    camera TEXT,
    date_taken TEXT,
    date_source TEXT,
    date_confidence TEXT,
    group_id INTEGER,
    dupe_of INTEGER,
    role TEXT,
    status TEXT DEFAULT 'scanned',
    dest_path TEXT,
    error TEXT
);
CREATE INDEX IF NOT EXISTS idx_hash ON files(content_hash);
CREATE INDEX IF NOT EXISTS idx_status ON files(status);

CREATE TABLE IF NOT EXISTS runs (
    id INTEGER PRIMARY KEY,
    started TEXT,
    command TEXT,
    args TEXT
);

CREATE TABLE IF NOT EXISTS actions (
    id INTEGER PRIMARY KEY,
    run_id INTEGER,
    file_id INTEGER,
    action TEXT,
    detail TEXT,
    ts TEXT
);
"""


def open_db(workdir: Path) -> sqlite3.Connection:
    workdir.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(workdir / "photoflow.db")
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA)
    return conn


def new_run(conn, command, args) -> int:
    cur = conn.execute("INSERT INTO runs(started, command, args) VALUES (?,?,?)",
                       (datetime.now().isoformat(timespec="seconds"), command,
                        json.dumps(args)))
    conn.commit()
    return cur.lastrowid


def log_action(conn, log_fh, run_id, file_id, action, detail=""):
    ts = datetime.now().isoformat(timespec="seconds")
    conn.execute("INSERT INTO actions(run_id,file_id,action,detail,ts) VALUES (?,?,?,?,?)",
                 (run_id, file_id, action, detail, ts))
    log_fh.write(json.dumps({"ts": ts, "run": run_id, "file_id": file_id,
                             "action": action, "detail": detail}) + "\n")


# ---------------------------------------------------------------- exiftool
def exiftool_available() -> bool:
    return shutil.which("exiftool") is not None


def exiftool_json(paths: list[str]) -> dict[str, dict]:
    """Run exiftool on a batch of paths, return {path: tags}."""
    out: dict[str, dict] = {}
    for i in range(0, len(paths), EXIFTOOL_BATCH):
        batch = paths[i:i + EXIFTOOL_BATCH]
        with tempfile.NamedTemporaryFile("w", suffix=".args", delete=False,
                                         encoding="utf-8") as af:
            af.write("-j\n-n\n-fast2\n-charset\nfilename=utf8\n")
            for t in EXIF_TAGS:
                af.write(t + "\n")
            for p in batch:
                af.write(p + "\n")
            argfile = af.name
        try:
            res = subprocess.run(["exiftool", "-@", argfile],
                                 capture_output=True, text=True,
                                 encoding="utf-8", errors="replace")
            if res.stdout.strip():
                for rec in json.loads(res.stdout):
                    out[rec.get("SourceFile", "")] = rec
        except (json.JSONDecodeError, OSError) as e:
            print(f"  exiftool batch failed: {e}", file=sys.stderr)
        finally:
            os.unlink(argfile)
    return out


def exiftool_apply_argfile(lines: list[str]):
    """Run one exiftool process over a prepared -execute argfile (fast batching)."""
    if not lines:
        return
    with tempfile.NamedTemporaryFile("w", suffix=".args", delete=False,
                                     encoding="utf-8") as af:
        af.write("\n".join(lines) + "\n")
        argfile = af.name
    try:
        subprocess.run(["exiftool", "-@", argfile, "-charset", "filename=utf8"],
                       capture_output=True, text=True)
    finally:
        os.unlink(argfile)


# ---------------------------------------------------------------- hashing
def content_hash(path: Path) -> str:
    h = hashlib.blake2b(digest_size=20)
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def perceptual_hash(path: Path) -> str | None:
    if not HAVE_IMAGEHASH:
        return None
    try:
        with Image.open(path) as im:
            return str(imagehash.phash(im))
    except Exception:
        return None


def hamming(a: int, b: int) -> int:
    return bin(a ^ b).count("1")


class BKTree:
    """BK-tree over 64-bit perceptual hashes for fast near-neighbor lookup."""

    def __init__(self):
        self.root = None
        self.children: dict[int, dict[int, int]] = {}

    def add(self, h: int):
        if self.root is None:
            self.root = h
            self.children[h] = {}
            return
        node = self.root
        while True:
            d = hamming(h, node)
            if d == 0:
                return
            nxt = self.children[node].get(d)
            if nxt is None:
                self.children[node][d] = h
                self.children.setdefault(h, {})
                return
            node = nxt

    def query(self, h: int, radius: int) -> list[int]:
        if self.root is None:
            return []
        hits, stack = [], [self.root]
        while stack:
            node = stack.pop()
            d = hamming(h, node)
            if d <= radius:
                hits.append(node)
            lo, hi = d - radius, d + radius
            for dist, child in self.children[node].items():
                if lo <= dist <= hi:
                    stack.append(child)
        return hits


# ---------------------------------------------------------------- date resolution
EXIF_DATE_RE = re.compile(r"(\d{4}):(\d{2}):(\d{2})[ T](\d{2}):(\d{2}):(\d{2})")
FNAME_FULL_RE = re.compile(r"((?:19|20)\d{2})(\d{2})(\d{2})[_\-. ]?(\d{2})(\d{2})(\d{2})")
FNAME_WA_RE = re.compile(r"IMG-((?:19|20)\d{2})(\d{2})(\d{2})-WA", re.I)
FNAME_DATE_RE = re.compile(r"((?:19|20)\d{2})[-_.]([01]?\d)[-_.]([0-3]?\d)")
FOLDER_YEAR_RE = re.compile(r"\b((?:19|20)\d{2})\b")


def _valid(y, m, d, hh=0, mm=0, ss=0) -> datetime | None:
    try:
        dt = datetime(y, m, d, hh, mm, ss)
    except ValueError:
        return None
    return dt if MIN_YEAR <= y <= MAX_YEAR else None


def parse_exif_date(raw: str | None) -> datetime | None:
    if not raw:
        return None
    m = EXIF_DATE_RE.search(str(raw))
    return _valid(*(int(g) for g in m.groups())) if m else None


def date_from_filename(name: str) -> datetime | None:
    m = FNAME_FULL_RE.search(name)
    if m:
        dt = _valid(*(int(g) for g in m.groups()))
        if dt:
            return dt
    m = FNAME_WA_RE.search(name)
    if m:
        dt = _valid(int(m[1]), int(m[2]), int(m[3]))
        if dt:
            return dt
    m = FNAME_DATE_RE.search(name)
    if m:
        return _valid(int(m[1]), int(m[2]), int(m[3]))
    return None


def year_from_folder(rel_path: str) -> int | None:
    for part in reversed(Path(rel_path).parts[:-1]):
        m = FOLDER_YEAR_RE.search(part)
        if m and MIN_YEAR <= int(m[1]) <= MAX_YEAR:
            return int(m[1])
    return None


def resolve_date(row) -> tuple[str | None, str, str]:
    """Return (iso_date_or_None, source, confidence)."""
    dt = parse_exif_date(row["exif_date"])
    if dt:
        return dt.isoformat(), "exif", "high"
    dt = date_from_filename(Path(row["source_path"]).name)
    if dt:
        return dt.isoformat(), "filename", "medium"
    year = year_from_folder(row["rel_path"] or "")
    if year:
        return datetime(year, 1, 1).isoformat(), "folder", "low"
    if row["mtime"]:
        dt = datetime.fromtimestamp(row["mtime"])
        if MIN_YEAR <= dt.year <= MAX_YEAR:
            return dt.isoformat(), "mtime", "low"
    return None, "none", "none"


# ---------------------------------------------------------------- scan
def cmd_scan(conn, workdir, run_id, log_fh, args):
    if not exiftool_available():
        sys.exit("exiftool not found on PATH - install it first (see README).")
    if not HAVE_IMAGEHASH:
        print("NOTE: Pillow/ImageHash not installed - near-dupe flagging disabled, "
              "exact dedupe still works.")
    if not HAVE_HEIF:
        print("NOTE: pillow-heif not installed - HEIC files get exact dedupe only.")

    new_paths = []
    for root in args.sources:
        root = Path(root).expanduser().resolve()
        if not root.exists():
            print(f"skipping missing source: {root}")
            continue
        print(f"scanning {root} ...")
        for p in sorted(root.rglob("*")):
            if not p.is_file() or p.name.startswith("."):
                continue
            ext = p.suffix.lower()
            kind = classify(ext)
            if kind == "other":
                continue
            sp = str(p)
            existing = conn.execute(
                "SELECT size, mtime FROM files WHERE source_path=?", (sp,)).fetchone()
            st = p.stat()
            if existing and existing["size"] == st.st_size and \
                    abs(existing["mtime"] - st.st_mtime) < 1:
                continue  # already scanned, unchanged
            conn.execute(
                """INSERT INTO files(source_path, source_root, rel_path, size,
                                     mtime, ext, kind)
                   VALUES (?,?,?,?,?,?,?)
                   ON CONFLICT(source_path) DO UPDATE SET
                     size=excluded.size, mtime=excluded.mtime, status='scanned',
                     content_hash=NULL, phash=NULL""",
                (sp, str(root), str(p.relative_to(root)), st.st_size,
                 st.st_mtime, ext, kind))
            new_paths.append(sp)
    conn.commit()
    print(f"{len(new_paths)} new/changed files to fingerprint")

    # content hashes
    for n, sp in enumerate(new_paths, 1):
        try:
            ch = content_hash(Path(sp))
            conn.execute("UPDATE files SET content_hash=? WHERE source_path=?",
                         (ch, sp))
        except OSError as e:
            conn.execute("UPDATE files SET status='error', error=? WHERE source_path=?",
                         (str(e), sp))
        if n % 500 == 0:
            print(f"  hashed {n}/{len(new_paths)}")
            conn.commit()
    conn.commit()

    # exif
    print("reading metadata (exiftool)...")
    meta = exiftool_json(new_paths)
    for sp, rec in meta.items():
        raw_date = rec.get("DateTimeOriginal") or rec.get("CreateDate") \
            or rec.get("MediaCreateDate")
        conn.execute(
            "UPDATE files SET exif_date=?, camera=?, width=?, height=? "
            "WHERE source_path=?",
            (str(raw_date) if raw_date else None, rec.get("Model"),
             rec.get("ImageWidth"), rec.get("ImageHeight"), sp))
    conn.commit()

    # perceptual hashes (images only)
    if HAVE_IMAGEHASH:
        rows = conn.execute(
            "SELECT id, source_path FROM files WHERE kind='image' AND phash IS NULL "
            "AND status='scanned' AND source_path IN (%s)" %
            ",".join("?" * len(new_paths)), new_paths).fetchall() if new_paths else []
        for n, r in enumerate(rows, 1):
            ph = perceptual_hash(Path(r["source_path"]))
            if ph:
                conn.execute("UPDATE files SET phash=? WHERE id=?", (ph, r["id"]))
            if n % 500 == 0:
                print(f"  phashed {n}/{len(rows)}")
                conn.commit()
        conn.commit()

    for sp in new_paths:
        row = conn.execute("SELECT id FROM files WHERE source_path=?", (sp,)).fetchone()
        log_action(conn, log_fh, run_id, row["id"], "scanned", sp)
    conn.commit()
    print("scan complete. Next: python photoflow.py plan")


# ---------------------------------------------------------------- plan
def cmd_plan(conn, workdir, run_id, log_fh, args):
    # roles/groups are recomputed every plan; only copied/error/manual-skip
    # statuses are durable across plans
    conn.execute("""UPDATE files SET role=NULL, group_id=NULL, dupe_of=NULL
                    WHERE status NOT IN ('error','skipped_manual')""")
    conn.execute("""UPDATE files SET status='scanned'
                    WHERE status NOT IN ('copied','error','skipped_manual')""")
    conn.commit()
    group_seq = (conn.execute("SELECT COALESCE(MAX(group_id),0) FROM files")
                 .fetchone()[0] or 0)

    def next_group():
        nonlocal group_seq
        group_seq += 1
        return group_seq

    rows = conn.execute("SELECT * FROM files WHERE status IN ('scanned','copied') "
                        "AND content_hash IS NOT NULL").fetchall()
    by_id = {r["id"]: dict(r) for r in rows}

    # ---- 1. exact duplicate groups (identical bytes)
    by_hash = defaultdict(list)
    for r in by_id.values():
        by_hash[r["content_hash"]].append(r)
    exact_dupes = 0
    for members in by_hash.values():
        if len(members) == 1:
            members[0]["role"] = members[0]["role"] or "keep"
            continue
        gid = next_group()
        # keeper preference: already copied > earliest mtime
        members.sort(key=lambda m: (m["status"] != "copied", m["mtime"] or 0))
        keeper = members[0]
        keeper["role"], keeper["group_id"] = "keep", gid
        for d in members[1:]:
            d.update(role="exact_dupe", group_id=gid, dupe_of=keeper["id"])
            exact_dupes += 1

    keepers = [r for r in by_id.values() if r["role"] == "keep"]

    # ---- 2. RAW + JPEG pairs (same folder, same stem) - both kept, tagged
    stems = defaultdict(list)
    for r in keepers:
        p = Path(r["source_path"])
        stems[(str(p.parent).lower(), p.stem.lower())].append(r)
    raw_pairs, paired_ids = 0, set()
    for members in stems.values():
        kinds = {m["kind"] for m in members}
        if "raw" in kinds and ("image" in kinds):
            gid = next_group()
            for m in members:
                m["group_id"] = m["group_id"] or gid
                m["role"] = "raw_jpeg_pair"
                paired_ids.add(m["id"])
            raw_pairs += 1
        elif "video" in kinds and "image" in kinds:           # live photo
            gid = next_group()
            img = next(m for m in members if m["kind"] == "image")
            for m in members:
                m["group_id"] = m["group_id"] or gid
                if m["kind"] == "video":
                    m["role"] = "live_pair"
                    if not m["exif_date"]:
                        m["exif_date"] = img["exif_date"]
                paired_ids.add(m["id"])

    # ---- 3. near-dupe flagging via pHash (images only, never auto-delete)
    review_groups, burst_groups = 0, 0
    if HAVE_IMAGEHASH:
        ph_map = defaultdict(list)            # phash int -> [file rows]
        for r in keepers:
            if r["kind"] == "image" and r["phash"]:
                ph_map[int(r["phash"], 16)].append(r)
        tree = BKTree()
        for h in ph_map:
            tree.add(h)
        parent = {h: h for h in ph_map}

        def find(x):
            while parent[x] != x:
                parent[x] = parent[parent[x]]
                x = parent[x]
            return x

        for h in ph_map:
            for nb in tree.query(h, NEAR_DUPE_THRESHOLD):
                ra, rb = find(h), find(nb)
                if ra != rb:
                    parent[ra] = rb
        comps = defaultdict(list)
        for h in ph_map:
            comps[find(h)].extend(ph_map[h])
        for members in comps.values():
            # drop raw/jpeg pair members - those are intentional twins
            members = [m for m in members if m["id"] not in paired_ids]
            if len(members) < 2:
                continue
            # burst test: every member has an exif time, same camera,
            # consecutive gaps within the window -> keep all silently
            times = [parse_exif_date(m["exif_date"]) for m in members]
            cams = {m["camera"] for m in members}
            is_burst = (all(times) and len(cams) == 1 and None not in cams)
            if is_burst:
                times.sort()
                is_burst = all((b - a) <= timedelta(seconds=BURST_WINDOW_S)
                               for a, b in zip(times, times[1:]))
            gid = next_group()
            for m in members:
                m["group_id"] = gid
                m["role"] = "burst" if is_burst else "review"
            if is_burst:
                burst_groups += 1
            else:
                review_groups += 1

    # ---- 4. resolve dates
    for r in by_id.values():
        iso, src, conf = resolve_date(r)
        r["date_taken"], r["date_source"], r["date_confidence"] = iso, src, conf

    # ---- write plan back
    for r in by_id.values():
        status = r["status"]
        if status != "copied":
            status = "review" if r["role"] == "review" else "planned"
        conn.execute(
            """UPDATE files SET role=?, group_id=?, dupe_of=?, date_taken=?,
               date_source=?, date_confidence=?, exif_date=?, status=? WHERE id=?""",
            (r["role"], r["group_id"], r["dupe_of"], r["date_taken"],
             r["date_source"], r["date_confidence"], r["exif_date"], status, r["id"]))
    conn.commit()
    log_action(conn, log_fh, run_id, 0, "plan",
               f"exact_dupes={exact_dupes} raw_pairs={raw_pairs} "
               f"bursts={burst_groups} review_groups={review_groups}")
    conn.commit()
    print(f"plan complete: {exact_dupes} exact dupes will be skipped, "
          f"{raw_pairs} RAW+JPEG pairs kept, {burst_groups} burst groups kept, "
          f"{review_groups} near-dupe groups need review.")
    if review_groups:
        print("Next: python photoflow.py review")
    else:
        print("Next: python photoflow.py apply --out <DIR>")


# ---------------------------------------------------------------- review
def cmd_review(conn, workdir, run_id, log_fh, args):
    rows = conn.execute("SELECT * FROM files WHERE role='review' "
                        "ORDER BY group_id, size DESC").fetchall()
    if not rows:
        print("nothing queued for review.")
        return
    thumbs = workdir / "thumbs"
    thumbs.mkdir(exist_ok=True)
    dec_path = workdir / "decisions.csv"
    html_path = workdir / "review.html"

    # carry forward any decisions already made so regeneration never loses work
    prior: dict[str, dict] = {}
    if dec_path.exists():
        with open(dec_path, newline="", encoding="utf-8") as f:
            for row in csv.DictReader(f):
                if (row.get("decision") or "").strip():
                    prior[row["file_id"]] = row

    groups = defaultdict(list)
    for r in rows:
        groups[r["group_id"]].append(r)

    with open(dec_path, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["group_id", "file_id", "source_path", "resolution", "size_kb",
                    "suggestion", "decision", "merge_from_file_id"])
        for gid, members in groups.items():
            best = max(members, key=lambda m: (m["width"] or 0) * (m["height"] or 0))
            for m in members:
                old = prior.get(str(m["id"]), {})
                w.writerow([gid, m["id"], m["source_path"],
                            f'{m["width"]}x{m["height"]}',
                            round((m["size"] or 0) / 1024),
                            "keep" if m["id"] == best["id"] else "keep?",
                            old.get("decision", ""),
                            old.get("merge_from_file_id", "")])

    parts = ["<html><head><meta charset='utf-8'><style>",
             "body{font-family:sans-serif;background:#111;color:#ddd}",
             ".g{border:1px solid #444;margin:14px;padding:10px;border-radius:8px}",
             ".f{display:inline-block;margin:6px;text-align:center;vertical-align:top}",
             "img{max-height:220px;border-radius:4px}",
             "small{display:block;max-width:260px;word-break:break-all;color:#9ab}",
             "</style></head><body><h2>photoflow review queue</h2>",
             "<p>Edit <b>decisions.csv</b>: set decision to <b>keep</b> or "
             "<b>skip</b> per row. Optionally set merge_from_file_id to pull "
             "missing metadata (GPS, dates) from a skipped twin into the keeper. "
             "Rows left blank stay on hold.</p>"]
    for gid, members in groups.items():
        parts.append(f"<div class='g'><h3>group {gid}</h3>")
        for m in members:
            tp = thumbs / f"{m['id']}.jpg"
            if HAVE_PIL and not tp.exists():
                try:
                    with Image.open(m["source_path"]) as im:
                        im.thumbnail((320, 320))
                        im.convert("RGB").save(tp, "JPEG", quality=80)
                except Exception:
                    pass
            img = f"<img src='thumbs/{m['id']}.jpg'>" if tp.exists() else "(no preview)"
            parts.append(
                f"<div class='f'>{img}<small>id {m['id']} | "
                f"{m['width']}x{m['height']} | {round((m['size'] or 0)/1024)} KB | "
                f"{m['date_taken'] or 'no date'}<br>"
                f"{html.escape(m['source_path'])}</small></div>")
        parts.append("</div>")
    parts.append("</body></html>")
    html_path.write_text("\n".join(parts), encoding="utf-8")

    log_action(conn, log_fh, run_id, 0, "review_exported",
               f"{len(groups)} groups, {len(rows)} files")
    conn.commit()
    print(f"review.html  -> {html_path}")
    print(f"decisions.csv -> {dec_path}")
    print("Open the HTML, fill the CSV, then run apply.")


# ---------------------------------------------------------------- apply
def slugify(stem: str) -> str:
    s = re.sub(r"[^A-Za-z0-9]+", "-", stem).strip("-")
    return (s[:SLUG_MAX] or "img")


def dest_for(row, out_root: Path) -> Path:
    h8 = row["content_hash"][:8]
    slug = slugify(Path(row["source_path"]).stem)
    ext = row["ext"].lower()
    if row["date_taken"]:
        dt = datetime.fromisoformat(row["date_taken"])
        folder = out_root / f"{dt.year}" / f"{dt.month:02d}"
        if row["date_source"] in ("exif", "filename") and \
                (dt.hour, dt.minute, dt.second) != (0, 0, 0):
            name = f"{dt:%Y%m%d_%H%M%S}_{slug}_{h8}{ext}"
        else:
            name = f"{dt:%Y%m%d}_{slug}_{h8}{ext}"
    else:
        folder = out_root / "unknown-date"
        name = f"{slug}_{h8}{ext}"
    return folder / name


def xmp_sidecar(dest: Path, description: str, keywords: list[str]):
    kw = "".join(f"<rdf:li>{html.escape(k)}</rdf:li>" for k in keywords)
    xml = f"""<?xpacket begin="\ufeff" id="W5M0MpCehiHzreSzNTczkc9d"?>
<x:xmpmeta xmlns:x="adobe:ns:meta/">
 <rdf:RDF xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#">
  <rdf:Description xmlns:dc="http://purl.org/dc/elements/1.1/">
   <dc:description><rdf:Alt><rdf:li xml:lang="x-default">{html.escape(description)}</rdf:li></rdf:Alt></dc:description>
   <dc:subject><rdf:Bag>{kw}</rdf:Bag></dc:subject>
  </rdf:Description>
 </rdf:RDF>
</x:xmpmeta>
<?xpacket end="w"?>"""
    dest.with_suffix(dest.suffix + ".xmp").write_text(xml, encoding="utf-8")


def cmd_apply(conn, workdir, run_id, log_fh, args):
    out_root = Path(args.out).expanduser().resolve()
    decisions: dict[int, tuple[str, int | None]] = {}
    dec_path = Path(args.decisions) if args.decisions else workdir / "decisions.csv"
    if dec_path.exists():
        with open(dec_path, newline="", encoding="utf-8") as f:
            for row in csv.DictReader(f):
                d = (row.get("decision") or "").strip().lower()
                if d in ("keep", "skip"):
                    mf = (row.get("merge_from_file_id") or "").strip()
                    decisions[int(row["file_id"])] = (d, int(mf) if mf else None)

    rows = conn.execute(
        "SELECT * FROM files WHERE status IN ('planned','review')").fetchall()
    embed_kinds_ext = {".jpg", ".jpeg", ".tif", ".tiff", ".png", ".heic", ".heif"}
    copied = skipped = held = 0
    xmp_args: list[str] = []
    merge_jobs: list[tuple[int, int]] = []

    for r in rows:
        role = r["role"]
        if role == "exact_dupe":
            conn.execute("UPDATE files SET status='skipped_dupe' WHERE id=?",
                         (r["id"],))
            log_action(conn, log_fh, run_id, r["id"], "skipped_exact_dupe",
                       f"dupe_of={r['dupe_of']}")
            skipped += 1
            continue
        if role == "review":
            d = decisions.get(r["id"])
            if d is None:
                held += 1
                continue
            if d[0] == "skip":
                conn.execute("UPDATE files SET status='skipped_manual' WHERE id=?",
                             (r["id"],))
                log_action(conn, log_fh, run_id, r["id"], "skipped_manual_review", "")
                skipped += 1
                continue
            if d[1]:
                merge_jobs.append((r["id"], d[1]))

        dest = dest_for(r, out_root)
        dest.parent.mkdir(parents=True, exist_ok=True)
        if args.dry_run:
            print(f"DRY  {r['source_path']}  ->  {dest}")
            continue
        if not dest.exists():
            shutil.copy2(r["source_path"], dest)

        # provenance metadata: original folder names + dupes' folders as keywords
        rels = [r["rel_path"] or ""]
        for d2 in conn.execute("SELECT rel_path FROM files WHERE dupe_of=?",
                               (r["id"],)):
            rels.append(d2["rel_path"] or "")
        kw = sorted({part for rel in rels
                     for part in Path(rel).parts[:-1] if part})[:12]
        desc = "photoflow src: " + " | ".join(filter(None, rels))
        if r["ext"] in embed_kinds_ext:
            xmp_args += ["-overwrite_original", f"-XMP-dc:Description={desc}"]
            xmp_args += [f"-XMP-dc:Subject={k}" for k in kw]
            xmp_args += [str(dest), "-execute"]
        else:
            xmp_sidecar(dest, desc, kw)

        conn.execute("UPDATE files SET status='copied', dest_path=? WHERE id=?",
                     (str(dest), r["id"]))
        log_action(conn, log_fh, run_id, r["id"], "copied",
                   f"{r['source_path']} -> {dest} (date:{r['date_source']}/"
                   f"{r['date_confidence']}, role:{role})")
        copied += 1
        if copied % 500 == 0:
            conn.commit()
            print(f"  copied {copied}...")
    conn.commit()

    if not args.dry_run and xmp_args:
        print("embedding XMP provenance (exiftool)...")
        exiftool_apply_argfile(xmp_args)

    # metadata merges chosen during review: fill missing tags from the twin
    for keeper_id, donor_id in merge_jobs:
        k = conn.execute("SELECT dest_path FROM files WHERE id=?",
                         (keeper_id,)).fetchone()
        d = conn.execute("SELECT source_path FROM files WHERE id=?",
                         (donor_id,)).fetchone()
        if k and k["dest_path"] and d and not args.dry_run:
            subprocess.run(["exiftool", "-overwrite_original", "-wm", "cg",
                            "-tagsfromfile", d["source_path"], "-all:all",
                            k["dest_path"]], capture_output=True)
            log_action(conn, log_fh, run_id, keeper_id, "metadata_merged",
                       f"from file {donor_id}")
    conn.commit()
    print(f"apply complete: {copied} copied, {skipped} skipped, "
          f"{held} still held for review.")
    if held:
        print("Held files: fill in decisions.csv and run apply again.")


# ---------------------------------------------------------------- status
def cmd_status(conn, workdir, run_id, log_fh, args):
    print("by status:")
    for r in conn.execute("SELECT status, COUNT(*) c FROM files GROUP BY status"):
        print(f"  {r['status']:>14}: {r['c']}")
    print("by role:")
    for r in conn.execute("SELECT COALESCE(role,'-') role, COUNT(*) c "
                          "FROM files GROUP BY role"):
        print(f"  {r['role']:>14}: {r['c']}")
    print("by date source:")
    for r in conn.execute("SELECT COALESCE(date_source,'-') s, COUNT(*) c "
                          "FROM files GROUP BY date_source"):
        print(f"  {r['s']:>14}: {r['c']}")


# ---------------------------------------------------------------- main
def main():
    ap = argparse.ArgumentParser(description="photoflow - incremental photo organizer")
    ap.add_argument("--workdir", default="photoflow_work",
                    help="state directory (db, logs, review files)")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("scan", help="fingerprint source folders into the manifest")
    p.add_argument("sources", nargs="+")

    sub.add_parser("plan", help="resolve dates, group dupes, queue reviews")
    sub.add_parser("review", help="export review.html + decisions.csv")

    p = sub.add_parser("apply", help="copy keepers into the organized library")
    p.add_argument("--out", required=True, help="output library root")
    p.add_argument("--decisions", help="decisions CSV (default workdir/decisions.csv)")
    p.add_argument("--dry-run", action="store_true")

    sub.add_parser("status", help="manifest summary")

    args = ap.parse_args()
    workdir = Path(args.workdir).expanduser().resolve()
    conn = open_db(workdir)
    run_id = new_run(conn, args.cmd, vars(args) | {"workdir": str(workdir)})
    logs = workdir / "logs"
    logs.mkdir(exist_ok=True)
    with open(logs / f"run_{run_id:04d}_{args.cmd}.jsonl", "a",
              encoding="utf-8") as log_fh:
        {"scan": cmd_scan, "plan": cmd_plan, "review": cmd_review,
         "apply": cmd_apply, "status": cmd_status}[args.cmd](
            conn, workdir, run_id, log_fh, args)


if __name__ == "__main__":
    main()
