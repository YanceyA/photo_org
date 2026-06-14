"""enrich review: export the interactive enrich_review.html + faces.csv + tags.csv.

Mirrors the top-level review command. People clusters and review-band tags are shaped into
the page payloads (enrich/page.py); decisions carry forward by id like decisions.csv.
"""

from __future__ import annotations

import csv
from collections import defaultdict

from photoflow.audit import log_action
from photoflow.enrich.imgutil import make_thumb
from photoflow.enrich.page import (
    build_people_payload,
    build_tags_payload,
    face_rows,
    render_page,
    tag_rows,
    write_faces_csv,
    write_tags_csv,
)


def _read_prior_faces(path) -> dict:
    prior: dict = {}
    if path.exists():
        with open(path, newline="", encoding="utf-8") as f:
            for row in csv.DictReader(f):
                if (row.get("decision") or "").strip() or (row.get("person") or "").strip():
                    prior[row["face_id"]] = row
    return prior


def _read_prior_tags(path) -> dict:
    prior: dict = {}
    if path.exists():
        with open(path, newline="", encoding="utf-8") as f:
            for row in csv.DictReader(f):
                if (row.get("decision") or "").strip():
                    prior[(str(row["file_id"]), row["tag"])] = row
    return prior


def cmd_enrich_review(conn, workdir, run_id, log_fh, args, cfg):
    import numpy as np

    from photoflow.enrich.clustering import nearest_person

    # Suggestions for the name field: DB persons + any names already typed into faces.csv, so
    # they survive Save -> re-review even before `enrich apply` writes them to the persons table.
    prior_faces = _read_prior_faces(workdir / "faces.csv")
    db_names = {r["name"] for r in conn.execute("SELECT name FROM persons")}
    csv_names = {(p.get("person") or "").strip() for p in prior_faces.values()}
    csv_names.discard("")
    persons = sorted(db_names | csv_names)

    # person centroids (mean embedding) for nearest-person suggestions on unassigned faces
    centroids: dict[int, np.ndarray] = {}
    pname: dict[int, str] = {}
    for p in conn.execute("SELECT id, name FROM persons"):
        pname[p["id"]] = p["name"]
        embs = [
            np.frombuffer(f["embedding"], dtype=np.float32)
            for f in conn.execute(
                "SELECT embedding FROM faces WHERE person_id=? AND embedding IS NOT NULL",
                (p["id"],),
            )
        ]
        if embs:
            centroids[p["id"]] = np.mean(np.stack(embs), axis=0)

    clusters: dict = defaultdict(list)
    noise: list = []
    for r in conn.execute(
        """SELECT fa.id face_id, fa.file_id, fa.cluster_id, fa.cluster_prob, fa.thumb,
                  fa.embedding, f.dest_path
           FROM faces fa JOIN files f ON f.id = fa.file_id
           WHERE fa.person_id IS NULL ORDER BY fa.cluster_id"""
    ):
        sug = ""
        if centroids and r["embedding"] is not None:
            pid, _sim = nearest_person(
                np.frombuffer(r["embedding"], dtype=np.float32),
                centroids,
                cfg.enrich_assign_threshold,
            )
            if pid is not None:
                sug = pname[pid]
        m = {
            "face_id": r["face_id"],
            "file_id": r["file_id"],
            "source_path": r["dest_path"],
            "thumb": r["thumb"],
            "cluster_prob": r["cluster_prob"] or 0.0,
            "suggested_person": sug,
        }
        if r["cluster_id"] is not None:
            clusters[r["cluster_id"]].append(m)
        else:
            noise.append(m)

    tagthumbs = workdir / "tagthumbs"
    tag_items: list = []
    for r in conn.execute(
        """SELECT t.file_id, t.tag, t.source, t.score, f.dest_path
           FROM tags t JOIN files f ON f.id = t.file_id WHERE t.status='review' ORDER BY t.tag"""
    ):
        thumb_rel = None
        if r["dest_path"]:
            tagthumbs.mkdir(exist_ok=True)
            try:
                make_thumb(r["dest_path"], tagthumbs / f"{r['file_id']}.jpg")
                thumb_rel = f"tagthumbs/{r['file_id']}.jpg"
            except Exception:
                thumb_rel = None
        tag_items.append(
            {
                "file_id": r["file_id"],
                "tag": r["tag"],
                "source": r["source"],
                "score": r["score"],
                "suggestion": "review",
                "thumb": thumb_rel,
                "source_path": r["dest_path"],
            }
        )
    auto_items = [
        {"file_id": r["file_id"], "tag": r["tag"], "suggestion": "auto"}
        for r in conn.execute("SELECT file_id, tag FROM tags WHERE status='auto'")
    ]

    if not clusters and not noise and not tag_items:
        print("enrich review: nothing to review yet (run enrich scan + enrich cluster first).")
        return

    f_rows = face_rows(clusters, noise, prior_faces)
    t_rows = tag_rows(tag_items, _read_prior_tags(workdir / "tags.csv"))
    write_faces_csv(workdir / "faces.csv", f_rows)
    write_tags_csv(workdir / "tags.csv", t_rows)

    wk = str(workdir.resolve())
    people_payload = build_people_payload(
        clusters, noise, f_rows, persons, wk, cfg.enrich_cluster_prob_floor
    )
    tags_payload = build_tags_payload(tag_items + auto_items, t_rows, wk)
    html_path = workdir / "enrich_review.html"
    html_path.write_text(render_page(people_payload, tags_payload), encoding="utf-8")

    log_action(
        conn,
        log_fh,
        run_id,
        0,
        "enrich_review",
        f"clusters={len(clusters)} noise={len(noise)} review_tags={len(tag_items)}",
    )
    conn.commit()
    print(f"enrich_review.html -> {html_path}")
    print(f"faces.csv -> {workdir / 'faces.csv'}")
    print(f"tags.csv  -> {workdir / 'tags.csv'}")
    print(
        "Open the HTML, name clusters + confirm edge-case tags, save the CSVs, then: enrich apply"
    )
