"""enrich status: a short summary of faces, clusters, people, and tags."""

from __future__ import annotations


def cmd_enrich_status(conn, workdir, run_id, log_fh, args, cfg):
    def count(sql, *params):
        return conn.execute(sql, params).fetchone()[0]

    faces = count("SELECT COUNT(*) FROM faces")
    assigned = count("SELECT COUNT(*) FROM faces WHERE person_id IS NOT NULL")
    persons = count("SELECT COUNT(*) FROM persons")
    clusters = count("SELECT COUNT(DISTINCT cluster_id) FROM faces WHERE cluster_id IS NOT NULL")
    auto = count("SELECT COUNT(*) FROM tags WHERE status='auto'")
    review = count("SELECT COUNT(*) FROM tags WHERE status='review'")
    applied = count("SELECT COUNT(*) FROM enrich_state WHERE applied=1")
    enriched = count("SELECT COUNT(*) FROM enrich_state")

    print("enrich status:")
    print(f"  enriched files : {enriched}")
    print(f"  faces          : {faces} ({assigned} assigned to {persons} named people)")
    print(f"  open clusters  : {clusters} (unassigned faces awaiting a name)")
    print(f"  content tags   : {auto} auto-applied, {review} awaiting review")
    print(f"  applied files  : {applied} written to the library")
