"""prune-sidecars: move sidecar files an earlier apply copied out of the library.

Never deletes. Files are moved to workdir/pruned/<path relative to --out> so the
owner can inspect (and restore) them; the manifest row goes back to
status='skipped_sidecar' with dest_path cleared, which is exactly the state a
fresh apply would have produced under copy_sidecars=false.
"""

from __future__ import annotations

import shutil
from pathlib import Path

from photoflow.audit import log_action


def _free_path(p: Path) -> Path:
    """Never overwrite something already sitting in the pruned tree."""
    if not p.exists():
        return p
    for n in range(1, 1000):
        cand = p.with_name(f"{p.stem}_{n}{p.suffix}")
        if not cand.exists():
            return cand
    raise OSError(f"no free name for {p}")


def cmd_prune_sidecars(conn, workdir, run_id, log_fh, args, cfg):
    out_root = Path(args.out).expanduser().resolve()
    pruned_root = workdir / "pruned"
    rows = conn.execute(
        "SELECT id, dest_path FROM files "
        "WHERE status='copied' AND kind='sidecar' AND dest_path IS NOT NULL"
    ).fetchall()
    if not rows:
        print("no copied sidecars in the library - nothing to prune.")
        return

    pruned = missing = outside = 0
    for r in rows:
        dest = Path(r["dest_path"])
        try:
            rel = dest.resolve().relative_to(out_root)
        except ValueError:
            print(f"  not under --out, leaving alone: {dest}")
            outside += 1
            continue
        moves = [(dest, pruned_root / rel)]
        sidecar = dest.with_name(dest.name + ".xmp")  # the provenance .xmp apply wrote
        if sidecar.exists():
            moves.append((sidecar, pruned_root / rel.parent / sidecar.name))

        if args.dry_run:
            for old, new in moves:
                print(f"DRY  {old}  ->  {new}")
            pruned += 1
            continue

        for old, new in moves:
            if not old.exists():
                print(f"  already gone: {old}")
                missing += 1
                continue
            new = _free_path(new)
            new.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(old), str(new))  # move, not replace: workdir may be another drive
            log_action(conn, log_fh, run_id, r["id"], "pruned_sidecar", f"{old} -> {new}")
        conn.execute(
            "UPDATE files SET status='skipped_sidecar', dest_path=NULL WHERE id=?", (r["id"],)
        )
        pruned += 1
    conn.commit()

    verb = "would prune" if args.dry_run else "pruned"
    print(f"{verb} {pruned} sidecar(s); {missing} already gone, {outside} outside --out.")
    if not args.dry_run and pruned:
        print(f"moved into {pruned_root} (nothing was deleted).")
        print("Immich will see these as removed assets; rescan the external library.")
