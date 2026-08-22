"""enrich merge: fold duplicate / misspelled person names into one canonical person.

Typing a name before it exists in the autocomplete (or with different casing) creates a
separate `persons` row, so one real person ends up split across "Deidre Hough" / "Deirdre
Hough" / "Deirdre hough" - each with its own faces and its own (weaker) assign centroid.
This repoints every alias's faces to the canonical person, strips the old name out of the
already-written library files, and deletes the empty alias rows.

Ordering matters: `enrich apply` only ever removes a name from PersonInImage /
HierarchicalSubject while that name still has a `persons` row (a name photoflow does not
own is foreign and is preserved forever - H11). So the strip must happen BEFORE the alias
row is deleted, and an alias whose strip failed keeps its row so a later run can still
finish the job.
"""

from __future__ import annotations

import csv
from datetime import datetime
from pathlib import Path

from photoflow.audit import log_action
from photoflow.enrich.regions import keyword_remove_argfile_lines
from photoflow.exiftool import exiftool_apply_argfile, exiftool_available
from photoflow.xmp import EMBED_EXT

# Per-file strip failures printed in full before collapsing to a "... and N more" line.
MAX_FAILURE_REPORTS = 10


def _person_id(conn, name: str) -> int | None:
    row = conn.execute("SELECT id FROM persons WHERE name=?", (name,)).fetchone()
    return row["id"] if row else None


def _strip_alias_keywords(
    conn, log_fh, run_id, alias: str, touched: dict[int, tuple[str, str]], cfg
) -> tuple[int, list[tuple[int, str]]]:
    """Delete one merged-away name from the library files' keyword lists.

    enrich apply only ever UNIONS keywords, so a renamed person lingers in dc:Subject /
    IPTC:Keywords / PersonInImage / HierarchicalSubject on every already-applied file (H12).
    exiftool's '-=' removes that exact list value and is a no-op when it's absent, so this
    strips only the named values and never disturbs other keywords. -P keeps the mtime.

    Returns (files stripped OK, [(file_id, dest) that could not be written]). Like apply, a
    failed batch is re-run one block at a time: exiftool keeps going after a bad file, so a
    non-zero rc only means "at least one block failed".
    """
    remove = keyword_remove_argfile_lines(
        [alias], iptc=cfg.write_iptc_keywords, people_prefix=cfg.people_keyword_prefix
    )
    blocks: list[tuple[int, str, list[str]]] = []  # (file_id, dest, argfile lines)
    for file_id, (dest, ext) in touched.items():
        target = dest if (ext or "").lower() in EMBED_EXT else dest + ".xmp"
        # A sidecar apply never created holds no keywords - nothing to strip, and asking
        # exiftool to edit a missing file would be a spurious failure that keeps the row.
        if not Path(target).exists():
            continue
        blocks.append((file_id, dest, ["-P", "-overwrite_original", *remove, target, "-execute"]))
    if not blocks:
        return 0, []

    lines: list[str] = []
    for _fid, _dest, block in blocks:
        lines += block
    res = exiftool_apply_argfile(lines)
    if res.returncode == 0:
        return len(blocks), []

    print(
        f"  exiftool rc={res.returncode} while stripping '{alias}': retrying its"
        f" {len(blocks)} file(s) individually to find the bad one(s)..."
    )
    ok = 0
    failed: list[tuple[int, str]] = []
    reported = 0
    for file_id, dest, block in blocks:
        one = exiftool_apply_argfile(block)
        if one.returncode == 0:
            ok += 1
            continue
        failed.append((file_id, dest))
        head = "; ".join((one.stderr or "").strip().splitlines()[:3])
        log_action(
            conn, log_fh, run_id, file_id, "enrich_merge_strip_error", f"{alias}: {dest}: {head}"
        )
        if reported < MAX_FAILURE_REPORTS:
            reported += 1
            print(f"  FAILED to strip '{alias}' (rc={one.returncode}): {dest}")
            for err in (one.stderr or "").strip().splitlines()[:3]:
                print(f"    {err}")
    if len(failed) > reported:
        print(f"  ... and {len(failed) - reported} more strip failure(s) not shown.")
    return ok, failed


