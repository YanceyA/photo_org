"""enrich merge: fold duplicate / misspelled person names into one canonical person.

Typing a name before it exists in the autocomplete (or with different casing) creates a
separate `persons` row, so one real person ends up split across "Deidre Hough" / "Deirdre
Hough" / "Deirdre hough" - each with its own faces and its own (weaker) assign centroid.
This repoints every alias's faces to the canonical person and deletes the empty alias rows.
Re-run `enrich apply` afterwards to rewrite the library's people + region tags with the
canonical name.
"""

from __future__ import annotations

from datetime import datetime

from photoflow.audit import log_action


def _person_id(conn, name: str) -> int | None:
    row = conn.execute("SELECT id FROM persons WHERE name=?", (name,)).fetchone()
    return row["id"] if row else None


def cmd_enrich_merge(conn, workdir, run_id, log_fh, args, cfg):
    canonical = (args.canonical or "").strip()
    aliases = [(a or "").strip() for a in getattr(args, "aliases", [])]
    if not canonical:
        print("enrich merge: canonical name is empty.")
        return

    cid = _person_id(conn, canonical)
    if cid is None:  # the correct spelling may not exist yet (all rows are misspellings)
        cur = conn.execute(
            "INSERT INTO persons(name, created) VALUES (?,?)",
            (canonical, datetime.now().isoformat(timespec="seconds")),
        )
        cid = cur.lastrowid

    moved: list[tuple[str, int]] = []
    for alias in aliases:
        aid = _person_id(conn, alias)
        if aid is None or aid == cid:  # unknown name, or it's the canonical row itself
            continue
        n = conn.execute("UPDATE faces SET person_id=? WHERE person_id=?", (cid, aid)).rowcount
        conn.execute("DELETE FROM persons WHERE id=?", (aid,))
        moved.append((alias, n))
    conn.commit()

    total = sum(n for _a, n in moved)
    log_action(
        conn,
        log_fh,
        run_id,
        0,
        "enrich_merge",
        f"canonical={canonical} aliases={len(moved)} faces_moved={total}",
    )
    conn.commit()
    print(f"enrich merge: folded {len(moved)} alias(es) into '{canonical}', moved {total} face(s).")
    for alias, n in moved:
        print(f"  {alias} -> {canonical}: {n}")
    if total:
        print("Re-run `enrich apply` to rewrite the library people/region tags with this name.")
