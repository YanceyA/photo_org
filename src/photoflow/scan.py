"""Scan command: fingerprint source folders into the manifest."""

from __future__ import annotations

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
            kind = classify(ext, cfg)
            if kind == "other":
                continue
            sp = str(p)
            existing = conn.execute(
                "SELECT size, mtime FROM files WHERE source_path=?", (sp,)
            ).fetchone()
            st = p.stat()
            if (
                existing
                and existing["size"] == st.st_size
                and abs(existing["mtime"] - st.st_mtime) < 1
            ):
                continue  # already scanned, unchanged
            conn.execute(
                """INSERT INTO files(source_path, source_root, rel_path, size,
                                     mtime, ext, kind)
                   VALUES (?,?,?,?,?,?,?)
                   ON CONFLICT(source_path) DO UPDATE SET
                     size=excluded.size, mtime=excluded.mtime, status='scanned',
                     content_hash=NULL, phash=NULL""",
                (sp, str(root), str(p.relative_to(root)), st.st_size, st.st_mtime, ext, kind),
            )
            new_paths.append(sp)
    conn.commit()
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
    print("reading metadata (exiftool)...")
    meta = exiftool_json(new_paths, cfg.exiftool_batch)
    for sp, rec in meta.items():
        raw_date = (
            rec.get("DateTimeOriginal") or rec.get("CreateDate") or rec.get("MediaCreateDate")
        )
        conn.execute(
            "UPDATE files SET exif_date=?, camera=?, width=?, height=? WHERE source_path=?",
            (
                str(raw_date) if raw_date else None,
                rec.get("Model"),
                rec.get("ImageWidth"),
                rec.get("ImageHeight"),
                sp,
            ),
        )
    conn.commit()

    # perceptual hashes (images only)
    if HAVE_IMAGEHASH:
        rows = (
            conn.execute(
                "SELECT id, source_path FROM files WHERE kind='image' AND phash IS NULL "  # noqa: UP031
                "AND status='scanned' AND source_path IN (%s)" % ",".join("?" * len(new_paths)),
                new_paths,
            ).fetchall()
            if new_paths
            else []
        )
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
