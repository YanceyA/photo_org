"""enrich scan: detect faces + embeddings and content tags for copied library files.

Incremental like the top-level scan: a file whose enrich_state already records both
faces_done and tags_done is skipped. Degrades gracefully - missing face stack skips faces,
missing tagger skips tags, both missing exits with an install hint.
"""

from __future__ import annotations

import json
import time
from datetime import datetime

from photoflow.audit import log_action
from photoflow.enrich import deps
from photoflow.enrich import faces as faces_mod
from photoflow.enrich import tagger as tagger_mod
from photoflow.enrich.faces import face_crop
from photoflow.enrich.imgutil import open_rgb
from photoflow.enrich.tagger import classify_tag

# A file that fails this many times is left alone until something changes: a transient CUDA
# OOM must be retried on the next run, a truncated JPEG must not burn a model call forever.
MAX_ERRORS = 3
COMMIT_EVERY = 20  # ~20 files of inference is the most we're willing to lose to a crash


def _bump_errors(conn, file_id: int) -> None:
    """Count one failed attempt. MAX_ERRORS strikes and the candidate query drops the file."""
    conn.execute(
        "INSERT INTO enrich_state(file_id, errors, ts) VALUES (?,1,?) "
        "ON CONFLICT(file_id) DO UPDATE SET errors=COALESCE(errors,0)+1, ts=excluded.ts",
        (file_id, datetime.now().isoformat(timespec="seconds")),
    )


def _store_faces(conn, file_id: int, im, dets, cfg, faces_dir) -> int:
    n = 0
    for fc in dets:
        cur = conn.execute(
            "INSERT INTO faces(file_id, bbox, det_score, embedding, img_w, img_h)"
            " VALUES (?,?,?,?,?,?)",
            (
                file_id,
                json.dumps([float(v) for v in fc["bbox"]]),
                fc["det_score"],
                fc["embedding"].tobytes(),
                im.width,
                im.height,
            ),
        )
        face_id = cur.lastrowid
        try:
            face_crop(im, fc["bbox"], cfg.face_crop_pad).save(
                faces_dir / f"{face_id}.jpg", "JPEG", quality=80
            )
            conn.execute("UPDATE faces SET thumb=? WHERE id=?", (f"faces/{face_id}.jpg", face_id))
        except Exception:
            pass
        n += 1
    return n


def _store_tags(conn, file_id: int, items, tagger, cfg, blacklist) -> int:
    n = 0
    for tag, score in items:
        if tag in blacklist:
            continue
        st = classify_tag(score, cfg.tag_score_accept, cfg.tag_score_review)
        if st is None:
            continue
        conn.execute(
            "INSERT OR IGNORE INTO tags(file_id, tag, source, score, status) VALUES (?,?,?,?,?)",
            (file_id, tag, getattr(tagger, "source", "clip"), score, st),
        )
        n += 1
    return n


