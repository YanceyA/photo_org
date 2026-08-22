"""refile: move already-copied library files to the dest their corrected date implies."""

import os
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


# --------------------------------------------------------------------------------------
# Review follow-ups: chained moves, --out sanity, shared dest_path, crash reconcile,
# rename-majority warning, sidecar partial-row rule.
# --------------------------------------------------------------------------------------

S8 = "bbbbbbbb"  # shared hash8 -> shared slug+hash8 tail, so dest_for() can chain
CHAIN_A_OLD = Path("2018") / "08" / "20180813_IMG-1_bbbbbbbb.mov"
CHAIN_MID = Path("2010") / "09" / "20100904_040331_IMG-1_bbbbbbbb.mov"
CHAIN_B_NEW = Path("2011") / "03" / "20110315_101112_IMG-1_bbbbbbbb.mov"


def empty_lib(tmp_path: Path):
    work, lib = tmp_path / "work", tmp_path / "lib"
    lib.mkdir()
    return work, lib, open_db(work)


def actions(conn, name):
    return conn.execute("SELECT detail FROM actions WHERE action=?", (name,)).fetchall()


def test_a_chained_move_never_overwrites_the_file_still_sitting_in_the_target(
    tmp_path: Path, capsys
):
    """A's target is B's current path and B also moves. Pass 2 clears it (B vacates), but
    pass 3 may run A first - the per-move guard must refuse rather than clobber B."""
    work, lib, conn = empty_lib(tmp_path)
    add_row(
        conn,
        source_path=r"H:\a\IMG_1.MOV",
        content_hash=S8 + "1" * 56,
        date="2010-09-04T04:03:31",  # -> CHAIN_MID, which is B's current home
        dest_path=lib / CHAIN_A_OLD,
        body=b"A-CONTENT",
    )
    add_row(
        conn,
        source_path=r"H:\b\IMG_1.MOV",
        content_hash=S8 + "2" * 56,
        date="2011-03-15T10:11:12",  # -> CHAIN_B_NEW
        dest_path=lib / CHAIN_MID,
        body=b"B-CONTENT-IRREPLACEABLE",
    )
    run_refile(work, lib, conn, dry_run=False)

    # the summary counts rows that actually moved, so it must not contradict "1 failed"
    assert "1 moved (folder changed: 1)" in capsys.readouterr().out

    bodies = {p.read_bytes() for p in lib.rglob("*.mov")}
    assert b"B-CONTENT-IRREPLACEABLE" in bodies  # the whole point
    assert b"A-CONTENT" in bodies
    assert len(actions(conn, "refile_error")) == 1
    assert (lib / CHAIN_A_OLD).read_bytes() == b"A-CONTENT"  # A deferred
    assert (lib / CHAIN_B_NEW).read_bytes() == b"B-CONTENT-IRREPLACEABLE"

    # the next run completes the chain now that the blocker has vacated
    run_refile(work, lib, conn, dry_run=False)
    assert (lib / CHAIN_MID).read_bytes() == b"A-CONTENT"
    assert not (lib / CHAIN_A_OLD).exists()
    assert dest_of(conn, S8 + "1" * 56) == str(lib / CHAIN_MID)


def test_wrong_out_root_is_refused_before_anything_moves(tmp_path: Path):
    work, lib, conn = make_lib(tmp_path)
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    run_id = new_run(conn, "refile", {})
    with open(work / "refile.jsonl", "w", encoding="utf-8") as fh:
        with pytest.raises(SystemExit) as e:
            cmd_refile(
                conn,
                work,
                run_id,
                fh,
                SimpleNamespace(out=str(elsewhere), dry_run=False),
                Config(),
            )
    assert "wrong --out" in str(e.value)
    assert (lib / OLD_REL).exists()
    assert conn.execute("SELECT dest_path FROM files").fetchone()[0] == str(lib / OLD_REL)


