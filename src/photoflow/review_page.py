"""Pure helpers + HTML/JS template for the interactive review page.

No I/O beyond what the caller hands in: cmd_review feeds plain dict-like rows
and writes the returned strings. Testable without exiftool or Pillow.
"""

from __future__ import annotations

import csv

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
