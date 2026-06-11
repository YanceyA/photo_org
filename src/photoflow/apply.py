"""Apply command: copy keepers into the organized library, embed provenance, merge metadata."""

from __future__ import annotations

import csv
import shutil
from pathlib import Path

from photoflow.audit import log_action
from photoflow.exiftool import exiftool_apply_argfile, merge_metadata
from photoflow.naming import dest_for
from photoflow.xmp import EMBED_EXT, embed_args, xmp_sidecar


def cmd_apply(conn, workdir, run_id, log_fh, args, cfg):
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

    rows = conn.execute("SELECT * FROM files WHERE status IN ('planned','review')").fetchall()
    copied = skipped = held = 0
    xmp_args: list[str] = []
    merge_jobs: list[tuple[int, int]] = []

    for r in rows:
        role = r["role"]
        if role == "exact_dupe":
            conn.execute("UPDATE files SET status='skipped_dupe' WHERE id=?", (r["id"],))
            log_action(
                conn, log_fh, run_id, r["id"], "skipped_exact_dupe", f"dupe_of={r['dupe_of']}"
            )
            skipped += 1
            continue
        if role == "review":
            d = decisions.get(r["id"])
            if d is None:
                held += 1
                continue
            if d[0] == "skip":
                conn.execute("UPDATE files SET status='skipped_manual' WHERE id=?", (r["id"],))
                log_action(conn, log_fh, run_id, r["id"], "skipped_manual_review", "")
                skipped += 1
                continue
            if d[1]:
                merge_jobs.append((r["id"], d[1]))

        dest = dest_for(r, out_root, cfg.slug_max)
        dest.parent.mkdir(parents=True, exist_ok=True)
        if args.dry_run:
            print(f"DRY  {r['source_path']}  ->  {dest}")
            continue
        if not dest.exists():
            shutil.copy2(r["source_path"], dest)

        # provenance metadata: original folder names + dupes' folders as keywords
        rels = [r["rel_path"] or ""]
        for d2 in conn.execute("SELECT rel_path FROM files WHERE dupe_of=?", (r["id"],)):
            rels.append(d2["rel_path"] or "")
        kw = sorted({part for rel in rels for part in Path(rel).parts[:-1] if part})[:12]
        desc = "photoflow src: " + " | ".join(filter(None, rels))
        if r["ext"] in EMBED_EXT:
            xmp_args += embed_args(str(dest), desc, kw)
        else:
            xmp_sidecar(dest, desc, kw)

        conn.execute(
            "UPDATE files SET status='copied', dest_path=? WHERE id=?", (str(dest), r["id"])
        )
        log_action(
            conn,
            log_fh,
            run_id,
            r["id"],
            "copied",
            f"{r['source_path']} -> {dest} (date:{r['date_source']}/"
            f"{r['date_confidence']}, role:{role})",
        )
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
        k = conn.execute("SELECT dest_path FROM files WHERE id=?", (keeper_id,)).fetchone()
        d = conn.execute("SELECT source_path FROM files WHERE id=?", (donor_id,)).fetchone()
        if k and k["dest_path"] and d and not args.dry_run:
            merge_metadata(d["source_path"], k["dest_path"])
            log_action(conn, log_fh, run_id, keeper_id, "metadata_merged", f"from file {donor_id}")
    conn.commit()
    print(f"apply complete: {copied} copied, {skipped} skipped, {held} still held for review.")
    if held:
        print("Held files: fill in decisions.csv and run apply again.")