def test_a_dest_path_shared_by_two_rows_is_not_vacated_by_one_of_them_moving(tmp_path: Path):
    """B stays put at P while A (which shares P) moves away; C targeting P is a collision."""
    work, lib, conn = empty_lib(tmp_path)
    shared = lib / CHAIN_MID
    add_row(  # B: dest_for == its current path, so it never moves
        conn,
        source_path=r"H:\b\IMG_1.MOV",
        content_hash=S8 + "1" * 56,
        date="2010-09-04T04:03:31",
        dest_path=shared,
        body=b"B-STAYS-PUT",
    )
    add_row(  # A: shares B's dest_path, moves away
        conn,
        source_path=r"H:\a\IMG_1.MOV",
        content_hash=S8 + "2" * 56,
        date="2011-03-15T10:11:12",
        dest_path=shared,
        on_disk=False,
    )
    add_row(  # C: elsewhere today, targets the shared path
        conn,
        source_path=r"H:\c\IMG_1.MOV",
        content_hash=S8 + "3" * 56,
        date="2010-09-04T04:03:31",
        dest_path=lib / CHAIN_A_OLD,
        body=b"C-CONTENT",
    )
    with pytest.raises(SystemExit) as e:
        run_refile(work, lib, conn, dry_run=False)
    assert e.value.code != 0
    assert shared.read_bytes() == b"B-STAYS-PUT"
    assert (lib / CHAIN_A_OLD).read_bytes() == b"C-CONTENT"
    assert dest_of(conn, S8 + "3" * 56) == str(lib / CHAIN_A_OLD)


def test_a_file_already_at_its_new_path_is_reconciled_not_reported_missing(tmp_path: Path, capsys):
    """A run that died between the move and the commit leaves the disk ahead of the manifest."""
    work, lib, conn = make_lib(tmp_path, sidecar=False)
    (lib / NEW_REL).parent.mkdir(parents=True)
    (lib / OLD_REL).rename(lib / NEW_REL)
    run_refile(work, lib, conn, dry_run=False)
    out = capsys.readouterr().out

    assert "0 moved" in out and "1 reconciled" in out
    assert "0 missing" in out and "  missing:" not in out
    assert conn.execute("SELECT dest_path FROM files").fetchone()[0] == str(lib / NEW_REL)
    assert len(actions(conn, "refile_reconciled")) == 1


def test_a_run_that_is_mostly_pure_renames_warns(tmp_path: Path, capsys):
    work, lib, conn = empty_lib(tmp_path)
    add_row(  # same folder, different name -> "name changed"
        conn,
        source_path=r"H:\a\IMG_1.MOV",
        content_hash=S8 + "1" * 56,
        date="2010-09-04T04:03:31",
        dest_path=lib / "2010" / "09" / "stale-name_bbbbbbbb.mov",
    )
    run_refile(work, lib, conn, dry_run=True)
    assert "slug_max" in capsys.readouterr().out


def test_a_failed_sidecar_move_still_updates_dest_path(tmp_path: Path, capsys, monkeypatch):
    work, lib, conn = make_lib(tmp_path)
    real_move = refile_mod._move

    def flaky(src: Path, dst: Path) -> None:
        if str(src).endswith(".xmp"):
            raise PermissionError("sidecar is open elsewhere")
        real_move(src, dst)

    monkeypatch.setattr(refile_mod, "_move", flaky)
    run_refile(work, lib, conn, dry_run=False)
    out = capsys.readouterr().out

    assert "1 moved" in out
    assert (lib / NEW_REL).read_bytes() == b"MOVIEBYTES"  # main file moved
    assert dest_of(conn, H) == str(lib / NEW_REL)  # manifest follows the main file
    assert Path(str(lib / OLD_REL) + ".xmp").exists()  # sidecar left behind, not lost
    assert len(actions(conn, "refile_sidecar_error")) == 1


_ROOTS = ["E:/", "//nas/photos", "E:/Photos"] if os.name == "nt" else ["/", "/mnt/photos"]


@pytest.mark.parametrize("root", _ROOTS)
def test_root_prefix_ends_with_exactly_one_separator(root: str):
    """A drive or UNC root already ends in a separator; `str(root) + os.sep` would double it
    and the --out guard would then reject every legitimate run against that root."""
    prefix = refile_mod._root_prefix(Path(root))
    assert prefix.endswith(os.sep)
    assert not prefix.endswith(os.sep + os.sep)
    assert prefix == prefix.casefold()
    assert str(Path(root) / "2010" / "x.mov").casefold().startswith(prefix)


