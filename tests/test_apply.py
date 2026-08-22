"""apply hardening: atomic copies, per-file error isolation, mtime preservation.

The whole module needs exiftool because `scan` hard-exits without it.
"""

import os
import shutil
from pathlib import Path

import pytest
from conftest import _gradient, pf, q

from photoflow import apply as apply_mod
from photoflow.apply import cmd_apply
from photoflow.config import load_config
from photoflow.db import new_run, open_db
from photoflow.exiftool import ExiftoolResult
from photoflow.naming import dest_for
from photoflow.prune import cmd_prune_sidecars

pytestmark = pytest.mark.exiftool

OLD_MTIME = 1104537600.0  # 2005-01-01 UTC


class Args:
    def __init__(self, out, dry_run=False, decisions=None):
        self.out = str(out)
        self.dry_run = dry_run
        self.decisions = decisions


def run_apply(work: Path, out: Path, **kw) -> None:
    """Call cmd_apply in-process (so tests can monkeypatch inside it)."""
    conn = open_db(work)
    run_id = new_run(conn, "apply", {})
    (work / "logs").mkdir(exist_ok=True)
    with open(work / "logs" / f"test_{run_id}.jsonl", "a", encoding="utf-8") as fh:
        cmd_apply(conn, work, run_id, fh, Args(out, **kw), load_config(work))
    conn.commit()
    conn.close()


def test_truncated_dest_is_recopied(photo_fixture: Path, tmp_path: Path):
    """A dest left half-written by a disk-full / yanked-USB run must not be trusted."""
    work, lib = tmp_path / "work", tmp_path / "library"
    pf(work, "scan", str(photo_fixture))
    pf(work, "plan")
    row = q(work, "SELECT * FROM files WHERE role='keep' AND source_path LIKE ?", "%beach.jpg")[0]
    dest = dest_for(row, lib.resolve(), 40)
    dest.parent.mkdir(parents=True)
    dest.write_bytes(b"truncated")

    pf(work, "apply", "--out", str(lib))

    src_size = Path(row["source_path"]).stat().st_size
    assert dest.read_bytes()[:2] == b"\xff\xd8"  # a real JPEG again
    assert dest.stat().st_size >= src_size  # >= : provenance XMP was embedded after the copy
    actions = {a["action"] for a in q(work, "SELECT action FROM actions")}
    assert "recopied_size_mismatch" in actions


def test_copy_error_is_isolated_to_one_row(photo_fixture: Path, tmp_path: Path, monkeypatch):
    """One unreadable/locked source must not abort the run."""
    work, lib = tmp_path / "work", tmp_path / "library"
    pf(work, "scan", str(photo_fixture))
    pf(work, "plan")
    victim = str(photo_fixture / "Old Laptop" / "Holiday 2015" / "beach.jpg")
    vrow = q(work, "SELECT role FROM files WHERE source_path=?", victim)[0]
    assert vrow["role"] == "keep", (
        "this test needs beach.jpg to be the keeper of its exact-dupe group with "
        "'beach copy.jpg' (equal mtimes, tie broken by an un-ORDERed SELECT in plan). "
        "If the keeper flipped, the injected copy error lands on a skipped dupe instead."
    )
    real_copy2 = shutil.copy2

    def fake_copy2(src, dst, *a, **kw):
        if str(src) == victim:
            raise PermissionError(13, "locked by another process")
        return real_copy2(src, dst, *a, **kw)

    monkeypatch.setattr(apply_mod.shutil, "copy2", fake_copy2)
    run_apply(work, lib)

    rows = {Path(r["source_path"]).name: r for r in q(work, "SELECT * FROM files")}
    assert rows["beach.jpg"]["status"] == "error"
    assert "locked by another process" in rows["beach.jpg"]["error"]
    assert rows["mountain.jpg"]["status"] == "copied"  # the run kept going
    assert not list(lib.rglob("*.part"))  # the partial copy was cleaned up
    actions = {a["action"] for a in q(work, "SELECT action FROM actions")}
    assert "copy_error" in actions


def test_dry_run_creates_no_directories(photo_fixture: Path, tmp_path: Path):
    work, lib = tmp_path / "work", tmp_path / "library"
    pf(work, "scan", str(photo_fixture))
    pf(work, "plan")
    pf(work, "apply", "--out", str(lib), "--dry-run")
    assert not lib.exists()


def test_library_mtime_equals_source_mtime(tmp_path: Path):
    """copy2 preserves mtime; exiftool -P must not reset it when embedding XMP."""
    src = tmp_path / "src"
    src.mkdir()
    photo = src / "old.jpg"
    _gradient(640, 480, seed=31).save(photo, "JPEG", quality=92)
    os.utime(photo, (OLD_MTIME, OLD_MTIME))
    work, lib = tmp_path / "work", tmp_path / "library"

    pf(work, "scan", str(src))
    pf(work, "plan")
    pf(work, "apply", "--out", str(lib))

    dest = Path(q(work, "SELECT dest_path FROM files")[0]["dest_path"])
    assert dest.suffix == ".jpg" and dest.exists()
    assert abs(dest.stat().st_mtime - OLD_MTIME) < 2  # FAT/exFAT tolerance


