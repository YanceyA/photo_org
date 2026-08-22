"""Command-line entry point: argument parsing, run/log setup, command dispatch."""

from __future__ import annotations

import argparse
from pathlib import Path

from photoflow.apply import cmd_apply
from photoflow.config import load_config
from photoflow.db import new_run, open_db
from photoflow.enrich.apply import cmd_enrich_apply
from photoflow.enrich.assign import cmd_enrich_assign
from photoflow.enrich.cluster import cmd_enrich_cluster
from photoflow.enrich.merge import cmd_enrich_merge
from photoflow.enrich.review import cmd_enrich_review
from photoflow.enrich.scan import cmd_enrich_scan
from photoflow.enrich.status import cmd_enrich_status
from photoflow.planner import cmd_plan
from photoflow.prune import cmd_prune_sidecars
from photoflow.refile import cmd_refile
from photoflow.review import cmd_review
from photoflow.scan import cmd_scan
from photoflow.status import cmd_status

ENRICH_COMMANDS = {
    "scan": cmd_enrich_scan,
    "cluster": cmd_enrich_cluster,
    "assign": cmd_enrich_assign,
    "merge": cmd_enrich_merge,
    "review": cmd_enrich_review,
    "apply": cmd_enrich_apply,
    "status": cmd_enrich_status,
}


def main():
    ap = argparse.ArgumentParser(description="photoflow - incremental photo organizer")
    ap.add_argument(
        "--workdir", default="photoflow_work", help="state directory (db, logs, review files)"
    )
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("scan", help="fingerprint source folders into the manifest")
    p.add_argument(
        "sources",
        nargs="*",
        help="source folders to scan (with --refresh-meta: path prefixes to limit the refresh)",
    )
    p.add_argument(
        "--refresh-meta",
        action="store_true",
        help=(
            "re-read exiftool metadata for rows already in the manifest (no re-hash, no copy); "
            "rows in error/skipped_manual are left alone"
        ),
    )
    p.add_argument(
        "--kind",
        action="append",
        choices=["image", "raw", "video", "sidecar"],
        help="with --refresh-meta: limit to this kind (repeatable)",
    )

    sub.add_parser("plan", help="resolve dates, group dupes, queue reviews")
    sub.add_parser("review", help="export review.html + decisions.csv")

    p = sub.add_parser("apply", help="copy keepers into the organized library")
    p.add_argument("--out", required=True, help="output library root")
    p.add_argument("--decisions", help="decisions CSV (default workdir/decisions.csv)")
    p.add_argument("--dry-run", action="store_true")

    p = sub.add_parser(
        "refile",
        help=(
            "move already-copied library files to the dest their current date implies "
            "(rescan your external library, e.g. Immich, afterwards)"
        ),
    )
    p.add_argument("--out", required=True, help="output library root (same as apply --out)")
    p.add_argument("--dry-run", action="store_true")

    p = sub.add_parser(
        "prune-sidecars", help="move already-copied sidecar files out of the library"
    )
    p.add_argument("--out", required=True, help="output library root")
    p.add_argument("--dry-run", action="store_true")

    sub.add_parser("status", help="manifest summary")

    # enrich: faces + content tags written into the organized library as portable XMP
    enrich = sub.add_parser("enrich", help="add people + content tags to the organized library")
    esub = enrich.add_subparsers(dest="enrich_step", required=True)
    esub.add_parser("scan", help="detect faces + content tags for copied library images")
    esub.add_parser("cluster", help="group unassigned faces into per-person clusters")
    eas = esub.add_parser("assign", help="auto-assign unassigned faces to already-named people")
    eas.add_argument("--dry-run", action="store_true", help="report what would be assigned only")
    eas.add_argument(
        "--min-sim", type=float, default=None, help="cosine-sim threshold to commit (default cfg)"
    )
    em = esub.add_parser("merge", help="fold duplicate/misspelled person names into one")
    em.add_argument("canonical", help="the correct name to keep")
    em.add_argument("aliases", nargs="+", help="duplicate name(s) to fold into canonical")
    esub.add_parser("review", help="export enrich_review.html + faces.csv + tags.csv")
    ea = esub.add_parser("apply", help="write confirmed people + tags into the library files")
    ea.add_argument("--dry-run", action="store_true")
    esub.add_parser("status", help="enrich summary (faces, clusters, tags)")

    args = ap.parse_args()
    workdir = Path(args.workdir).expanduser().resolve()
    cfg = load_config(workdir)
    conn = open_db(workdir)

    if args.cmd == "enrich":
        label = f"enrich-{args.enrich_step}"
        command_fn = ENRICH_COMMANDS[args.enrich_step]
    else:
        label = args.cmd
        command_fn = {
            "scan": cmd_scan,
            "plan": cmd_plan,
            "review": cmd_review,
            "apply": cmd_apply,
            "refile": cmd_refile,
            "prune-sidecars": cmd_prune_sidecars,
            "status": cmd_status,
        }[args.cmd]

    run_id = new_run(conn, label, vars(args) | {"workdir": str(workdir)})
    logs = workdir / "logs"
    logs.mkdir(exist_ok=True)
    with open(logs / f"run_{run_id:04d}_{label}.jsonl", "a", encoding="utf-8") as log_fh:
        command_fn(conn, workdir, run_id, log_fh, args, cfg)
