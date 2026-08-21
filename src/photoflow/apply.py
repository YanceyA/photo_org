"""Apply command: copy keepers into the organized library, embed provenance, merge metadata."""

from __future__ import annotations

import csv
import os
import shutil
from pathlib import Path

from photoflow.audit import log_action
from photoflow.exiftool import exiftool_apply_argfile, merge_metadata
from photoflow.naming import dest_for
from photoflow.xmp import EMBED_EXT, embed_args, xmp_sidecar


def _copy_atomic(src: str, dest: Path) -> None:
    """Copy via <dest>.part + os.replace so a crash / full disk can never leave a
    truncated file sitting at dest (os.replace is atomic within one filesystem)."""
    tmp = dest.with_name(dest.name + ".part")
    try:
        shutil.copy2(src, tmp)
        os.replace(tmp, dest)
    except OSError:
        tmp.unlink(missing_ok=True)
        raise


def _flush_xmp(conn, log_fh, run_id, xmp_args: list[str]) -> None:
    """Embed the queued provenance blocks and report (never raise) exiftool failures.

    Called before each commit, so a crash can only ever leave files embedded but not
    yet marked copied - which the next run repairs. The reverse order would mark rows
    copied with no provenance and never revisit them.
    """
    if not xmp_args:
        return
    print(f"  embedding XMP provenance for {xmp_args.count('-execute')} files (exiftool)...")
    res = exiftool_apply_argfile(xmp_args)
    if res.returncode != 0:
        head = " / ".join(res.stderr.strip().splitlines()[:3])
        print(f"exiftool reported errors: {head}")
        log_action(conn, log_fh, run_id, 0, "xmp_embed_errors", f"rc={res.returncode} {head}")
    xmp_args.clear()


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
    copied = skipped = held = errors = 0
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
        if args.dry_run:
            print(f"DRY  {r['source_path']}  ->  {dest}")
            continue

        try:
            dest.parent.mkdir(parents=True, exist_ok=True)
            src_size = os.path.getsize(r["source_path"])
            if not dest.exists():
                _copy_atomic(r["source_path"], dest)
            elif dest.stat().st_size < src_size:
                # Only reachable when a previous run died between the copy and its
                # commit. A dest SMALLER than the source is a truncated copy from a
                # pre-atomic-copy version -> re-copy. A dest that is LARGER is a file
                # that was already XMP-embedded (embedding grows it) before the crash ->
                # trust it; the provenance lines are re-emitted below anyway, so it
                # converges without a wasted copy. (Coordinator note: `<` not `!=`.)
                _copy_atomic(r["source_path"], dest)
                log_action(conn, log_fh, run_id, r["id"], "recopied_size_mismatch", str(dest))
        except OSError as e:
            conn.execute("UPDATE files SET status='error', error=? WHERE id=?", (str(e), r["id"]))
            log_action(conn, log_fh, run_id, r["id"], "copy_error", str(e))
            errors += 1
            continue

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
            _flush_xmp(conn, log_fh, run_id, xmp_args)
            conn.commit()
            print(f"  copied {copied}...")
    if not args.dry_run:
        _flush_xmp(conn, log_fh, run_id, xmp_args)
    conn.commit()

    # metadata merges chosen during review: fill missing tags from the twin
    for keeper_id, donor_id in merge_jobs:
        k = conn.execute("SELECT dest_path FROM files WHERE id=?", (keeper_id,)).fetchone()
        d = conn.execute("SELECT source_path FROM files WHERE id=?", (donor_id,)).fetchone()
        if k and k["dest_path"] and d and not args.dry_run:
            merge_metadata(d["source_path"], k["dest_path"])
            log_action(conn, log_fh, run_id, keeper_id, "metadata_merged", f"from file {donor_id}")
    conn.commit()
    print(
        f"apply complete: {copied} copied, {skipped} skipped, {held} still held for review, "
        f"{errors} errors."
    )
    if errors:
        print("Errored files keep status='error' (durable) - fix the source and clear the row.")
    if held:
        print("Held files: fill in decisions.csv and run apply again.")
