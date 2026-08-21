"""enrich apply: write confirmed people + content tags into the library files.

Reads the faces.csv / tags.csv decision overlays, makes person assignments durable, then
writes XMP into each copied file (embed for EMBED_EXT, .xmp sidecar otherwise):
  * keywords  - dc:Subject union (existing + tags + people), mirrored to IPTC + lr hierarchy
  * regions   - MWG face rectangles per assigned person

The keyword write is a read-union-replace so it never clobbers apply's provenance
description / folder keywords, and re-running is idempotent.

The pass is incremental: each file carries a signature of the people/tags/regions apply last
wrote (enrich_state.applied_sig), and a file whose signature is unchanged is left alone
(`--all` forces the rewrite). Writes use -P so the library mtime never moves.
"""

from __future__ import annotations

import csv
import hashlib
import json
from collections import defaultdict
from datetime import datetime
from pathlib import Path

from photoflow.audit import log_action
from photoflow.enrich.page import face_is_applied, tag_is_applied
from photoflow.enrich.regions import keyword_argfile_lines, region_argfile_lines
from photoflow.exiftool import exiftool_apply_argfile, exiftool_available, read_keywords
from photoflow.xmp import EMBED_EXT

# -execute blocks per exiftool process. Failure is recorded per BATCH, not per file: exiftool
# reports "N image files updated" for the whole run, so a batch that exits non-zero leaves
# every one of its files with its previous applied_sig and gets retried next run.
WRITE_BATCH = 100


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


def _signature(tags, people, regions, img_w, img_h) -> str:
    """Hash of everything this file's write depends on. Equal signature => skip the rewrite.

    Deliberately excludes the file's EXISTING keywords: a keyword added in digiKam must not
    trigger a rewrite (the write is a union, so nothing would change) - only OUR data does.
    """
    payload = {
        "tags": list(tags),
        "people": list(people),
        "regions": [[name, [float(v) for v in bbox]] for name, bbox in regions],
        "dims": [img_w, img_h],
    }
    return hashlib.sha1(json.dumps(payload, sort_keys=True).encode("utf-8")).hexdigest()