def _rewrite_faces_csv(path, mapping: dict[str, str]) -> int:
    """Point alias names in the workdir faces.csv at the canonical name.

    faces.csv is a decision overlay that apply replays; leaving the alias in it means the
    page and the DB disagree about who this face is (and, before the apply guard, meant the
    next apply re-created the alias - R8).
    """
    if not path.exists() or not mapping:
        return 0
    with open(path, newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        columns = list(reader.fieldnames or [])
        rows = list(reader)
    n = 0
    for r in rows:
        name = (r.get("person") or "").strip()
        if name in mapping:
            r["person"] = mapping[name]
            n += 1
    if n:
        with open(path, "w", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=columns)
            w.writeheader()
            w.writerows(rows)
    return n


def cmd_enrich_merge(conn, workdir, run_id, log_fh, args, cfg):
    canonical = (args.canonical or "").strip()
    aliases = [(a or "").strip() for a in getattr(args, "aliases", [])]
    if not canonical:
        print("enrich merge: canonical name is empty.")
        return
    if not exiftool_available():
        # Abort before touching the DB: deleting the alias row without stripping the name
        # from the files would make that name foreign, and then nothing (not even
        # `enrich apply --all`) could ever remove it again.
        print(
            "enrich merge: exiftool not found on PATH - the old name could not be stripped"
            " from the library files; nothing changed."
        )
        return

    cid = _person_id(conn, canonical)
    if cid is None:  # the correct spelling may not exist yet (all rows are misspellings)
        cur = conn.execute(
            "INSERT INTO persons(name, created) VALUES (?,?)",
            (canonical, datetime.now().isoformat(timespec="seconds")),
        )
        cid = cur.lastrowid

    # Pass 1: which files carry each alias? Collect BEFORE anything is repointed - once
    # person_id is canonical the alias is untraceable.
    plans: list[tuple[str, int, dict[int, tuple[str, str]]]] = []
    for alias in aliases:
        aid = _person_id(conn, alias)
        if aid is None or aid == cid:  # unknown name, or it's the canonical row itself
            continue
        touched: dict[int, tuple[str, str]] = {}  # file_id -> (dest_path, ext)
        for row in conn.execute(
            """SELECT DISTINCT f.id, f.dest_path, f.ext FROM faces fa
               JOIN files f ON f.id = fa.file_id
               WHERE fa.person_id=? AND f.dest_path IS NOT NULL""",
            (aid,),
        ):
            touched[row["id"]] = (row["dest_path"], row["ext"] or "")
        plans.append((alias, aid, touched))

    # Pass 2: strip the old name from the files while the alias is still owned.
    stripped = 0
    failures: dict[str, int] = {}
    for alias, _aid, touched in plans:
        if not touched:
            continue
        ok, failed = _strip_alias_keywords(conn, log_fh, run_id, alias, touched, cfg)
        stripped += ok
        if failed:
            failures[alias] = len(failed)

    # Pass 3: repoint the faces, queue a rewrite, and retire the alias rows.
    moved: list[tuple[str, int]] = []
    kept: list[tuple[str, int]] = []
    for alias, aid, touched in plans:
        n = conn.execute("UPDATE faces SET person_id=? WHERE person_id=?", (cid, aid)).rowcount
        for file_id in touched:  # one statement per id: an IN(...) list would trip the
            conn.execute(  # 32766-variable limit on a large merge
                "UPDATE enrich_state SET applied_sig=NULL WHERE file_id=?", (file_id,)
            )
        if alias in failures:
            # Keep the row: it is what makes the name photoflow's to remove. Deleting it now
            # would leave the old name on those files as a foreign value apply must preserve.
            kept.append((alias, failures[alias]))
        else:
            conn.execute("DELETE FROM persons WHERE id=?", (aid,))
        moved.append((alias, n))
    conn.commit()
    csv_fixed = _rewrite_faces_csv(workdir / "faces.csv", {a: canonical for a, _n in moved})

    total = sum(n for _a, n in moved)
    log_action(
        conn,
        log_fh,
        run_id,
        0,
        "enrich_merge",
        f"canonical={canonical} aliases={len(moved)} faces_moved={total} "
        f"files_stripped={stripped} strip_failed={sum(failures.values())} csv_rows={csv_fixed}",
    )
    conn.commit()
    print(f"enrich merge: folded {len(moved)} alias(es) into '{canonical}', moved {total} face(s).")
    for alias, n in moved:
        print(f"  {alias} -> {canonical}: {n}")
    if stripped:
        print(f"  stripped the old name(s) from {stripped} library file(s).")
    if csv_fixed:
        print(f"  updated {csv_fixed} row(s) in faces.csv.")
    for alias, nfail in kept:
        print(
            f"  kept persons row '{alias}' (strip failed on {nfail} file(s)) so the next enrich"
            " apply still owns the name and removes it from PersonInImage/HierarchicalSubject;"
            " fix those files and re-run the merge to drop the row."
        )
    if total:
        print("Next: photoflow enrich apply (writes the canonical name into regions/people).")
