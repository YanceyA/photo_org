"""Audit logging: every action recorded in the actions table and a JSONL log."""

from __future__ import annotations

import json
from datetime import datetime


def log_action(conn, log_fh, run_id, file_id, action, detail=""):
    ts = datetime.now().isoformat(timespec="seconds")
    conn.execute(
        "INSERT INTO actions(run_id,file_id,action,detail,ts) VALUES (?,?,?,?,?)",
        (run_id, file_id, action, detail, ts),
    )
    log_fh.write(
        json.dumps(
            {"ts": ts, "run": run_id, "file_id": file_id, "action": action, "detail": detail}
        )
        + "\n"
    )
