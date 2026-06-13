"""enrich scan: detect faces + embeddings and content tags for copied library files.

Incremental like the top-level scan: a file whose enrich_state already records both
faces_done and tags_done is skipped. Degrades gracefully - missing face stack skips faces,
missing tagger skips tags, both missing exits with an install hint.
"""

from __future__ import annotations

import json
from datetime import datetime

from photoflow.audit import log_action
from photoflow.enrich import deps
from photoflow.enrich import faces as faces_mod
from photoflow.enrich import tagger as tagger_mod
from photoflow.enrich.faces import face_crop
from photoflow.enrich.imgutil import open_rgb
from photoflow.enrich.tagger import classify_tag


def cmd_enrich_scan(conn, workdir, run_id, log_fh, args, cfg):
    rows = conn.execute(
        """SELECT f.id, f.dest_path, COALESCE(e.faces_done, 0) fd, COALESCE(e.tags_done, 0) td
           FROM files f LEFT JOIN enrich_state e ON e.file_id = f.id
           WHERE f.status = 'copied' AND f.kind = 'image' AND f.dest_path IS NOT NULL
             AND (e.file_id IS NULL OR e.faces_done = 0 OR e.tags_done = 0)"""
    ).fetchall()
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

    faces_dir = workdir / "faces"
    faces_dir.mkdir(exist_ok=True)
    n_files = n_faces = n_tags = 0

    for r in rows:
        try:
            im = open_rgb(r["dest_path"])
        except Exception as e:
            log_action(conn, log_fh, run_id, r["id"], "enrich_open_error", str(e))
            continue

        if detector is not None and not r["fd"]:
            import numpy as np

            arr = np.asarray(im)
            for fc in detector.detect(arr):
                cur = conn.execute(
                    "INSERT INTO faces(file_id, bbox, det_score, embedding, img_w, img_h)"
                    " VALUES (?,?,?,?,?,?)",
                    (
                        r["id"],
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
                    conn.execute(
                        "UPDATE faces SET thumb=? WHERE id=?", (f"faces/{face_id}.jpg", face_id)
                    )
                except Exception:
                    pass
                n_faces += 1

        if tagger is not None and not r["td"]:
            for tag, score in tagger.tag(im):
                st = classify_tag(score, cfg.tag_score_accept, cfg.tag_score_review)
                if st is None:
                    continue
                conn.execute(
                    "INSERT OR IGNORE INTO tags(file_id, tag, source, score, status)"
                    " VALUES (?,?,?,?,?)",
                    (r["id"], tag, getattr(tagger, "source", "clip"), score, st),
                )
                n_tags += 1

        conn.execute(
            "INSERT INTO enrich_state(file_id, faces_done, tags_done, ts) VALUES (?,?,?,?) "
            "ON CONFLICT(file_id) DO UPDATE SET faces_done=MAX(faces_done, excluded.faces_done), "
            "tags_done=MAX(tags_done, excluded.tags_done), ts=excluded.ts",
            (
                r["id"],
                1 if detector is not None else 0,
                1 if tagger is not None else 0,
                datetime.now().isoformat(timespec="seconds"),
            ),
        )
        n_files += 1
        if n_files % 200 == 0:
            conn.commit()
            print(f"  enriched {n_files}...")

    conn.commit()
    log_action(
        conn, log_fh, run_id, 0, "enrich_scan", f"files={n_files} faces={n_faces} tags={n_tags}"
    )
    conn.commit()
    print(
        f"enrich scan complete: {n_files} files, {n_faces} faces, {n_tags} tags. "
        "Next: photoflow enrich cluster"
    )
