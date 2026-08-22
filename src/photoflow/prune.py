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

COMMIT_EVERY = 200


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

    pruned = cleared_only = missing = failed = outside = 0
    for r in rows:
        dest = Path(r["dest_path"])
        try:
            rel = dest.resolve().relative_to(out_root)
        except ValueError:
            print(f"  not under --out, leaving alone: {dest}")
            outside += 1
            continue

        # Rename the main file first (collision-safe), then derive the .xmp target
        # from ITS final name, so a renamed main file never gets separated from its
        # provenance sidecar inside pruned/.
        main_target = _free_path(pruned_root / rel)
        sidecar = dest.with_name(dest.name + ".xmp")  # the provenance .xmp apply wrote
        has_sidecar = sidecar.exists()
        sidecar_target = (
            _free_path(main_target.with_name(main_target.name + ".xmp")) if has_sidecar else None
        )

        if args.dry_run:
            main_present = dest.exists()
            if not main_present:
                missing += 1
            else:
                print(f"DRY  {dest}  ->  {main_target}")
            if has_sidecar:
                print(f"DRY  {sidecar}  ->  {sidecar_target}")
            if main_present:
                pruned += 1
            else:
                cleared_only += 1
            continue

        main_target.parent.mkdir(parents=True, exist_ok=True)

        # Partial-row rule: move the attachment (sidecar) before the anchor (main).
        # If the sidecar move fails, bail out before touching main at all, so the
        # row - which only tracks dest_path for the main file - is left completely
        # untouched (main file still in place, status still 'copied'). If the main
        # move itself fails AFTER the sidecar already moved, the sidecar is left
        # orphaned in pruned/ (logged as an error) but the row is still left alone,
        # since dest_path still points at a real, unmoved file. The row's UPDATE
        # only ever runs when the main file was actually moved, or was already gone.
        moved_any = False
        if has_sidecar:
            try:
                shutil.move(str(sidecar), str(sidecar_target))
            except OSError as e:
                print(f"  error: {sidecar}: {e}")
                log_action(conn, log_fh, run_id, r["id"], "prune_error", str(e))
                failed += 1
                continue
            log_action(
                conn, log_fh, run_id, r["id"], "pruned_sidecar", f"{sidecar} -> {sidecar_target}"
            )
            moved_any = True

        main_ok = False
        if not dest.exists():
            print(f"  already gone: {dest}")
            missing += 1
            main_ok = True  # nothing left to move, but the row is stale - clear it
        else:
            try:
                shutil.move(str(dest), str(main_target))
            except OSError as e:
                print(f"  error: {dest}: {e}")
                log_action(conn, log_fh, run_id, r["id"], "prune_error", str(e))
                failed += 1
            else:
                log_action(
                    conn, log_fh, run_id, r["id"], "pruned_sidecar", f"{dest} -> {main_target}"
                )
                moved_any = True
                main_ok = True

        if not main_ok:
            continue

        conn.execute(
            "UPDATE files SET status='skipped_sidecar', dest_path=NULL WHERE id=?", (r["id"],)
        )
        if moved_any:
            pruned += 1
        else:
            cleared_only += 1
        if (pruned + cleared_only) % COMMIT_EVERY == 0:
            conn.commit()
    conn.commit()

    total_cleared = pruned + cleared_only
    verb = "would prune" if args.dry_run else "pruned"
    print(
        f"{verb} {total_cleared} sidecar(s) ({cleared_only} cleared, files already gone); "
        f"{missing} already gone, {failed} failed, {outside} outside --out."
    )
    if not args.dry_run and pruned:
        print(f"moved into {pruned_root} (nothing was deleted).")
        print("Immich will see these as removed assets; rescan the external library.")
