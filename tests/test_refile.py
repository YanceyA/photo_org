"""refile: move already-copied library files to the dest their corrected date implies."""

from pathlib import Path
from types import SimpleNamespace

import pytest
from conftest import pf, q

from photoflow import refile as refile_mod
from photoflow.config import Config
from photoflow.db import new_run, open_db
from photoflow.refile import cmd_refile

H = "deadbeef" + "0" * 56  # content_hash -> hash8 'deadbeef'
OLD_REL = Path("2018") / "08" / "20180813_IMG-0735_deadbeef.mov"
NEW_REL = Path("2010") / "09" / "20100904_040331_IMG-0735_deadbeef.mov"

H2 = "feedface" + "0" * 56
OLD_REL2 = Path("2018") / "08" / "20180813_IMG-0736_feedface.mov"
NEW_REL2 = Path("2011") / "03" / "20110315_101112_IMG-0736_feedface.mov"


def make_lib(tmp_path: Path, *, sidecar=True, date="2010-09-04T04:03:31"):
    """A workdir + library holding one copied row filed under the WRONG (import-year) folder."""
    work, lib = tmp_path / "work", tmp_path / "lib"
    conn = open_db(work)
    old = lib / OLD_REL
    old.parent.mkdir(parents=True)
    old.write_bytes(b"MOVIEBYTES")
    if sidecar:
        Path(str(old) + ".xmp").write_text("<x/>", encoding="utf-8")
    conn.execute(
        "INSERT INTO files(source_path, rel_path, ext, kind, content_hash, date_taken,"
        " date_source, date_confidence, status, dest_path)"
        " VALUES (?,?,?,?,?,?,?,?,?,?)",
        (
            r"H:\backup\2018\IMG_0735.MOV",
            r"2018\IMG_0735.MOV",
            ".mov",
            "video",
            H,
            date,
            "exif",
            "high",
            "copied",
            str(old),
        ),
    )
    conn.commit()
    return work, lib, conn


def add_row(conn, *, source_path, content_hash, date, dest_path, on_disk=True, body=b"OTHERBYTES"):
    """Add a second copied row; optionally materialise its library file."""
    if on_disk:
        Path(dest_path).parent.mkdir(parents=True, exist_ok=True)
        Path(dest_path).write_bytes(body)
    conn.execute(
        "INSERT INTO files(source_path, rel_path, ext, kind, content_hash, date_taken,"
        " date_source, date_confidence, status, dest_path)"
        " VALUES (?,?,?,?,?,?,?,?,?,?)",
        (
            source_path,
            Path(source_path).name,
            ".mov",
            "video",
            content_hash,
            date,
            "exif",
            "high",
            "copied",
            str(dest_path),
        ),
    )
    conn.commit()


def dest_of(conn, content_hash):
    return conn.execute(
        "SELECT dest_path FROM files WHERE content_hash=?", (content_hash,)
    ).fetchone()[0]


def run_refile(work: Path, lib: Path, conn, dry_run: bool):
    run_id = new_run(conn, "refile", {})
    with open(work / "refile.jsonl", "w", encoding="utf-8") as fh:
        cmd_refile(conn, work, run_id, fh, SimpleNamespace(out=str(lib), dry_run=dry_run), Config())


def test_dry_run_moves_nothing(tmp_path: Path, capsys):
    work, lib, conn = make_lib(tmp_path)
    run_refile(work, lib, conn, dry_run=True)
    out = capsys.readouterr().out
    assert "MOVE" in out and str(NEW_REL) in out
    assert (lib / OLD_REL).exists()
    assert not (lib / NEW_REL).exists()
    assert conn.execute("SELECT dest_path FROM files").fetchone()[0] == str(lib / OLD_REL)


def test_refile_moves_file_and_sidecar_and_updates_the_manifest(tmp_path: Path):
    work, lib, conn = make_lib(tmp_path)
    run_refile(work, lib, conn, dry_run=False)

    assert not (lib / OLD_REL).exists()
    assert (lib / NEW_REL).read_bytes() == b"MOVIEBYTES"
    assert Path(str(lib / NEW_REL) + ".xmp").read_text(encoding="utf-8") == "<x/>"
    assert conn.execute("SELECT dest_path FROM files").fetchone()[0] == str(lib / NEW_REL)
    act = conn.execute("SELECT action, detail FROM actions WHERE action='refiled'").fetchone()
    assert act is not None
    assert str(OLD_REL) in act["detail"] and str(NEW_REL) in act["detail"]