def test_sidecars_are_not_copied_into_the_library(photo_fixture: Path, tmp_path: Path):
    """.thm/.aae/.xmp are not photos: copying them littered the library with standalone
    'assets', each with its own bogus .thm.xmp provenance sidecar (review finding H5)."""
    work, lib = tmp_path / "work", tmp_path / "library"
    pf(work, "scan", str(photo_fixture))
    pf(work, "plan")
    pf(work, "apply", "--out", str(lib))

    names = [p.name.lower() for p in lib.rglob("*") if p.is_file()]
    assert not [n for n in names if n.endswith(".thm")]
    assert not [n for n in names if n.endswith(".thm.xmp")]
    # the only .xmp left is the provenance sidecar apply writes for the RAW keeper
    assert [n for n in names if n.endswith(".xmp")] == [n for n in names if n.endswith(".dng.xmp")]
    statuses = {
        Path(r["source_path"]).name: r["status"]
        for r in q(work, "SELECT source_path, status FROM files WHERE kind='sidecar'")
    }
    assert set(statuses.values()) == {"skipped_sidecar"}
    assert "IMG_0001.THM" in statuses and "mountain.xmp" in statuses


def test_copy_sidecars_true_restores_old_behaviour(photo_fixture: Path, tmp_path: Path):
    work, lib = tmp_path / "work", tmp_path / "library"
    work.mkdir(parents=True)
    (work / "photoflow.toml").write_text("copy_sidecars = true\n", encoding="utf-8")
    pf(work, "scan", str(photo_fixture))
    pf(work, "plan")
    pf(work, "apply", "--out", str(lib))

    names = [p.name.lower() for p in lib.rglob("*") if p.is_file()]
    assert [n for n in names if n.endswith(".thm")]
    statuses = {r["status"] for r in q(work, "SELECT status FROM files WHERE kind='sidecar'")}
    assert statuses == {"copied"}


def test_xmp_embed_error_logs_full_stderr(photo_fixture: Path, tmp_path: Path, monkeypatch):
    """The printed head is truncated to 3 lines, but exiftool names the failing file on
    every stderr line, so the audit detail must keep the full text to be repairable."""
    work, lib = tmp_path / "work", tmp_path / "library"
    pf(work, "scan", str(photo_fixture))
    pf(work, "plan")
    stderr = "Error: File not found - a.jpg\nError: x - b.jpg\nError: y - c.jpg\nError: z - d.jpg"
    monkeypatch.setattr(
        apply_mod, "exiftool_apply_argfile", lambda args: ExiftoolResult(1, stderr, "")
    )
    run_apply(work, lib)

    rows = q(work, "SELECT detail FROM actions WHERE action='xmp_embed_errors'")
    assert rows and "d.jpg" in rows[0]["detail"]
    copied = q(work, "SELECT status FROM files WHERE status='copied'")
    assert copied  # the exiftool failure never rolled back the copy


def test_sidecar_prune_and_reapply_round_trip(photo_fixture: Path, tmp_path: Path):
    """copy_sidecars=true -> prune -> plan resets it -> copy_sidecars=false converges."""
    work, lib = tmp_path / "work", tmp_path / "library"
    work.mkdir(parents=True)
    (work / "photoflow.toml").write_text("copy_sidecars = true\n", encoding="utf-8")
    pf(work, "scan", str(photo_fixture))
    pf(work, "plan")
    pf(work, "apply", "--out", str(lib))
    assert list(lib.rglob("*.thm"))

    conn = open_db(work)
    run_id = new_run(conn, "prune-sidecars", {})
    with open(work / "logs" / f"test_{run_id}.jsonl", "a", encoding="utf-8") as fh:
        cmd_prune_sidecars(conn, work, run_id, fh, Args(lib), load_config(work))
    conn.commit()
    conn.close()

    rows = q(work, "SELECT status, dest_path FROM files WHERE ext='.thm'")
    assert all(r["status"] == "skipped_sidecar" and r["dest_path"] is None for r in rows)
    assert not list(lib.rglob("*.thm"))

    pf(work, "plan")
    rows = q(work, "SELECT status FROM files WHERE ext='.thm'")
    assert all(r["status"] == "planned" for r in rows)  # non-durable: reset by plan

    (work / "photoflow.toml").write_text("copy_sidecars = false\n", encoding="utf-8")
    pf(work, "apply", "--out", str(lib))
    rows = q(work, "SELECT status FROM files WHERE ext='.thm'")
    assert all(r["status"] == "skipped_sidecar" for r in rows)
    assert not list(lib.rglob("*.thm"))
