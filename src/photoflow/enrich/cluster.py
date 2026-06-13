"""enrich cluster: (re)group unassigned faces into per-person clusters with HDBSCAN.

Only faces with person_id IS NULL are clustered; faces already assigned to a named person
are left untouched, so human-confirmed names carry forward across re-runs automatically.
cluster_id / cluster_prob are ephemeral and recomputed every run.
"""

from __future__ import annotations

from photoflow.audit import log_action
from photoflow.enrich.clustering import cluster_embeddings


def cmd_enrich_cluster(conn, workdir, run_id, log_fh, args, cfg):
    import numpy as np

    # reset ephemeral cluster state for everything still unassigned
    conn.execute("UPDATE faces SET cluster_id=NULL, cluster_prob=NULL WHERE person_id IS NULL")
    rows = conn.execute(
        "SELECT id, embedding FROM faces WHERE person_id IS NULL AND embedding IS NOT NULL"
    ).fetchall()

    # Guard against malformed embedding BLOBs (e.g. a physically damaged DB): frombuffer
    # needs a multiple-of-4 byte length and np.stack needs a consistent dimension. Skip the
    # bad ones with a clear count rather than crashing on an opaque numpy error.
    vecs: list[tuple[int, np.ndarray]] = []
    for r in rows:
        blob = r["embedding"]
        if blob and len(blob) % 4 == 0:
            vecs.append((r["id"], np.frombuffer(blob, dtype=np.float32)))
    if vecs:
        from collections import Counter

        dim = Counter(v.shape[0] for _id, v in vecs).most_common(1)[0][0]
        vecs = [(fid, v) for fid, v in vecs if v.shape[0] == dim]
    skipped = len(rows) - len(vecs)
    if skipped:
        print(f"enrich cluster: skipped {skipped} face(s) with malformed embeddings.")

    if len(vecs) < cfg.enrich_min_cluster_size:
        conn.commit()
        print(
            f"enrich cluster: only {len(vecs)} usable unassigned faces - need at least "
            f"{cfg.enrich_min_cluster_size} to form a cluster."
        )
        return

    embs = np.stack([v for _id, v in vecs])
    labels, probs, _medoids = cluster_embeddings(
        embs,
        min_cluster_size=cfg.enrich_min_cluster_size,
        min_samples=cfg.enrich_min_samples or None,
    )
    for (face_id, _v), label, prob in zip(vecs, labels, probs):  # noqa: B905 (equal lengths)
        conn.execute(
            "UPDATE faces SET cluster_id=?, cluster_prob=? WHERE id=?",
            (int(label) if label != -1 else None, float(prob), face_id),
        )
    conn.commit()

    n_clusters = len({int(lbl) for lbl in labels if lbl != -1})
    n_noise = int(sum(1 for lbl in labels if lbl == -1))
    log_action(
        conn,
        log_fh,
        run_id,
        0,
        "enrich_cluster",
        f"faces={len(vecs)} clusters={n_clusters} noise={n_noise} skipped={skipped}",
    )
    conn.commit()
    print(
        f"enrich cluster complete: {n_clusters} clusters over {len(vecs)} unassigned faces "
        f"({n_noise} unclustered). Next: photoflow enrich review"
    )
