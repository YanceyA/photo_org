"""Plan command: resolve dates, group duplicates, queue near-dupes for review."""

from __future__ import annotations

from collections import defaultdict
from datetime import timedelta
from pathlib import Path

from photoflow.audit import log_action
from photoflow.bktree import BKTree
from photoflow.config import BURST_WINDOW_S, NEAR_DUPE_THRESHOLD
from photoflow.dates import parse_exif_date, resolve_date
from photoflow.hashing import HAVE_IMAGEHASH


def cmd_plan(conn, workdir, run_id, log_fh, args):
    # roles/groups are recomputed every plan; only copied/error/manual-skip
    # statuses are durable across plans
    conn.execute("""UPDATE files SET role=NULL, group_id=NULL, dupe_of=NULL
                    WHERE status NOT IN ('error','skipped_manual')""")
    conn.execute("""UPDATE files SET status='scanned'
                    WHERE status NOT IN ('copied','error','skipped_manual')""")
    conn.commit()
    group_seq = conn.execute("SELECT COALESCE(MAX(group_id),0) FROM files").fetchone()[0] or 0

    def next_group():
        nonlocal group_seq
        group_seq += 1
        return group_seq

    rows = conn.execute(
        "SELECT * FROM files WHERE status IN ('scanned','copied') AND content_hash IS NOT NULL"
    ).fetchall()
    by_id = {r["id"]: dict(r) for r in rows}

    # ---- 1. exact duplicate groups (identical bytes)
    by_hash = defaultdict(list)
    for r in by_id.values():
        by_hash[r["content_hash"]].append(r)
    exact_dupes = 0
    for members in by_hash.values():
        if len(members) == 1:
            members[0]["role"] = members[0]["role"] or "keep"
            continue
        gid = next_group()
        # keeper preference: already copied > earliest mtime
        members.sort(key=lambda m: (m["status"] != "copied", m["mtime"] or 0))
        keeper = members[0]
        keeper["role"], keeper["group_id"] = "keep", gid
        for d in members[1:]:
            d.update(role="exact_dupe", group_id=gid, dupe_of=keeper["id"])
            exact_dupes += 1

    keepers = [r for r in by_id.values() if r["role"] == "keep"]

    # ---- 2. RAW + JPEG pairs (same folder, same stem) - both kept, tagged
    stems = defaultdict(list)
    for r in keepers:
        p = Path(r["source_path"])
        stems[(str(p.parent).lower(), p.stem.lower())].append(r)
    raw_pairs, paired_ids = 0, set()
    for members in stems.values():
        kinds = {m["kind"] for m in members}
        if "raw" in kinds and ("image" in kinds):
            gid = next_group()
            for m in members:
                m["group_id"] = m["group_id"] or gid
                m["role"] = "raw_jpeg_pair"
                paired_ids.add(m["id"])
            raw_pairs += 1
        elif "video" in kinds and "image" in kinds:  # live photo
            gid = next_group()
            img = next(m for m in members if m["kind"] == "image")
            for m in members:
                m["group_id"] = m["group_id"] or gid
                if m["kind"] == "video":
                    m["role"] = "live_pair"
                    if not m["exif_date"]:
                        m["exif_date"] = img["exif_date"]
                paired_ids.add(m["id"])

    # ---- 3. near-dupe flagging via pHash (images only, never auto-delete)
    review_groups, burst_groups = 0, 0
    if HAVE_IMAGEHASH:
        ph_map = defaultdict(list)  # phash int -> [file rows]
        for r in keepers:
            if r["kind"] == "image" and r["phash"]:
                ph_map[int(r["phash"], 16)].append(r)
        tree = BKTree()
        for h in ph_map:
            tree.add(h)
        parent = {h: h for h in ph_map}

        def find(x):
            while parent[x] != x:
                parent[x] = parent[parent[x]]
                x = parent[x]
            return x

        for h in ph_map:
            for nb in tree.query(h, NEAR_DUPE_THRESHOLD):
                ra, rb = find(h), find(nb)
                if ra != rb:
                    parent[ra] = rb
        comps = defaultdict(list)
        for h in ph_map:
            comps[find(h)].extend(ph_map[h])
        for members in comps.values():
            # drop raw/jpeg pair members - those are intentional twins
            members = [m for m in members if m["id"] not in paired_ids]
            if len(members) < 2:
                continue
            # burst test: every member has an exif time, same camera,
            # consecutive gaps within the window -> keep all silently
            times = [parse_exif_date(m["exif_date"]) for m in members]
            cams = {m["camera"] for m in members}
            is_burst = all(times) and len(cams) == 1 and None not in cams
            if is_burst:
                times.sort()
                is_burst = all(
                    (b - a) <= timedelta(seconds=BURST_WINDOW_S)
                    for a, b in zip(times, times[1:])  # noqa: B905 (verbatim from reference)
                )
            gid = next_group()
            for m in members:
                m["group_id"] = gid
                m["role"] = "burst" if is_burst else "review"
            if is_burst:
                burst_groups += 1
            else:
                review_groups += 1

    # ---- 4. resolve dates
    for r in by_id.values():
        iso, src, conf = resolve_date(r)
        r["date_taken"], r["date_source"], r["date_confidence"] = iso, src, conf

    # ---- write plan back
    for r in by_id.values():
        status = r["status"]
        if status != "copied":
            status = "review" if r["role"] == "review" else "planned"
        conn.execute(
            """UPDATE files SET role=?, group_id=?, dupe_of=?, date_taken=?,
               date_source=?, date_confidence=?, exif_date=?, status=? WHERE id=?""",
            (
                r["role"],
                r["group_id"],
                r["dupe_of"],
                r["date_taken"],
                r["date_source"],
                r["date_confidence"],
                r["exif_date"],
                status,
                r["id"],
            ),
        )
    conn.commit()
    log_action(
        conn,
        log_fh,
        run_id,
        0,
        "plan",
        f"exact_dupes={exact_dupes} raw_pairs={raw_pairs} "
        f"bursts={burst_groups} review_groups={review_groups}",
    )
    conn.commit()
    print(
        f"plan complete: {exact_dupes} exact dupes will be skipped, "
        f"{raw_pairs} RAW+JPEG pairs kept, {burst_groups} burst groups kept, "
        f"{review_groups} near-dupe groups need review."
    )
    if review_groups:
        print("Next: python photoflow.py review")
    else:
        print("Next: python photoflow.py apply --out <DIR>")
