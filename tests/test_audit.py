import io
import json
from pathlib import Path

from photoflow.audit import log_action
from photoflow.db import new_run, open_db


def test_log_action_writes_table_and_jsonl(tmp_path: Path):
    conn = open_db(tmp_path)
    run_id = new_run(conn, "scan", {})
    fh = io.StringIO()
    log_action(conn, fh, run_id, 7, "copied", "a -> b")
    conn.commit()

    row = conn.execute("SELECT * FROM actions").fetchone()
    assert (row["run_id"], row["file_id"], row["action"], row["detail"]) == (
        run_id,
        7,
        "copied",
        "a -> b",
    )

    rec = json.loads(fh.getvalue())
    assert rec["action"] == "copied" and rec["file_id"] == 7 and rec["run"] == run_id
