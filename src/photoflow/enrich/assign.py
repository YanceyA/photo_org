"""enrich assign: propagate confirmed person labels to unassigned faces (Layer 0).

Once you've named clusters (enrich review -> apply), each person has a set of embeddings.
This builds a per-person centroid (mean embedding) and assigns any still-unassigned face
whose cosine similarity to a centroid clears a threshold straight to that person - mopping up
the burst-fragments and lone "noise" faces of people you've already named, without re-running
unsupervised clustering. It's the high-ROI semi-supervised step: every naming round makes the
next assign pass stronger. Only person_id IS NULL, non-ignored faces are touched, so confirmed
names and "not interested" faces are never disturbed.
"""

from __future__ import annotations

from collections import Counter

from photoflow.audit import log_action


def cmd_enrich_assign(conn, workdir, run_id, log_fh, args, cfg):
    import numpy as np

    from photoflow.enrich.clustering import nearest_person

    dry = getattr(args, "dry_run", False)
    min_sim = getattr(args, "min_sim", None)
    if min_sim is None:
        min_sim = cfg.enrich_auto_assign_threshold

    centroids: dict[int, np.ndarray] = {}
    pname: dict[int, str] = {}
    for p in conn.execute("SELECT id, name FROM persons"):
        embs = [
            np.frombuffer(f["embedding"], dtype=np.float32)
            for f in conn.execute(
                "SELECT embedding FROM faces WHERE person_id=? AND embedding IS NOT NULL",
                (p["id"],),
            )
        ]
        if embs:
            centroids[p["id"]] = np.mean(np.stack(embs), axis=0)
            pname[p["id"]] = p["name"]
    if not centroids:
        print("enrich assign: no named people yet - name some clusters first (enrich review).")
        return

    rows = conn.execute(
        "SELECT id, embedding FROM faces "
        "WHERE person_id IS NULL AND ignored=0 AND embedding IS NOT NULL"
    ).fetchall()

    by_person: Counter[str] = Counter()
    for r in rows:
        pid, _sim = nearest_person(
            np.frombuffer(r["embedding"], dtype=np.float32), centroids, min_sim
        )
        if pid is None:
            continue
        by_person[pname[pid]] += 1
        if not dry:
            conn.execute(
                "UPDATE faces SET person_id=?, cluster_id=NULL, cluster_prob=NULL WHERE id=?",
                (pid, r["id"]),
            )
    if not dry:
        conn.commit()

    total = sum(by_person.values())
    log_action(
        conn,
        log_fh,
        run_id,
        0,
        "enrich_assign",
        f"assigned={total} candidates={len(rows)} min_sim={min_sim} dry={dry}",
    )
    conn.commit()
    verb = "would assign" if dry else "assigned"
    print(f"enrich assign: {verb} {total} of {len(rows)} unassigned faces (sim >= {min_sim}).")
    for name, c in by_person.most_common():
        print(f"  {name}: {c}")
    if not dry and total:
        print("Next: photoflow enrich cluster (regroup the rest) or enrich apply (write XMP).")
