"""Scan command: fingerprint source folders into the manifest."""

from __future__ import annotations

import os
import sys
from pathlib import Path

from photoflow.audit import log_action
from photoflow.dates import MIN_YEAR_DEFAULT, parse_exif_date
from photoflow.exiftool import exiftool_available, exiftool_json
from photoflow.hashing import HAVE_HEIF, HAVE_IMAGEHASH, content_hash, perceptual_hash
from photoflow.models import classify


def _like_prefix(prefix: str) -> str:
    """SQL LIKE pattern matching everything under `prefix`, with wildcards escaped.

    Windows source roots are full of '_' (H:\\_photos_backup) and '_' is a LIKE wildcard,
    so the pattern is escaped and used with ESCAPE '~'.
    """
    esc = prefix.replace("~", "~~").replace("%", "~%").replace("_", "~_")
    return esc + "%"


def _refresh_meta(conn, args, cfg) -> None:
    """Re-run only the exiftool pass over rows already in the manifest.

    Repairs metadata for files whose bytes never changed but whose read was wrong (e.g. the
    video dates fixed in this lane). Never re-hashes, never changes `status`, and applies to
    `copied` rows too - `plan` then recomputes date_taken for them (planner.py:30,132,142)
    and `refile` moves the library file to match.
    """
    where, params = [], []
    kinds = args.kind or []
    if kinds:
        where.append("kind IN ({})".format(",".join("?" * len(kinds))))
        params += kinds
    prefixes = [str(Path(s).expanduser().resolve()) for s in (args.sources or [])]
    if prefixes:
        where.append(" OR ".join(["source_path LIKE ? ESCAPE '~'"] * len(prefixes)))
        params += [_like_prefix(p) for p in prefixes]
    sql = "UPDATE files SET meta_read=0"
    if where:
        sql += " WHERE " + " AND ".join(f"({w})" for w in where)
    n = conn.execute(sql, params).rowcount
    conn.commit()
    print(f"{n} manifest rows marked for metadata refresh")
    read_metadata_pending(conn, cfg)
    print("refresh complete. Next: photoflow plan")


def cmd_scan(conn, workdir, run_id, log_fh, args, cfg):
    if not exiftool_available():
        sys.exit("exiftool not found on PATH - install it first (see README).")
    if getattr(args, "refresh_meta", False):
        _refresh_meta(conn, args, cfg)
        return
    if not args.sources:
        sys.exit("scan: give at least one source folder, or use --refresh-meta [PREFIX ...]")
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
                         content_hash=NULL, phash=NULL, meta_read=0,
                         exif_date=NULL, camera=NULL, width=NULL, height=NULL""",
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


#: Capture-date tags in preference order. QuickTime CreationDate first: it is tz-aware and
#: capture-local (iPhones write it), so it beats the UTC-converted CreateDate for video.
DATE_TAGS = ("CreationDate", "DateTimeOriginal", "CreateDate", "MediaCreateDate")


def _first_parseable_date(rec: dict, min_year: int = MIN_YEAR_DEFAULT) -> str | None:
    """Pick the first DATE_TAGS value that actually parses, as the raw exiftool string.

    Preference alone is not enough: wild files carry "0000:00:00 00:00:00" and similar junk,
    and a garbage CreationDate must not shadow a good DateTimeOriginal. If nothing parses, the
    first *present* value is returned anyway so plan's existing bad-date handling (and anyone
    reading the manifest) still sees what the file claimed, rather than a silent NULL.

    `min_year` is threaded through to `parse_exif_date` so a configured `photoflow.toml`
    (e.g. `min_year = 1970`) picks the same tag `plan` would accept.
    """
    present = [str(rec[t]) for t in DATE_TAGS if rec.get(t)]
    for v in present:
        if parse_exif_date(v, min_year) is not None:
            return v
    return present[0] if present else None


def read_metadata_pending(conn, cfg) -> int:
    """Read exiftool metadata for every manifest row still flagged meta_read=0.

    Manifest-driven for the same reasons as phash_pending_images: an IN(<paths>) clause
    overflows SQLite's 32766-variable cap on large imports, and an interrupted scan resumes
    here instead of losing the pass. exiftool emits a record (with SourceFile) even for a
    tag-less file, so those are marked done per batch instead of being retried forever; a path
    with no record at all is missing/offline and keeps whatever the manifest already knows.
    """
    rows = conn.execute(
        "SELECT id, source_path, kind FROM files "
        "WHERE content_hash IS NOT NULL AND meta_read=0 "
        "AND status NOT IN ('error','skipped_manual')"
    ).fetchall()
    if not rows:
        return 0
    print(f"reading metadata (exiftool) for {len(rows)} files...")
    done = 0
    for i in range(0, len(rows), cfg.exiftool_batch):
        batch = rows[i : i + cfg.exiftool_batch]
        # -fast2 is a large win for JPEG/RAW but returns NOTHING for trailing-moov QuickTime,
        # so video is read in a second, slower call.
        video = [r["source_path"] for r in batch if r["kind"] == "video"]
        other = [r["source_path"] for r in batch if r["kind"] != "video"]
        meta_other = exiftool_json(other, cfg.exiftool_batch, fast=True)
        meta_video = exiftool_json(video, cfg.exiftool_batch, fast=False)
        for r in batch:
            # Emptiness is judged per invocation, never across the two: the sub-batches are
            # <= cfg.exiftool_batch (exiftool_json's own batch size) so each call is exactly
            # one invocation, and a failed video call must not ride on a successful image one.
            src = meta_video if r["kind"] == "video" else meta_other
            rec = src.get(r["source_path"])
            if rec is None:
                # No record: the file is missing/offline, or the call failed. Never clobber
                # what the manifest already knows. If this invocation returned records the
                # path is individually unreadable -> mark it done rather than retry forever;
                # if it returned nothing, leave meta_read=0 so a transient failure retries.
                if src:
                    conn.execute("UPDATE files SET meta_read=1 WHERE id=?", (r["id"],))
                continue
            raw_date = _first_parseable_date(rec, cfg.min_year)
            conn.execute(
                "UPDATE files SET exif_date=?, camera=?, width=?, height=?, meta_read=1 WHERE id=?",
                (
                    raw_date,
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
