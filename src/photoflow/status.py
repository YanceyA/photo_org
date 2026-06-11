"""Status command: manifest summary by status, role, and date source."""

from __future__ import annotations


def cmd_status(conn, workdir, run_id, log_fh, args, cfg):
    print("by status:")
    for r in conn.execute("SELECT status, COUNT(*) c FROM files GROUP BY status"):
        print(f"  {r['status']:>14}: {r['c']}")
    print("by role:")
    for r in conn.execute("SELECT COALESCE(role,'-') role, COUNT(*) c FROM files GROUP BY role"):
        print(f"  {r['role']:>14}: {r['c']}")
    print("by date source:")
    for r in conn.execute(
        "SELECT COALESCE(date_source,'-') s, COUNT(*) c FROM files GROUP BY date_source"
    ):
        print(f"  {r['s']:>14}: {r['c']}")