def cmd_enrich_apply(conn, workdir, run_id, log_fh, args, cfg):
    dry = getattr(args, "dry_run", False)
    rewrite_all = getattr(args, "all", False)
    if not exiftool_available():
        print("enrich apply: exiftool not found on PATH - nothing written.")
        return
    face_csv = _read_csv(workdir / "faces.csv")
    tag_csv = _read_csv(workdir / "tags.csv")
    now = datetime.now().isoformat(timespec="seconds")

    # 1. make person assignments durable (faces.person_id)
    for row in face_csv:
        if face_is_applied(row.get("person", ""), row.get("decision", "")):
            pid = _upsert_person(conn, row["person"].strip())
            conn.execute("UPDATE faces SET person_id=? WHERE id=?", (pid, int(row["face_id"])))

    # 1b. "not interested" clusters: a cluster whose every member is skipped (none named) was
    # dismissed wholesale in the page -> mark its faces ignored so re-cluster/review drop them
    # for good. A lone skip inside an otherwise-named cluster is just an eject, left eligible.
    by_cluster: dict[str, list[dict]] = defaultdict(list)
    for row in face_csv:
        cid = (row.get("cluster_id") or "").strip()
        if cid:
            by_cluster[cid].append(row)
    for members in by_cluster.values():
        if all((m.get("decision") or "") == "skip" for m in members):
            for m in members:
                conn.execute(
                    "UPDATE faces SET ignored=1, cluster_id=NULL, cluster_prob=NULL WHERE id=?",
                    (int(m["face_id"]),),
                )
    # R2: in dry mode NOTHING above is committed - the whole command runs inside one
    # transaction that is rolled back at the end, so a dry run can't hide clusters from the
    # next `enrich review`.
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
        if dry:
            conn.rollback()
        return

    prior_sig = {
        r["file_id"]: r["applied_sig"]
        for r in conn.execute("SELECT file_id, applied_sig FROM enrich_state")
    }

    # 4. pass one: compute what each file WOULD get and skip the ones already carrying it.
    pending: list[dict] = []
    unchanged = 0
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
        sig = _signature(tags_for_file, people, regions, img_w, img_h)
        if not rewrite_all and prior_sig.get(fid) == sig:
            unchanged += 1
            continue
        pending.append(
            {
                "fid": fid,
                "target": target,
                "dest": dest,
                "tags": tags_for_file,
                "people": people,
                "regions": regions,
                "w": img_w,
                "h": img_h,
                "sig": sig,
            }
        )

    # 5. pass two: read the CURRENT keywords of just the files we're about to rewrite.
    # Keys are re-normalized because exiftool reports SourceFile with forward slashes even on
    # Windows (HANDOFF §7) - a mismatch here would look like an unreadable file below.
    existing_map = {
        str(Path(k)): v
        for k, v in (read_keywords([p["target"] for p in pending]) if pending else {}).items()
    }

    blocks: list[tuple[int, str, list[str]]] = []  # (file_id, sig, argfile lines)
    skipped_unreadable = 0
    for p in pending:
        key = str(Path(p["target"]))
        if key not in existing_map:
            # R1: one corrupt XMP makes read_keywords return {} for its whole batch. Writing
            # with existing=set() would CLEAR every pre-existing keyword on these files.
            skipped_unreadable += 1
            print(f"  WARNING: could not read existing keywords, skipping {p['dest']}")
            continue
        lines = keyword_argfile_lines(
            existing_map[key],
            p["tags"],
            p["people"],
            prefix=cfg.people_keyword_prefix,
            iptc=cfg.write_iptc_keywords,
        )
        if cfg.write_mwg_regions and p["regions"] and p["w"] and p["h"]:
            lines += region_argfile_lines(p["w"], p["h"], p["regions"])
        if dry:
            print(f"DRY enrich {p['dest']}: +{len(p['tags'])} tags, {len(p['people'])} people")
            continue
        # -P preserves the file's mtime (H9); without it every apply bumps the whole library.
        blocks.append(
            (p["fid"], p["sig"], ["-P", "-overwrite_original", *lines, p["target"], "-execute"])
        )

    # 6. write in batches; a batch that exits non-zero marks NONE of its files applied.
    written = failed = 0
    for i in range(0, len(blocks), WRITE_BATCH):
        chunk = blocks[i : i + WRITE_BATCH]
        lines = []
        for _fid, _sig, block in chunk:
            lines += block
        print(f"  writing enrich XMP {i + 1}-{i + len(chunk)} of {len(blocks)} (exiftool)...")
        res = exiftool_apply_argfile(lines)
        if res.returncode != 0:
            failed += len(chunk)
            print(f"  exiftool batch FAILED (rc={res.returncode}); not marking applied:")
            for err in (res.stderr or "").strip().splitlines()[:5]:
                print(f"    {err}")
            continue
        for fid, sig, _block in chunk:
            conn.execute(
                "INSERT INTO enrich_state(file_id, applied, applied_sig, ts) VALUES (?,1,?,?) "
                "ON CONFLICT(file_id) DO UPDATE SET applied=1, applied_sig=excluded.applied_sig,"
                " ts=excluded.ts",
                (fid, sig, now),
            )
            log_action(conn, log_fh, run_id, fid, "enrich_applied", sig)
            written += 1

    if dry:
        conn.rollback()  # R2: discard step 1/1b - a dry run mutates nothing
    log_action(
        conn,
        log_fh,
        run_id,
        0,
        "enrich_apply",
        f"written={written} unchanged={unchanged} skipped={skipped_unreadable} "
        f"failed={failed} dry={dry}",
    )
    conn.commit()
    print(
        f"enrich apply: written {written} / unchanged {unchanged} / "
        f"skipped-unreadable {skipped_unreadable} / failed {failed}"
        f"{' (dry-run, nothing written)' if dry else ''}."
    )
