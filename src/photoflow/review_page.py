"""Pure helpers + HTML/JS template for the interactive review page.

No I/O beyond what the caller hands in: cmd_review feeds plain dict-like rows
and writes the returned strings. Testable without exiftool or Pillow.
"""

from __future__ import annotations

import csv
from pathlib import Path

CSV_COLUMNS = [
    "group_id",
    "file_id",
    "source_path",
    "resolution",
    "size_kb",
    "suggestion",
    "decision",
    "merge_from_file_id",
]


def suggested_keeper_id(members) -> int:
    best = max(members, key=lambda m: (m["width"] or 0) * (m["height"] or 0))
    return best["id"]


def decision_rows(groups, prior: dict[str, dict]) -> list[dict]:
    """decisions.csv rows; carries forward decision/merge by file_id (invariant #4)."""
    rows = []
    for gid, members in groups.items():
        best_id = suggested_keeper_id(members)
        for m in members:
            old = prior.get(str(m["id"]), {})
            rows.append(
                {
                    "group_id": gid,
                    "file_id": m["id"],
                    "source_path": m["source_path"],
                    "resolution": f"{m['width']}x{m['height']}",
                    "size_kb": round((m["size"] or 0) / 1024),
                    "suggestion": "keep" if m["id"] == best_id else "keep?",
                    "decision": old.get("decision", ""),
                    "merge_from_file_id": old.get("merge_from_file_id", ""),
                }
            )
    return rows


def write_decisions_csv(path, rows: list[dict]) -> None:
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=CSV_COLUMNS)
        w.writeheader()
        w.writerows(rows)


def _file_uri(p: str) -> str | None:
    try:
        return Path(p).as_uri()
    except ValueError:
        return None


def build_payload(groups, rows: list[dict], workdir_key: str, thumbs_ok: set) -> dict:
    """Everything the in-page JS needs: display fields, group-best flags, and the
    exact CSV cell values so the browser can re-serialize a byte-compatible CSV."""
    by_id = {r["file_id"]: r for r in rows}
    out = []
    for gid, members in groups.items():
        best_id = suggested_keeper_id(members)
        max_px = max((m["width"] or 0) * (m["height"] or 0) for m in members)
        max_size = max((m["size"] or 0) for m in members)
        files = []
        for m in members:
            r = by_id[m["id"]]
            px = (m["width"] or 0) * (m["height"] or 0)
            files.append(
                {
                    "id": m["id"],
                    "path": m["source_path"],
                    "uri": _file_uri(m["source_path"]),
                    "thumb": f"thumbs/{m['id']}.jpg" if m["id"] in thumbs_ok else None,
                    "w": m["width"],
                    "h": m["height"],
                    "size": m["size"] or 0,
                    "ext": m["ext"],
                    "kind": m["kind"],
                    "camera": m["camera"],
                    "date": m["date_taken"],
                    "suggested": m["id"] == best_id,
                    "bestRes": px == max_px and px > 0,
                    "bestSize": (m["size"] or 0) == max_size and max_size > 0,
                    "csv": {
                        "resolution": r["resolution"],
                        "size_kb": r["size_kb"],
                        "suggestion": r["suggestion"],
                    },
                    "decision": r["decision"],
                    "merge": str(r["merge_from_file_id"] or ""),
                }
            )
        out.append({"gid": gid, "files": files})
    return {"workdir": workdir_key, "groups": out}
