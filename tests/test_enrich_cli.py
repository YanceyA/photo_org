"""CLI wiring for the nested `enrich` subcommands (no models / no [enrich] stack needed)."""

from pathlib import Path

from conftest import pf


def test_enrich_status_runs(tmp_path: Path):
    out = pf(tmp_path / "wd", "enrich", "status").stdout.lower()
    assert "faces" in out and "tags" in out


def test_enrich_scan_without_stack_exits_cleanly(tmp_path: Path):
    # No copied files in a fresh workdir -> clean "nothing to do", exit 0.
    proc = pf(tmp_path / "wd", "enrich", "scan")
    assert proc.returncode == 0


def test_enrich_apply_dry_run_parses(tmp_path: Path):
    proc = pf(tmp_path / "wd", "enrich", "apply", "--dry-run")
    assert proc.returncode == 0


def test_enrich_requires_a_step(tmp_path: Path):
    import subprocess
    import sys

    proc = subprocess.run(
        [sys.executable, "-m", "photoflow", "--workdir", str(tmp_path / "wd"), "enrich"],
        capture_output=True,
        text=True,
    )
    assert proc.returncode != 0  # argparse requires a sub-step
