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

from collections import defaultdict
from pathlib import Path

from photoflow.audit import log_action
from photoflow.enrich.page import render_assign_review


def _uri(dest_path) -> str | None:
    try:
        return Path(dest_path).as_uri() if dest_path else None
    except (ValueError, OSError):
        return None


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
        """SELECT fa.id, fa.embedding, fa.thumb, f.dest_path
           FROM faces fa JOIN files f ON f.id = fa.file_id
           WHERE fa.person_id IS NULL AND fa.ignored=0 AND fa.embedding IS NOT NULL"""
    ).fetchall()

    # pid -> list of (sim, thumb, uri) for every proposed face, for the review page + counts
    proposals: dict[int, list[tuple[float, str | None, str | None]]] = defaultdict(list)
    for r in rows:
        pid, sim = nearest_person(
            np.frombuffer(r["embedding"], dtype=np.float32), centroids, min_sim
        )
        if pid is None:
            continue
        proposals[pid].append((sim, r["thumb"], _uri(r["dest_path"])))
        if not dry:
            conn.execute(
                "UPDATE faces SET person_id=?, cluster_id=NULL, cluster_prob=NULL WHERE id=?",
                (pid, r["id"]),
            )
    if not dry:
        conn.commit()

    total = sum(len(v) for v in proposals.values())
    html_path = _write_review(conn, workdir, min_sim, total, proposals, pname)

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
    for pid in sorted(proposals, key=lambda k: len(proposals[k]), reverse=True):
        print(f"  {pname[pid]}: {len(proposals[pid])}")
    print(f"review page -> {html_path}")
    if not dry and total:
        print("Next: photoflow enrich cluster (regroup the rest) or enrich apply (write XMP).")


def _write_review(conn, workdir, min_sim, total, proposals, pname) -> Path:
    """Emit a static assign_review_sim<val>.html grouping every proposed face under its person
    (strongest first) with a strip of that person's known faces, so a human can confirm where a
    given threshold starts producing wrong matches."""
    persons = []
    for pid in sorted(proposals, key=lambda k: len(proposals[k]), reverse=True):
        cands = sorted(proposals[pid], key=lambda t: t[0], reverse=True)
        refs = [
            {"thumb": rr["thumb"], "uri": _uri(rr["dest_path"])}
            for rr in conn.execute(
                """SELECT fa.thumb, f.dest_path FROM faces fa JOIN files f ON f.id = fa.file_id
                   WHERE fa.person_id=? AND fa.thumb IS NOT NULL LIMIT 6""",
                (pid,),
            )
        ]
        persons.append(
            {
                "name": pname[pid],
                "count": len(cands),
                "weakest": min(s for s, _t, _u in cands),
                "refs": refs,
                "candidates": [{"sim": s, "thumb": t, "uri": u} for s, t, u in cands],
            }
        )
    html_path = workdir / f"assign_review_sim{min_sim:.2f}.html"
    html_path.write_text(render_assign_review(min_sim, total, persons), encoding="utf-8")
    return html_path