def cmd_enrich_scan(conn, workdir, run_id, log_fh, args, cfg):
    rows = conn.execute(
        """SELECT f.id, f.dest_path, COALESCE(e.faces_done, 0) fd, COALESCE(e.tags_done, 0) td
           FROM files f LEFT JOIN enrich_state e ON e.file_id = f.id
           WHERE f.status = 'copied' AND f.kind = 'image' AND f.dest_path IS NOT NULL
             AND (e.file_id IS NULL OR e.faces_done = 0 OR e.tags_done = 0)
             AND COALESCE(e.errors, 0) < ?""",
        (MAX_ERRORS,),
    ).fetchall()
    skipped_errors = conn.execute(
        """SELECT COUNT(*) c FROM files f JOIN enrich_state e ON e.file_id = f.id
           WHERE f.status = 'copied' AND f.kind = 'image' AND f.dest_path IS NOT NULL
             AND (e.faces_done = 0 OR e.tags_done = 0) AND COALESCE(e.errors, 0) >= ?""",
        (MAX_ERRORS,),
    ).fetchone()["c"]
    if skipped_errors:
        print(
            f"enrich scan: skipping {skipped_errors} file(s) with repeated errors "
            f"(>= {MAX_ERRORS} failures)."
        )
    if not rows:
        print("enrich scan: nothing to do (all copied images already enriched).")
        return

    detector = faces_mod.FaceDetector(cfg) if deps.HAVE_FACES else None
    tagger = tagger_mod.build_tagger(cfg, workdir)
    if detector is None:
        print("NOTE: face stack unavailable - skipping faces (pip install 'photoflow[enrich]').")
    if tagger is None:
        print("NOTE: no tagger available - skipping content tags (see README enrich setup).")
    if detector is None and tagger is None:
        print("enrich scan: install photoflow[enrich] to detect faces or tag content.")
        return

    blacklist = {r["tag"] for r in conn.execute("SELECT tag FROM tag_blacklist")}

    faces_dir = workdir / "faces"
    faces_dir.mkdir(exist_ok=True)
    total = len(rows)
    print(f"enrich scan: {total} files to process")
    n_files = n_faces = n_tags = n_errors = 0
    n_attempted = 0
    t0 = time.monotonic()

    for r in rows:
        # Counted here, before open_rgb, so a run dominated by unreadable files still commits
        # and prints progress periodically instead of never hitting the COMMIT_EVERY check below
        # (which used to be keyed off n_files, only incremented after a file fully succeeded).
        n_attempted += 1
        try:
            im = open_rgb(r["dest_path"])
        except Exception as e:
            log_action(conn, log_fh, run_id, r["id"], "enrich_open_error", str(e)[:200])
            _bump_errors(conn, r["id"])
            n_errors += 1
        else:
            faces_ok = detector is None or bool(r["fd"])
            tags_ok = tagger is None or bool(r["td"])
            errored = False

            if detector is not None and not r["fd"]:
                import numpy as np

                try:
                    # materialize inside the guard: a generator would raise later, outside it
                    dets = list(detector.detect(np.asarray(im)))
                except Exception as e:
                    log_action(conn, log_fh, run_id, r["id"], "enrich_detect_error", str(e)[:200])
                    errored = True
                else:
                    n_faces += _store_faces(conn, r["id"], im, dets, cfg, faces_dir)
                    faces_ok = True

            if tagger is not None and not r["td"]:
                try:
                    items = list(tagger.tag(im))
                except Exception as e:
                    log_action(conn, log_fh, run_id, r["id"], "enrich_tag_error", str(e)[:200])
                    errored = True
                else:
                    n_tags += _store_tags(conn, r["id"], items, tagger, cfg, blacklist)
                    tags_ok = True

            if errored:
                n_errors += 1
                _bump_errors(conn, r["id"])

            conn.execute(
                "INSERT INTO enrich_state(file_id, faces_done, tags_done, ts) VALUES (?,?,?,?) "
                "ON CONFLICT(file_id) DO UPDATE SET"
                " faces_done=MAX(faces_done, excluded.faces_done),"
                " tags_done=MAX(tags_done, excluded.tags_done), ts=excluded.ts",
                (
                    r["id"],
                    1 if (detector is not None and faces_ok) else 0,
                    1 if (tagger is not None and tags_ok) else 0,
                    datetime.now().isoformat(timespec="seconds"),
                ),
            )
            n_files += 1

        if n_attempted % COMMIT_EVERY == 0:
            conn.commit()
            rate = n_attempted / max(time.monotonic() - t0, 1e-6) * 60
            print(f"  attempted {n_attempted}/{total} ({rate:.1f} files/min)", flush=True)

    conn.commit()
    log_action(
        conn,
        log_fh,
        run_id,
        0,
        "enrich_scan",
        f"files={n_files} faces={n_faces} tags={n_tags} errors={n_errors}",
    )
    conn.commit()
    print(
        f"enrich scan complete: {n_files} files, {n_faces} faces, {n_tags} tags, "
        f"{n_errors} errors. Next: photoflow enrich cluster"
    )
