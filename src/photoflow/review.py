"""Review command: export interactive review.html + decisions.csv for near-dupe groups."""

from __future__ import annotations

import csv
from collections import defaultdict

from photoflow.audit import log_action
from photoflow.hashing import HAVE_PIL
from photoflow.review_page import build_payload, decision_rows, render_page, write_decisions_csv

if HAVE_PIL:
    from PIL import Image


def _read_prior(dec_path) -> dict[str, dict]:
    """Carry forward any decisions already made so regeneration never loses work."""
    prior: dict[str, dict] = {}
    if dec_path.exists():
        with open(dec_path, newline="", encoding="utf-8") as f:
            for row in csv.DictReader(f):
                if (row.get("decision") or "").strip():
                    prior[row["file_id"]] = row
    return prior


def _make_thumbs(groups, thumbs_dir) -> set[int]:
    ok: set[int] = set()
    for members in groups.values():
        for m in members:
            tp = thumbs_dir / f"{m['id']}.jpg"
            if HAVE_PIL and not tp.exists():
                try:
                    with Image.open(m["source_path"]) as im:
                        im.thumbnail((320, 320))
                        im.convert("RGB").save(tp, "JPEG", quality=80)
                except Exception:
                    pass
            if tp.exists():
                ok.add(m["id"])
    return ok


def cmd_review(conn, workdir, run_id, log_fh, args, cfg):
    rows = conn.execute(
        "SELECT * FROM files WHERE role='review' ORDER BY group_id, size DESC"
    ).fetchall()
    if not rows:
        print("nothing queued for review.")
        return
    thumbs = workdir / "thumbs"
    thumbs.mkdir(exist_ok=True)
    dec_path = workdir / "decisions.csv"
    html_path = workdir / "review.html"

    groups = defaultdict(list)
    for r in rows:
        groups[r["group_id"]].append(r)

    out_rows = decision_rows(groups, _read_prior(dec_path))
    write_decisions_csv(dec_path, out_rows)

    payload = build_payload(groups, out_rows, str(workdir.resolve()), _make_thumbs(groups, thumbs))
    html_path.write_text(render_page(payload), encoding="utf-8")

    log_action(
        conn, log_fh, run_id, 0, "review_exported", f"{len(groups)} groups, {len(rows)} files"
    )
    conn.commit()
    print(f"review.html  -> {html_path}")
    print(f"decisions.csv -> {dec_path}")
    print("Open the HTML, click keepers, Save decisions.csv, then run apply.")