def test_reconcile_also_brings_the_sidecar_across(tmp_path: Path, capsys):
    """A crash between the main move and the sidecar move strands the .xmp at the old path -
    and once dest_path is reconciled, pass 1 short-circuits and nobody looks for it again."""
    work, lib, conn = make_lib(tmp_path)
    (lib / NEW_REL).parent.mkdir(parents=True)
    (lib / OLD_REL).rename(lib / NEW_REL)  # main file only; .xmp left behind
    assert Path(str(lib / OLD_REL) + ".xmp").exists()

    run_refile(work, lib, conn, dry_run=False)

    assert "1 reconciled" in capsys.readouterr().out
    assert Path(str(lib / NEW_REL) + ".xmp").read_text(encoding="utf-8") == "<x/>"
    assert not Path(str(lib / OLD_REL) + ".xmp").exists()
    assert dest_of(conn, H) == str(lib / NEW_REL)


def test_a_row_outside_out_is_skipped_not_swept_into_the_library(tmp_path: Path, capsys):
    """A manifest spanning two libraries: the row still under the OLD root must stay there.

    The `any()` sanity check only proves SOME row lives under --out; without a per-row check
    the old-root row gets moved INTO this library."""
    work, lib, conn = make_lib(tmp_path)
    stray = tmp_path / "OLD_LIBRARY" / OLD_REL2
    add_row(
        conn,
        source_path=r"H:\backup\2018\IMG_0736.MOV",
        content_hash=H2,
        date="2011-03-15T10:11:12",
        dest_path=stray,
    )
    run_refile(work, lib, conn, dry_run=False)
    out = capsys.readouterr().out

    assert stray.read_bytes() == b"OTHERBYTES"  # never touched
    assert not (lib / NEW_REL2).exists()
    assert dest_of(conn, H2) == str(stray)  # manifest still points at the old root
    assert f"outside --out: {stray}" in out
    assert "1 outside --out" in out
    assert (lib / NEW_REL).read_bytes() == b"MOVIEBYTES"  # the in-root row still moved
    assert dest_of(conn, H) == str(lib / NEW_REL)


@pytest.mark.exiftool
def test_refile_runbook_repairs_a_video_filed_under_its_import_year(tmp_path: Path):
    """The owner-facing repair loop end to end: a video whose EXIF date was never read lands
    under its mtime year, then scan --refresh-meta -> plan -> refile walks it (and its sidecar)
    to the folder its real capture date implies."""
    from datetime import UTC, datetime

    from conftest import make_minimal_mp4

    src, work, lib = tmp_path / "src", tmp_path / "work", tmp_path / "lib"
    src.mkdir()
    clip = src / "IMG_0735.MOV"
    make_minimal_mp4(clip, datetime(2010, 9, 3, 16, 3, 31, tzinfo=UTC))
    os.utime(clip, (1534000000, 1534000000))  # mtime in 2018: the wrong year to file under
    pf(work, "scan", str(src))
    conn = open_db(work)
    conn.execute("UPDATE files SET exif_date=NULL")  # pre-fix state: -fast2 read nothing back
    conn.commit()
    conn.close()
    pf(work, "plan")
    pf(work, "apply", "--out", str(lib))
    wrong = Path(q(work, "SELECT dest_path FROM files")[0]["dest_path"])
    assert wrong.relative_to(lib).parts[0] == "2018"

    pf(work, "scan", "--refresh-meta", "--kind", "video")
    pf(work, "plan")
    assert "MOVE" in pf(work, "refile", "--out", str(lib), "--dry-run").stdout
    assert wrong.exists(), "dry run must not move anything"
    pf(work, "refile", "--out", str(lib))

    row = q(work, "SELECT dest_path, date_source FROM files")[0]
    fixed = Path(row["dest_path"])
    assert fixed.relative_to(lib).parts[:2] == ("2010", "09")
    assert fixed.exists() and Path(str(fixed) + ".xmp").exists()
    assert not wrong.exists() and not Path(str(wrong) + ".xmp").exists()
    assert row["date_source"] == "exif"
