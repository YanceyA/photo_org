"""Command-line entry point: argument parsing, run/log setup, command dispatch."""

from __future__ import annotations

import argparse
from pathlib import Path

from photoflow.apply import cmd_apply
from photoflow.db import new_run, open_db
from photoflow.planner import cmd_plan
from photoflow.review import cmd_review
from photoflow.scan import cmd_scan
from photoflow.status import cmd_status


def main():
    ap = argparse.ArgumentParser(description="photoflow - incremental photo organizer")
    ap.add_argument(
        "--workdir", default="photoflow_work", help="state directory (db, logs, review files)"
    )
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("scan", help="fingerprint source folders into the manifest")
    p.add_argument("sources", nargs="+")

    sub.add_parser("plan", help="resolve dates, group dupes, queue reviews")
    sub.add_parser("review", help="export review.html + decisions.csv")

    p = sub.add_parser("apply", help="copy keepers into the organized library")
    p.add_argument("--out", required=True, help="output library root")
    p.add_argument("--decisions", help="decisions CSV (default workdir/decisions.csv)")
    p.add_argument("--dry-run", action="store_true")

    sub.add_parser("status", help="manifest summary")

    args = ap.parse_args()
    workdir = Path(args.workdir).expanduser().resolve()
    conn = open_db(workdir)
    run_id = new_run(conn, args.cmd, vars(args) | {"workdir": str(workdir)})
    logs = workdir / "logs"
    logs.mkdir(exist_ok=True)
    with open(logs / f"run_{run_id:04d}_{args.cmd}.jsonl", "a", encoding="utf-8") as log_fh:
        {
            "scan": cmd_scan,
            "plan": cmd_plan,
            "review": cmd_review,
            "apply": cmd_apply,
            "status": cmd_status,
        }[args.cmd](conn, workdir, run_id, log_fh, args)
