"""Refile command: move already-copied library files to their current dest_for() path.

Why this exists: `status='copied'` is durable and `dest_path` is never recomputed, so a
metadata fix (e.g. the video dates repaired by `scan --refresh-meta`) followed by `plan`
changes `date_taken` but leaves the file sitting in the folder its OLD date implied. `apply`
will not touch it - it only processes `planned`/`review` rows (apply.py:27). `refile` closes
that loop.

Sources are never touched: this moves files inside the library root only.

Immich / digiKam / backup note: a moved file looks like delete + add to any external indexer.
Rescan the external library after a refile run.
"""

from __future__ import annotations

import errno
import os
import shutil
import sys
from collections import Counter
from pathlib import Path

from photoflow.audit import log_action
from photoflow.naming import dest_for

COMMIT_EVERY = 200  # rows between commits in pass 3
DRY_RUN_LINES = 200  # cap on printed MOVE lines
COLLISION_LINES = 50
MISSING_LINES = 10


def _move(src: Path, dst: Path) -> None:
    dst.parent.mkdir(parents=True, exist_ok=True)
    try:
        os.replace(src, dst)  # atomic within a volume, which is the expected case
    except OSError as e:
        if e.errno != errno.EXDEV:
            raise
        shutil.move(str(src), str(dst))


def _sidecar(p: Path) -> Path:
    return Path(str(p) + ".xmp")


def cmd_refile(conn, workdir, run_id, log_fh, args, cfg):
    out_root = Path(args.out).expanduser().resolve()
    rows = conn.execute(
        "SELECT * FROM files WHERE status='copied' AND dest_path IS NOT NULL ORDER BY id"
    ).fetchall()

    # ---- pass 1: collect every candidate move (nothing touches the disk yet)
    moves: list[tuple[int, Path, Path, str]] = []
    missing: list[str] = []
    for r in rows:
        old = Path(r["dest_path"])
        new = dest_for(r, out_root, cfg.slug_max)
        if old == new:
            continue
        if not old.exists():
            missing.append(str(old))
            continue
        reason = "folder changed" if old.parent != new.parent else "name changed"
        moves.append((r["id"], old, new, reason))

    # ---- pass 2: pre-flight. Refuse the WHOLE run on any collision; a partial refile with an
    # overwritten library file is unrecoverable, an aborted one costs nothing.
    # `vacated` = paths this run frees up (fine to move into). `occupied` = paths the manifest
    # says belong to some other copied row that is NOT moving - a different file legitimately
    # lives there, so it is a collision even when the file is missing from disk (moving in
    # would leave two rows sharing one dest_path).
    vacated = set()
    for _fid, old, _new, _reason in moves:
        vacated.add(str(old).casefold())
        vacated.add(str(_sidecar(old)).casefold())
    occupied = set()
    for r in rows:
        d = Path(r["dest_path"])
        occupied.add(str(d).casefold())
        occupied.add(str(_sidecar(d)).casefold())
    occupied -= vacated

    claimed: dict[str, Path] = {}
    collisions: list[str] = []
    for _fid, old, new, _reason in moves:
        for a, b in ((old, new), (_sidecar(old), _sidecar(new))):
            if not a.exists():
                continue  # sidecar simply isn't there
            key = str(b).casefold()
            if key in claimed:
                collisions.append(f"{b}  <- both {claimed[key]} and {a}")
            elif key in occupied:
                collisions.append(f"{b}  is another copied row's dest_path (target of {a})")
            elif b.exists() and key not in vacated:
                collisions.append(f"{b}  already exists (target of {a})")
            else:
                claimed[key] = a
    if collisions:
        print(f"refile aborted: {len(collisions)} destination collision(s), nothing moved:")
        for c in collisions[:COLLISION_LINES]:
            print(f"  {c}")
        if len(collisions) > COLLISION_LINES:
            print(f"  ... and {len(collisions) - COLLISION_LINES} more")
        sys.exit(1)

    by_reason = Counter(reason for _, _, _, reason in moves)
    summary = ", ".join(f"{k}: {v}" for k, v in sorted(by_reason.items())) or "none"
    if missing:
        print(f"refile: {len(missing)} copied file(s) missing from the library, skipped:")
        for m in missing[:MISSING_LINES]:
            print(f"  missing: {m}")
        if len(missing) > MISSING_LINES:
            print(f"  ... and {len(missing) - MISSING_LINES} more")

    if args.dry_run:
        for _fid, old, new, _reason in moves[:DRY_RUN_LINES]:
            print(f"MOVE {old}  ->  {new}")
        if len(moves) > DRY_RUN_LINES:
            print(
                f"  ... and {len(moves) - DRY_RUN_LINES} more "
                "(run without --dry-run to see the audit log)"
            )
        print(f"refile dry-run: {len(moves)} would move ({summary}).")
        return

    # ---- pass 3: execute. Every row is isolated: one locked/AV-held file must not abort a run
    # that has already moved thousands of others.
    #
    # Partial-row rule: the MAIN file moves first. If it lands but the .xmp sidecar move then
    # fails, dest_path is still updated (the main file HAS moved, so the manifest must follow)
    # and a `refile_sidecar_error` action records the orphaned sidecar for manual re-pairing.
    moved = sidecars = failed = 0
    for fid, old, new, _reason in moves:
        try:
            _move(old, new)
        except OSError as e:
            print(f"  error: {old}: {e}")
            log_action(conn, log_fh, run_id, fid, "refile_error", str(e))
            failed += 1
            continue
        old_sc, new_sc = _sidecar(old), _sidecar(new)
        if old_sc.exists():
            try:
                _move(old_sc, new_sc)
                sidecars += 1
            except OSError as e:
                print(f"  error: {old_sc}: {e}")
                log_action(conn, log_fh, run_id, fid, "refile_sidecar_error", f"{old_sc}: {e}")
        conn.execute("UPDATE files SET dest_path=? WHERE id=?", (str(new), fid))
        log_action(conn, log_fh, run_id, fid, "refiled", f"{old} -> {new}")
        moved += 1
        if moved % COMMIT_EVERY == 0:
            conn.commit()
            print(f"  refiled {moved}/{len(moves)}...")
    conn.commit()
    print(
        f"refile complete: {moved} moved ({summary}), {sidecars} sidecars, "
        f"{failed} failed, {len(missing)} missing. "
        "Rescan your external library (Immich/digiKam) afterwards."
    )