def test_refile_is_idempotent(tmp_path: Path, capsys):
    work, lib, conn = make_lib(tmp_path)
    run_refile(work, lib, conn, dry_run=False)
    capsys.readouterr()
    run_refile(work, lib, conn, dry_run=False)
    assert "0 moved" in capsys.readouterr().out


def test_occupied_target_aborts_the_whole_run(tmp_path: Path):
    work, lib, conn = make_lib(tmp_path)
    (lib / NEW_REL).parent.mkdir(parents=True)
    (lib / NEW_REL).write_bytes(b"SOMEONE ELSE")
    with pytest.raises(SystemExit) as e:
        run_refile(work, lib, conn, dry_run=False)
    assert e.value.code != 0
    assert (lib / OLD_REL).exists()  # nothing moved
    assert (lib / NEW_REL).read_bytes() == b"SOMEONE ELSE"
    assert conn.execute("SELECT dest_path FROM files").fetchone()[0] == str(lib / OLD_REL)


def test_target_claimed_by_a_non_moving_row_aborts_the_whole_run(tmp_path: Path):
    """A target that is another copied row's CURRENT dest is a collision even if it is missing.

    The library file is deliberately absent from disk, so `b.exists()` cannot catch this - only
    the `occupied` pre-flight set can. Moving onto it would give two rows the same dest_path.
    """
    work, lib, conn = make_lib(tmp_path)
    add_row(
        conn,
        source_path=r"H:\other\2010\IMG_0735.MOV",  # same stem+hash -> same dest_for as row 1's new
        content_hash=H,
        date="2010-09-04T04:03:31",
        dest_path=lib / NEW_REL,
        on_disk=False,
    )
    with pytest.raises(SystemExit) as e:
        run_refile(work, lib, conn, dry_run=False)
    assert e.value.code != 0
    assert (lib / OLD_REL).exists()  # nothing moved
    assert not (lib / NEW_REL).exists()
    assert dest_of(conn, H) == str(lib / OLD_REL)


def test_missing_library_file_is_reported_not_fatal(tmp_path: Path, capsys):
    work, lib, conn = make_lib(tmp_path, sidecar=False)
    (lib / OLD_REL).unlink()
    run_refile(work, lib, conn, dry_run=False)
    out = capsys.readouterr().out
    assert "missing" in out.lower()
    assert conn.execute("SELECT dest_path FROM files").fetchone()[0] == str(lib / OLD_REL)


def test_a_failed_move_is_isolated_to_its_own_row(tmp_path: Path, capsys, monkeypatch):
    work, lib, conn = make_lib(tmp_path)
    add_row(
        conn,
        source_path=r"H:\backup\2018\IMG_0736.MOV",
        content_hash=H2,
        date="2011-03-15T10:11:12",
        dest_path=lib / OLD_REL2,
    )
    real_move = refile_mod._move

    def flaky(src: Path, dst: Path) -> None:
        if "IMG-0735" in str(src):
            raise PermissionError("used by another process")
        real_move(src, dst)

    monkeypatch.setattr(refile_mod, "_move", flaky)
    run_refile(work, lib, conn, dry_run=False)
    out = capsys.readouterr().out

    assert "1 failed" in out
    # row 1 untouched, manifest unchanged
    assert (lib / OLD_REL).exists()
    assert not (lib / NEW_REL).exists()
    assert dest_of(conn, H) == str(lib / OLD_REL)
    # row 2 moved and updated regardless
    assert (lib / NEW_REL2).read_bytes() == b"OTHERBYTES"
    assert dest_of(conn, H2) == str(lib / NEW_REL2)
    errs = conn.execute("SELECT detail FROM actions WHERE action='refile_error'").fetchall()
    assert len(errs) == 1


def test_refile_is_wired_into_the_cli(tmp_path: Path):
    work, lib = tmp_path / "work", tmp_path / "lib"
    lib.mkdir()
    out = pf(work, "refile", "--out", str(lib), "--dry-run").stdout
    assert "refile" in out.lower()
    assert q(work, "SELECT COUNT(*) c FROM files")[0]["c"] == 0
