"""enrich apply: write confirmed people + content tags into the library files.

Reads the faces.csv / tags.csv decision overlays, makes person assignments durable, then
writes XMP into each copied file (embed for EMBED_EXT, .xmp sidecar otherwise):
  * keywords  - dc:Subject union (existing + tags + people), mirrored to IPTC + lr hierarchy
  * regions   - MWG face rectangles per assigned person

The keyword write is a read-union-replace so it never clobbers apply's provenance
description / folder keywords, and re-running is idempotent.
"""

from __future__ import annotations

import csv
import json
from datetime import datetime
from pathlib import Path

from photoflow.audit import log_action
from photoflow.enrich.page import face_is_applied, tag_is_applied
from photoflow.enrich.regions import keyword_argfile_lines, region_argfile_lines
from photoflow.exiftool import exiftool_apply_argfile, read_keywords
from photoflow.xmp import EMBED_EXT


def _read_csv(path) -> list[dict]:
    if not path.exists():
        return []
    with open(path, newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))


def _upsert_person(conn, name: str) -> int:
    row = conn.execute("SELECT id FROM persons WHERE name=?", (name,)).fetchone()
    if row:
        return row["id"]
    cur = conn.execute(
        "INSERT INTO persons(name, created) VALUES (?,?)",
        (name, datetime.now().isoformat(timespec="seconds")),
    )
    return cur.lastrowid


def cmd_enrich_apply(conn, workdir, run_id, log_fh, args, cfg):
    dry = getattr(args, "dry_run", False)
    face_csv = _read_csv(workdir / "faces.csv")
    tag_csv = _read_csv(workdir / "tags.csv")

    # 1. make person assignments durable (faces.person_id)
    for row in face_csv:
        if face_is_applied(row.get("person", ""), row.get("decision", "")):
            pid = _upsert_person(conn, row["person"].strip())
            conn.execute("UPDATE faces SET person_id=? WHERE id=?", (pid, int(row["face_id"])))
    if not dry:
        conn.commit()

    # 2. tag decision overlay: blacklist wildcards + per-(file,tag) review decisions
    blacklist = {
        r["tag"] for r in tag_csv if str(r.get("file_id")) == "*" and r.get("decision") == "reject"
    }
    review_dec = {
        (str(r["file_id"]), r["tag"]): (r.get("decision") or "")
        for r in tag_csv
        if str(r.get("file_id")) != "*"
    }

    # 3. candidate files: any assigned-person face or any tag. EXISTS subqueries (not an
    # IN(...) list) so this never trips SQLite's 32766-variable limit on a large library.
    targets: dict[int, tuple[str, str]] = {}  # file_id -> (write_target, dest_path)
    file_rows = conn.execute(
        """SELECT id, dest_path, ext FROM files f
           WHERE dest_path IS NOT NULL
             AND (EXISTS (SELECT 1 FROM faces WHERE file_id=f.id AND person_id IS NOT NULL)
                  OR EXISTS (SELECT 1 FROM tags WHERE file_id=f.id))"""
    ).fetchall()
    for fr in file_rows:
        is_embed = (fr["ext"] or "").lower() in EMBED_EXT
        dest = fr["dest_path"]
        targets[fr["id"]] = (dest if is_embed else dest + ".xmp", dest)
    if not targets:
        print("enrich apply: nothing to write (no assigned people or tags).")
        return

    existing_map = read_keywords([t for (t, _d) in targets.values()]) if targets else {}

    xmp_args: list[str] = []
    applied_ids: list[int] = []
    n = 0
    for fid, (target, dest) in targets.items():
        tags_for_file = sorted(
            t["tag"]
            for t in conn.execute("SELECT tag, status FROM tags WHERE file_id=?", (fid,))
            if tag_is_applied(
                t["status"], review_dec.get((str(fid), t["tag"]), ""), t["tag"] in blacklist
            )
        )
        people: list[str] = []
        regions: list[tuple[str, tuple]] = []
        img_w = img_h = None
        for fa in conn.execute(
            "SELECT fa.bbox, fa.img_w, fa.img_h, p.name FROM faces fa "
            "JOIN persons p ON p.id = fa.person_id WHERE fa.file_id=?",
            (fid,),
        ):
            people.append(fa["name"])
            try:
                bbox = tuple(json.loads(fa["bbox"]))
            except (TypeError, ValueError, json.JSONDecodeError):
                continue
            regions.append((fa["name"], bbox))
            img_w, img_h = fa["img_w"], fa["img_h"]
        people = sorted(set(people))

        if not tags_for_file and not people:
            continue

        existing = existing_map.get(str(Path(target)), set())
        lines = keyword_argfile_lines(
            existing,
            tags_for_file,
            people,
            prefix=cfg.people_keyword_prefix,
            iptc=cfg.write_iptc_keywords,
        )
        if cfg.write_mwg_regions and regions and img_w and img_h:
            lines += region_argfile_lines(img_w, img_h, regions)

        if dry:
            print(f"DRY enrich {dest}: +{len(tags_for_file)} tags, {len(people)} people")
        else:
            xmp_args += ["-overwrite_original", *lines, target, "-execute"]
            applied_ids.append(fid)
        n += 1

    if not dry and xmp_args:
        print("writing enrich XMP (exiftool)...")
        exiftool_apply_argfile(xmp_args)
    if not dry:
        for fid in applied_ids:
            conn.execute(
                "INSERT INTO enrich_state(file_id, applied, ts) VALUES (?,1,?) "
                "ON CONFLICT(file_id) DO UPDATE SET applied=1, ts=excluded.ts",
                (fid, datetime.now().isoformat(timespec="seconds")),
            )
        conn.commit()
    log_action(conn, log_fh, run_id, 0, "enrich_apply", f"files={n} dry={dry}")
    conn.commit()
    print(f"enrich apply complete: {n} files {'(dry-run, nothing written)' if dry else 'written'}.")
