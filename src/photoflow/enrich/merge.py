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
) -> tuple[set[int], set[int]]:
    """Delete one merged-away name from the library files' keyword lists.

    enrich apply only ever UNIONS keywords, so a renamed person lingers in dc:Subject /
    IPTC:Keywords / PersonInImage / HierarchicalSubject on every already-applied file (H12).
    exiftool's '-=' removes that exact list value and is a no-op when it's absent, so this
    strips only the named values and never disturbs other keywords. -P keeps the mtime.

    Returns ({file_id actually stripped}, {file_id that could not be stripped}). Like apply,
    a failed batch is re-run one block at a time: exiftool keeps going after a bad file, so a
    non-zero rc only means "at least one block failed". A file in neither set had nothing to
    strip (a sidecar apply has never created) and is treated as done.
    """
    remove = keyword_remove_argfile_lines(
        [alias], iptc=cfg.write_iptc_keywords, people_prefix=cfg.people_keyword_prefix
    )
    failed: set[int] = set()
    reported = 0

    def _fail(file_id: int, dest: str, detail: str, extra: list[str] = ()) -> None:
        nonlocal reported
        failed.add(file_id)
        log_action(
            conn, log_fh, run_id, file_id, "enrich_merge_strip_error", f"{alias}: {dest}: {detail}"
        )
        if reported < MAX_FAILURE_REPORTS:
            reported += 1
            print(f"  FAILED to strip '{alias}': {dest} ({detail})")
            for err in extra[:3]:
                print(f"    {err}")

    blocks: list[tuple[int, str, list[str]]] = []  # (file_id, dest, argfile lines)
    for file_id, (dest, ext) in touched.items():
        is_embed = (ext or "").lower() in EMBED_EXT
        target = dest if is_embed else dest + ".xmp"
        if not Path(target).exists():
            if is_embed:
                # The library file itself is gone (renamed, deleted, volume unmounted). This
                # is NOT "nothing to strip": the file may well come back still carrying the
                # old name, so treat it as a failure and keep the alias row.
                _fail(file_id, dest, "file not found")
            # else: a sidecar apply has never created holds no keywords - nothing to strip.
            continue
        blocks.append((file_id, dest, ["-P", "-overwrite_original", *remove, target, "-execute"]))
    if not blocks:
        return set(), failed

    lines: list[str] = []
    for _fid, _dest, block in blocks:
        lines += block
    res = exiftool_apply_argfile(lines)
    if res.returncode == 0:
        return {fid for fid, _dest, _block in blocks}, failed

    print(
        f"  exiftool rc={res.returncode} while stripping '{alias}': retrying its"
        f" {len(blocks)} file(s) individually to find the bad one(s)..."
    )
    ok: set[int] = set()
    for file_id, dest, block in blocks:
        one = exiftool_apply_argfile(block)
        if one.returncode == 0:
            ok.add(file_id)
            continue
        err_lines = (one.stderr or "").strip().splitlines()
        _fail(file_id, dest, f"rc={one.returncode}", err_lines)
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
    stripped_ids: set[int] = set()
    failures: dict[str, set[int]] = {}
    for alias, _aid, touched in plans:
        if not touched:
            continue
        ok, failed = _strip_alias_keywords(conn, log_fh, run_id, alias, touched, cfg)
        stripped_ids |= ok  # distinct: one file can carry two aliases
        if failed:
            failures[alias] = failed

    # Pass 3: repoint the faces of the stripped files, queue their rewrite, retire the alias.
    moved: list[tuple[str, int]] = []
    kept: list[tuple[str, int, int]] = []  # alias, faces left behind, files that failed
    for alias, aid, touched in plans:
        bad = failures.get(alias, set())
        # A face whose file still carries the old name STAYS on the alias: that is what makes
        # the next merge find the file again and retry the strip. Repointing it would leave
        # the stale keyword behind with nothing pointing at it (the re-run would see zero
        # faces, delete the row, and the name would be foreign - unremovable - from then on).
        stay: list[int] = []
        for file_id in bad:
            stay += [
                r["id"]
                for r in conn.execute(
                    "SELECT id FROM faces WHERE person_id=? AND file_id=?", (aid, file_id)
                )
            ]
        n = conn.execute("UPDATE faces SET person_id=? WHERE person_id=?", (cid, aid)).rowcount
        for face_id in stay:  # per-id restore: a NOT IN(...) list would trip the 32766-var cap
            conn.execute("UPDATE faces SET person_id=? WHERE id=?", (aid, face_id))
        n -= len(stay)
        for file_id in touched:  # one statement per id, same reason
            if file_id in bad:  # unchanged file, unchanged signature
                continue
            conn.execute("UPDATE enrich_state SET applied_sig=NULL WHERE file_id=?", (file_id,))
        if bad:
            # Keep the row: it is what makes the name photoflow's to remove. Deleting it now
            # would leave the old name on those files as a foreign value apply must preserve.
            kept.append((alias, len(stay), len(bad)))
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
        f"files_stripped={len(stripped_ids)} "
        f"strip_failed={len(set().union(*failures.values()) if failures else set())} "
        f"csv_rows={csv_fixed}",
    )
    conn.commit()
    print(f"enrich merge: folded {len(moved)} alias(es) into '{canonical}', moved {total} face(s).")
    for alias, n in moved:
        print(f"  {alias} -> {canonical}: {n}")
    if stripped_ids:
        print(f"  stripped the old name(s) from {len(stripped_ids)} library file(s).")
    if csv_fixed:
        print(f"  updated {csv_fixed} row(s) in faces.csv.")
    for alias, nstay, nfail in kept:
        print(
            f"  kept persons row '{alias}' with {nstay} face(s) on {nfail} file(s) whose strip"
            " failed; fix those files and re-run the merge - it will retry the strip and then"
            " fold them."
        )
    if total:
        print("Next: photoflow enrich apply (writes the canonical name into regions/people).")
