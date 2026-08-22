"""prune-sidecars: move already-copied sidecars out of the library (never delete).

No exiftool needed: the manifest rows and library files are built directly.
"""

import shutil
from pathlib import Path

from photoflow import prune as prune_mod
from photoflow.config import Config
from photoflow.db import new_run, open_db
from photoflow.prune import cmd_prune_sidecars


class Args:
    def __init__(self, out, dry_run=False):
        self.out = str(out)
        self.dry_run = dry_run


def _fixture(tmp_path: Path):
    """Two copied .thm rows, one with a provenance .xmp beside it, one already gone."""
    work, lib = tmp_path / "work", tmp_path / "lib"
    conn = open_db(work)
    dests = []
    for n, has_sidecar in ((1, True), (2, False)):
        d = lib / "2003" / "11" / f"20031116_15582{n}_CRW-016{n}_abcd000{n}.thm"
        d.parent.mkdir(parents=True, exist_ok=True)
        d.write_bytes(b"thumbnail-bytes")
        if has_sidecar:
            d.with_name(d.name + ".xmp").write_text("<xmp/>", encoding="utf-8")
        conn.execute(
            "INSERT INTO files(source_path, kind, ext, status, dest_path) VALUES (?,?,?,?,?)",
            (str(tmp_path / f"src{n}.thm"), "sidecar", ".thm", "copied", str(d)),
        )
        dests.append(d)
    # a copied JPEG must be left completely alone
    keep = lib / "2003" / "11" / "20031116_155830_photo_ffff0000.jpg"
    keep.write_bytes(b"\xff\xd8jpeg")
    conn.execute(
        "INSERT INTO files(source_path, kind, ext, status, dest_path) VALUES (?,?,?,?,?)",
        (str(tmp_path / "src3.jpg"), "image", ".jpg", "copied", str(keep)),
    )
    conn.commit()
    return conn, work, lib, dests, keep


def _run(conn, work, lib, dry_run=False):
    run_id = new_run(conn, "prune-sidecars", {})
    (work / "logs").mkdir(exist_ok=True)
    with open(work / "logs" / f"test_{run_id}.jsonl", "a", encoding="utf-8") as fh:
        cmd_prune_sidecars(conn, work, run_id, fh, Args(lib, dry_run), Config())


def test_prune_moves_sidecars_and_updates_the_manifest(tmp_path: Path):
    conn, work, lib, dests, keep = _fixture(tmp_path)
    _run(conn, work, lib)

    pruned = work / "pruned" / "2003" / "11"
    for d in dests:
        assert not d.exists()
        assert (pruned / d.name).read_bytes() == b"thumbnail-bytes"
    assert (pruned / (dests[0].name + ".xmp")).exists()  # its provenance sidecar came too
    assert keep.exists()  # the JPEG is untouched

    rows = conn.execute("SELECT status, dest_path FROM files WHERE kind='sidecar'").fetchall()
    assert {r["status"] for r in rows} == {"skipped_sidecar"}
    assert all(r["dest_path"] is None for r in rows)
    actions = [r["action"] for r in conn.execute("SELECT action FROM actions")]
    assert actions.count("pruned_sidecar") == 3  # 2 thumbs + 1 sidecar


def test_prune_dry_run_changes_nothing(tmp_path: Path, capsys):
    conn, work, lib, dests, keep = _fixture(tmp_path)
    _run(conn, work, lib, dry_run=True)

    assert all(d.exists() for d in dests)
    assert not (work / "pruned").exists()
    rows = conn.execute("SELECT status FROM files WHERE kind='sidecar'").fetchall()
    assert {r["status"] for r in rows} == {"copied"}
    assert "DRY" in capsys.readouterr().out


def test_prune_reports_missing_files_but_still_clears_the_row(tmp_path: Path):
    conn, work, lib, dests, keep = _fixture(tmp_path)
    dests[1].unlink()  # someone already deleted it by hand
    _run(conn, work, lib)

    row = conn.execute(
        "SELECT status, dest_path FROM files WHERE dest_path IS NULL AND kind='sidecar'"
    ).fetchall()
    assert len(row) == 2  # both rows cleared, including the vanished one


def test_prune_isolates_a_move_error_and_keeps_going(tmp_path: Path, monkeypatch, capsys):
    conn, work, lib, dests, keep = _fixture(tmp_path)
    real_move = shutil.move

    def flaky_move(src, dst):
        if str(src).endswith(".xmp"):
            raise PermissionError("locked by another process")
        return real_move(src, dst)

    monkeypatch.setattr(prune_mod.shutil, "move", flaky_move)
    _run(conn, work, lib)

    # row 1 (the one with the .xmp) never got its sidecar moved -> the whole row is
    # left untouched: main file still in place, status still 'copied'.
    assert dests[0].exists()
    row1 = conn.execute(
        "SELECT status, dest_path FROM files WHERE dest_path=?", (str(dests[0]),)
    ).fetchone()
    assert row1["status"] == "copied"

    # row 2 (no sidecar) is unaffected by the mock and still gets moved + cleared.
    assert not dests[1].exists()
    row2 = conn.execute(
        "SELECT status, dest_path FROM files WHERE source_path=?", (str(tmp_path / "src2.thm"),)
    ).fetchone()
    assert row2["status"] == "skipped_sidecar"
    assert row2["dest_path"] is None

    actions = [r["action"] for r in conn.execute("SELECT action FROM actions")]
    assert actions.count("prune_error") == 1

    out = capsys.readouterr().out
    assert "1 failed" in out


def test_prune_dry_run_reports_missing_files(tmp_path: Path, capsys):
    conn, work, lib, dests, keep = _fixture(tmp_path)
    dests[1].unlink()  # gone before the dry run even looks at it
    _run(conn, work, lib, dry_run=True)

    out = capsys.readouterr().out
    assert f"DRY  {dests[1]}" not in out
    assert "1 already gone" in out
    # the other row (still present) is reported normally
    assert f"DRY  {dests[0]}" in out


def test_prune_keeps_sidecar_next_to_a_collision_renamed_file(tmp_path: Path):
    conn, work, lib, dests, keep = _fixture(tmp_path)
    pruned_dir = work / "pruned" / "2003" / "11"
    pruned_dir.mkdir(parents=True)
    (pruned_dir / dests[0].name).write_bytes(b"already-there")  # forces a collision rename

    _run(conn, work, lib)

    renamed = pruned_dir / f"{dests[0].stem}_1{dests[0].suffix}"
    assert renamed.exists()
    assert renamed.read_bytes() == b"thumbnail-bytes"
    assert (pruned_dir / f"{renamed.name}.xmp").exists()
