"""Scan command: fingerprint source folders into the manifest."""

from __future__ import annotations

import os
import sys
from pathlib import Path

from photoflow.audit import log_action
from photoflow.exiftool import exiftool_available, exiftool_json
from photoflow.hashing import HAVE_HEIF, HAVE_IMAGEHASH, content_hash, perceptual_hash
from photoflow.models import classify


def cmd_scan(conn, workdir, run_id, log_fh, args, cfg):
    if not exiftool_available():
        sys.exit("exiftool not found on PATH - install it first (see README).")
    if not HAVE_IMAGEHASH:
        print(
            "NOTE: Pillow/ImageHash not installed - near-dupe flagging disabled, "
            "exact dedupe still works."
        )
    if not HAVE_HEIF:
        print("NOTE: pillow-heif not installed - HEIC files get exact dedupe only.")

    new_paths = []
    pruned = unreadable = too_small = 0
    excluded = {d.lower() for d in cfg.exclude_dirs}

    def _walk_error(err: OSError) -> None:
        nonlocal unreadable
        unreadable += 1
        print(f"  unreadable: {err}")

    for root in args.sources:
        root = Path(root).expanduser().resolve()
        if not root.exists():
            print(f"skipping missing source: {root}")
            continue
        print(f"scanning {root} ...")
        for dirpath, dirnames, filenames in os.walk(root, onerror=_walk_error):
            keep = []
            for d in sorted(dirnames):
                if d.lower() in excluded:
                    pruned += 1
                    continue
                keep.append(d)
            dirnames[:] = keep  # in-place: this is what prunes the walk
            for fn in sorted(filenames):
                if fn.startswith("."):
                    continue
                ext = Path(fn).suffix.lower()
                kind = classify(ext, cfg)
                if kind == "other":
                    continue
                p = Path(dirpath) / fn
                try:
                    st = p.stat()
                except OSError as e:
                    unreadable += 1
                    print(f"  unreadable: {e}")
                    continue
                if st.st_size < cfg.min_size_bytes:
                    too_small += 1
                    continue
                sp = str(p)
                existing = conn.execute(
                    "SELECT size, mtime, content_hash FROM files WHERE source_path=?", (sp,)
                ).fetchone()
                if (
                    existing
                    and existing["content_hash"] is not None
                    and existing["size"] == st.st_size
                    and abs(existing["mtime"] - st.st_mtime) < 1
                ):
                    continue  # already scanned AND fingerprinted, unchanged
                conn.execute(
                    """INSERT INTO files(source_path, source_root, rel_path, size,
                                         mtime, ext, kind)
                       VALUES (?,?,?,?,?,?,?)
                       ON CONFLICT(source_path) DO UPDATE SET
                         size=excluded.size, mtime=excluded.mtime, status='scanned',
                         content_hash=NULL, phash=NULL, meta_read=0""",
                    (
                        sp,
                        str(root),
                        str(p.relative_to(root)),
                        st.st_size,
                        st.st_mtime,
                        ext,
                        kind,
                    ),
                )
                new_paths.append(sp)
    conn.commit()
    print(
        f"walk: pruned {pruned} dirs, skipped {unreadable} unreadable, {too_small} below min size"
    )
    print(f"{len(new_paths)} new/changed files to fingerprint")

    # content hashes
    for n, sp in enumerate(new_paths, 1):
        try:
            ch = content_hash(Path(sp))
            conn.execute("UPDATE files SET content_hash=? WHERE source_path=?", (ch, sp))
        except OSError as e:
            conn.execute(
                "UPDATE files SET status='error', error=? WHERE source_path=?", (str(e), sp)
            )
        if n % 500 == 0:
            print(f"  hashed {n}/{len(new_paths)}")
            conn.commit()
    conn.commit()

    # exif
    read_metadata_pending(conn, cfg)

    # perceptual hashes (images only)
    if HAVE_IMAGEHASH:
        phash_pending_images(conn)

    for sp in new_paths:
        row = conn.execute("SELECT id FROM files WHERE source_path=?", (sp,)).fetchone()
        log_action(conn, log_fh, run_id, row["id"], "scanned", sp)
    conn.commit()
    print("scan complete. Next: photoflow plan")


def read_metadata_pending(conn, cfg) -> int:
    """Read exiftool metadata for every manifest row still flagged meta_read=0.

    Manifest-driven for the same reasons as phash_pending_images: an IN(<paths>) clause
    overflows SQLite's 32766-variable cap on large imports, and an interrupted scan resumes
    here instead of losing the pass. meta_read is set to 1 per batch even when exiftool
    returned nothing for a path - otherwise a tag-less file is retried on every future run.
    """
    rows = conn.execute(
        "SELECT id, source_path, kind FROM files "
        "WHERE status='scanned' AND content_hash IS NOT NULL AND meta_read=0"
    ).fetchall()
    if not rows:
        return 0
    print(f"reading metadata (exiftool) for {len(rows)} files...")
    done = 0
    for i in range(0, len(rows), cfg.exiftool_batch):
        batch = rows[i : i + cfg.exiftool_batch]
        meta = exiftool_json([r["source_path"] for r in batch], cfg.exiftool_batch)
        for r in batch:
            rec = meta.get(r["source_path"], {})
            raw_date = (
                rec.get("DateTimeOriginal") or rec.get("CreateDate") or rec.get("MediaCreateDate")
            )
            conn.execute(
                "UPDATE files SET exif_date=?, camera=?, width=?, height=?, meta_read=1 WHERE id=?",
                (
                    str(raw_date) if raw_date else None,
                    rec.get("Model"),
                    rec.get("ImageWidth"),
                    rec.get("ImageHeight"),
                    r["id"],
                ),
            )
        conn.commit()
        done += len(batch)
        print(f"  metadata {done}/{len(rows)}")
    return done


def phash_pending_images(conn):
    """Hash every scanned image still missing a phash.

    Candidacy is read from the manifest rather than this run's path list: an
    IN(<paths>) clause overflows SQLite's 32766-variable cap on large imports,
    and manifest-driven selection lets an interrupted scan resume here (and
    backfills images scanned before ImageHash/pillow-heif were installed).
    """
    rows = conn.execute(
        "SELECT id, source_path FROM files "
        "WHERE kind='image' AND phash IS NULL AND status='scanned'"
    ).fetchall()
    for n, r in enumerate(rows, 1):
        ph = perceptual_hash(Path(r["source_path"]))
        if ph:
            conn.execute("UPDATE files SET phash=? WHERE id=?", (ph, r["id"]))
        if n % 500 == 0:
            print(f"  phashed {n}/{len(rows)}")
            conn.commit()
    conn.commit()
