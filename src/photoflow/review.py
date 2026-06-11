"""Review command: export review.html + decisions.csv for near-dupe groups."""

from __future__ import annotations

import csv
import html
from collections import defaultdict

from photoflow.audit import log_action
from photoflow.hashing import HAVE_PIL

if HAVE_PIL:
    from PIL import Image


def cmd_review(conn, workdir, run_id, log_fh, args):
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

    # carry forward any decisions already made so regeneration never loses work
    prior: dict[str, dict] = {}
    if dec_path.exists():
        with open(dec_path, newline="", encoding="utf-8") as f:
            for row in csv.DictReader(f):
                if (row.get("decision") or "").strip():
                    prior[row["file_id"]] = row

    groups = defaultdict(list)
    for r in rows:
        groups[r["group_id"]].append(r)

    with open(dec_path, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(
            [
                "group_id",
                "file_id",
                "source_path",
                "resolution",
                "size_kb",
                "suggestion",
                "decision",
                "merge_from_file_id",
            ]
        )
        for gid, members in groups.items():
            best = max(members, key=lambda m: (m["width"] or 0) * (m["height"] or 0))
            for m in members:
                old = prior.get(str(m["id"]), {})
                w.writerow(
                    [
                        gid,
                        m["id"],
                        m["source_path"],
                        f"{m['width']}x{m['height']}",
                        round((m["size"] or 0) / 1024),
                        "keep" if m["id"] == best["id"] else "keep?",
                        old.get("decision", ""),
                        old.get("merge_from_file_id", ""),
                    ]
                )

    parts = [
        "<html><head><meta charset='utf-8'><style>",
        "body{font-family:sans-serif;background:#111;color:#ddd}",
        ".g{border:1px solid #444;margin:14px;padding:10px;border-radius:8px}",
        ".f{display:inline-block;margin:6px;text-align:center;vertical-align:top}",
        "img{max-height:220px;border-radius:4px}",
        "small{display:block;max-width:260px;word-break:break-all;color:#9ab}",
        "</style></head><body><h2>photoflow review queue</h2>",
        "<p>Edit <b>decisions.csv</b>: set decision to <b>keep</b> or "
        "<b>skip</b> per row. Optionally set merge_from_file_id to pull "
        "missing metadata (GPS, dates) from a skipped twin into the keeper. "
        "Rows left blank stay on hold.</p>",
    ]
    for gid, members in groups.items():
        parts.append(f"<div class='g'><h3>group {gid}</h3>")
        for m in members:
            tp = thumbs / f"{m['id']}.jpg"
            if HAVE_PIL and not tp.exists():
                try:
                    with Image.open(m["source_path"]) as im:
                        im.thumbnail((320, 320))
                        im.convert("RGB").save(tp, "JPEG", quality=80)
                except Exception:
                    pass
            img = f"<img src='thumbs/{m['id']}.jpg'>" if tp.exists() else "(no preview)"
            parts.append(
                f"<div class='f'>{img}<small>id {m['id']} | "
                f"{m['width']}x{m['height']} | {round((m['size'] or 0) / 1024)} KB | "
                f"{m['date_taken'] or 'no date'}<br>"
                f"{html.escape(m['source_path'])}</small></div>"
            )
        parts.append("</div>")
    parts.append("</body></html>")
    html_path.write_text("\n".join(parts), encoding="utf-8")

    log_action(
        conn, log_fh, run_id, 0, "review_exported", f"{len(groups)} groups, {len(rows)} files"
    )
    conn.commit()
    print(f"review.html  -> {html_path}")
    print(f"decisions.csv -> {dec_path}")
    print("Open the HTML, fill the CSV, then run apply.")
