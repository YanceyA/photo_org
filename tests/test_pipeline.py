"""End-to-end pipeline test per HANDOFF §5, incl. the §7 incremental regression round."""

import csv
import shutil
import subprocess
from pathlib import Path

import pytest
from conftest import _gradient, pf, q

pytestmark = pytest.mark.exiftool


def tree(root: Path) -> set[str]:
    return {str(p.relative_to(root)) for p in root.rglob("*") if p.is_file()}


def test_full_pipeline(photo_fixture: Path, tmp_path: Path):
    work = tmp_path / "work"
    lib = tmp_path / "library"

    # ---- 1. scan + plan
    pf(work, "scan", str(photo_fixture))
    pf(work, "plan")

    roles = {r["role"]: r["c"] for r in q(work, "SELECT role, COUNT(*) c FROM files GROUP BY role")}
    assert roles.get("exact_dupe") == 1  # beach copy.jpg
    assert roles.get("raw_jpeg_pair") == 2  # mountain.jpg + mountain.dng
    assert roles.get("burst") == 3  # burst trio kept silently
    assert roles.get("review") == 2  # sunset big + small

    date_sources = {
        r["s"]: r["c"]
        for r in q(work, "SELECT date_source s, COUNT(*) c FROM files GROUP BY date_source")
    }
    assert date_sources.get("filename") == 1  # IMG_20190304_101112.jpg
    assert date_sources.get("folder") >= 1  # no_meta.png
    assert date_sources.get("exif") == 5  # beach + copy + 3 bursts

    # ---- 2. review -> fill decisions -> apply
    pf(work, "review")
    assert (work / "review.html").exists()
    dec = work / "decisions.csv"
    with open(dec, newline="", encoding="utf-8") as f:
        rows = list(csv.DictReader(f))
    big = next(r for r in rows if "sunset_big" in r["source_path"])
    small = next(r for r in rows if "sunset_small" in r["source_path"])
    big["decision"], big["merge_from_file_id"] = "keep", small["file_id"]
    small["decision"] = "skip"
    with open(dec, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=rows[0].keys())
        w.writeheader()
        w.writerows(rows)

    pf(work, "apply", "--out", str(lib))

    files = tree(lib)
    beach_dests = [f for f in files if "beach" in f]
    assert len(beach_dests) == 1
    assert beach_dests[0] == str(Path("2015") / "07" / Path(beach_dests[0]).name)
    assert Path(beach_dests[0]).name.startswith("20150714_103000_")
    assert any(f.endswith(".dng.xmp") for f in files)  # RAW sidecar
    # slugify maps "sunset_small" -> "sunset-small" in dest names
    assert not any("sunset-small" in f for f in files)
    assert any("sunset-big" in f for f in files)
    assert any(f.startswith(str(Path("2015"))) and "no-meta" in f for f in files)  # folder-date PNG

    # XMP description on beach keeper carries BOTH source rel-paths
    beach_path = lib / beach_dests[0]
    out = subprocess.run(
        ["exiftool", "-j", "-XMP-dc:Description", str(beach_path)],
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    assert "beach.jpg" in out
    assert "beach copy.jpg" in out

    statuses = {
        r["status"]: r["c"] for r in q(work, "SELECT status, COUNT(*) c FROM files GROUP BY status")
    }
    assert statuses.get("skipped_manual") == 1
    assert statuses.get("skipped_dupe") == 1

    # ---- 3. incremental regression round (HANDOFF §7 bug)
    inc = tmp_path / "usb_stick"
    inc.mkdir()
    shutil.copy2(
        photo_fixture / "Old Laptop" / "Holiday 2015" / "beach.jpg", inc / "beach again.jpg"
    )
    _gradient(640, 480, seed=9).save(inc / "brand_new.jpg", "JPEG", quality=92)

    before = tree(lib)
    pf(work, "scan", str(inc))
    pf(work, "plan")
    pf(work, "apply", "--out", str(lib))
    after = tree(lib)

    new_files = after - before
    assert len(new_files) == 1  # exactly ONE new copy
    assert "brand-new" in next(iter(new_files))
    small_status = q(work, "SELECT status FROM files WHERE source_path LIKE ?", "%sunset_small%")[
        0
    ]["status"]
    assert small_status == "skipped_manual"  # decision survived re-plan
    big_rows = q(work, "SELECT status FROM files WHERE source_path LIKE ?", "%sunset_big%")
    assert big_rows[0]["status"] == "copied"  # not re-copied, not re-queued

    # ---- 4. review regeneration keeps decisions
    # Arbitrated against the reference: review exports only rows still in the
    # queue (role='review'). sunset_big is copied and its only twin is decided,
    # so it legitimately leaves the queue; sunset_small's skip carries forward.
    pf(work, "review")
    with open(dec, newline="", encoding="utf-8") as f:
        rows2 = list(csv.DictReader(f))
    kept = {r["file_id"]: r["decision"] for r in rows2 if r["decision"]}
    assert kept == {small["file_id"]: "skip"}
    assert not any(r["file_id"] == big["file_id"] for r in rows2)

    # ---- 5. dry-run changes nothing
    snapshot = tree(lib)
    pf(work, "apply", "--out", str(lib), "--dry-run")
    assert tree(lib) == snapshot


def test_planner_roles_are_known(photo_fixture: Path, tmp_path: Path):
    from photoflow.models import ROLES

    work = tmp_path / "work"
    pf(work, "scan", str(photo_fixture))
    pf(work, "plan")
    seen = {r["role"] for r in q(work, "SELECT DISTINCT role FROM files WHERE role IS NOT NULL")}
    assert seen <= ROLES
