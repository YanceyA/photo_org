import random
import shutil
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest
from PIL import Image


def pf(workdir: Path, *args: str) -> subprocess.CompletedProcess:
    """Run photoflow as a subprocess against the given workdir."""
    proc = subprocess.run(
        [sys.executable, "-m", "photoflow", "--workdir", str(workdir), *args],
        capture_output=True,
        text=True,
    )
    if proc.returncode != 0:
        raise AssertionError(
            f"photoflow {' '.join(args)} failed ({proc.returncode}):\n{proc.stdout}\n{proc.stderr}"
        )
    return proc


def q(workdir: Path, sql: str, *params):
    """Query the manifest db, returning sqlite3.Row results."""
    conn = sqlite3.connect(workdir / "photoflow.db")
    conn.row_factory = sqlite3.Row
    try:
        return conn.execute(sql, params).fetchall()
    finally:
        conn.close()


def pytest_collection_modifyitems(config, items):
    have_exiftool = shutil.which("exiftool") is not None
    from photoflow.enrich.deps import HAVE_CLIP, HAVE_FACES

    have_enrich = HAVE_FACES and HAVE_CLIP
    skip_exif = pytest.mark.skip(reason="exiftool not on PATH")
    skip_enrich = pytest.mark.skip(reason="[enrich] model stack not installed")
    for item in items:
        if "exiftool" in item.keywords and not have_exiftool:
            item.add_marker(skip_exif)
        if "enrich" in item.keywords and not have_enrich:
            item.add_marker(skip_enrich)


def _gradient(w: int, h: int, seed: int) -> Image.Image:
    """Low-frequency seeded pattern: an 8x6 random image upscaled bilinearly.

    Resolution-independent, so pHash is stable across resizes and re-encodes;
    distinct seeds give mutually distant hashes (verified hamming >= 22).
    """
    rng = random.Random(seed)
    small = Image.new("RGB", (8, 6))
    px = small.load()
    for x in range(8):
        for y in range(6):
            px[x, y] = (rng.randrange(256), rng.randrange(256), rng.randrange(256))
    return small.resize((w, h), Image.Resampling.BILINEAR)


def _set_exif(path: Path, **tags: str) -> None:
    args = ["exiftool", "-overwrite_original"]
    args += [f"-{k}={v}" for k, v in tags.items()]
    subprocess.run(args + [str(path)], capture_output=True, check=True)


@pytest.fixture
def photo_fixture(tmp_path: Path) -> Path:
    """Synthetic source tree per HANDOFF §5. Requires exiftool (mark tests exiftool)."""
    src = tmp_path / "sources"
    old = src / "Old Laptop" / "Holiday 2015"
    rnd = src / "Random"
    phone = src / "Phone Backup" / "Camera"
    for d in (old, rnd, phone):
        d.mkdir(parents=True)

    # beach.jpg: EXIF date + camera model
    beach = old / "beach.jpg"
    _gradient(640, 480, seed=1).save(beach, "JPEG", quality=92)
    _set_exif(beach, DateTimeOriginal="2015:07:14 10:30:00", Model="Canon EOS 70D")

    # exact dupe of beach, different folder
    shutil.copy2(beach, rnd / "beach copy.jpg")

    # filename-date path (no EXIF)
    _gradient(640, 480, seed=2).save(phone / "IMG_20190304_101112.jpg", "JPEG", quality=92)

    # near-dupe pair: same scene, downscaled re-encode -> review queue
    # (seed 11: pre-checked big-vs-small pHash distance 0; seed 3 drifted to 6)
    big = _gradient(1000, 750, seed=11)
    big.save(rnd / "sunset_big.jpg", "JPEG", quality=92)
    big.resize((400, 300)).save(rnd / "sunset_small.jpg", "JPEG", quality=70)

    # RAW+JPEG pair: same stem; .dng must NOT be byte-identical (HANDOFF §7)
    mountain = old / "mountain.jpg"
    _gradient(640, 480, seed=4).save(mountain, "JPEG", quality=92)
    shutil.copy2(mountain, old / "mountain.dng")
    with open(old / "mountain.dng", "ab") as f:
        f.write(b"\x00RAWPAYLOAD")

    # folder-year date path
    _gradient(320, 240, seed=5).save(old / "no_meta.png", "PNG")

    # burst trio: same Model, DateTimeOriginal 2s apart, near-identical pixels
    for i in range(3):
        p = old / f"burst_{i}.jpg"
        img = _gradient(640, 480, seed=6)
        px = img.load()
        px[i, 0] = (255, 0, 0)  # not byte-identical, pHash-identical
        img.save(p, "JPEG", quality=92)
        _set_exif(p, DateTimeOriginal=f"2015:07:14 12:00:{i * 2:02d}", Model="Canon EOS 70D")

    # camera thumbnail sidecar next to its JPEG (Canon writes .THM beside .CRW/.JPG)
    _gradient(640, 480, seed=7).save(rnd / "IMG_0001.jpg", "JPEG", quality=92)
    _gradient(160, 120, seed=8).save(rnd / "IMG_0001.THM", "JPEG", quality=70)

    # pre-existing Lightroom/Capture One sidecar next to the RAW
    (old / "mountain.xmp").write_text(
        '<?xpacket begin=""?><x:xmpmeta xmlns:x="adobe:ns:meta/"/><?xpacket end="w"?>',
        encoding="utf-8",
    )

    return src
