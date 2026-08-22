"""exiftool wrapper: availability check, batched JSON reads, batched argfile writes."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

EXIF_TAGS = [
    # QuickTime Keys:CreationDate - tz-aware, written by iPhones, preferred for video.
    # Group-scoped on purpose: bare -CreationDate also matches XMP-pdf:CreationDate, which
    # would outrank DateTimeOriginal on a PDF-derived JPEG. -j still keys it "CreationDate".
    "-QuickTime:CreationDate",
    "-DateTimeOriginal",
    "-CreateDate",
    "-MediaCreateDate",
    "-Model",
    "-ImageWidth",
    "-ImageHeight",
]


def exiftool_available() -> bool:
    return shutil.which("exiftool") is not None


def exiftool_json(paths: list[str], batch_size: int = 200, *, fast: bool = True) -> dict[str, dict]:
    """Run exiftool on a batch of paths, return {path: tags}.

    fast=True adds -fast2, which stops reading before trailing metadata - a big speedup for
    JPEG/RAW. It MUST be False for QuickTime (MP4/MOV): those keep their moov atom at the END
    of the file, so -fast2 returns nothing at all (verified: exiftool 13.59 returns {} for a
    trailing-moov MP4 with -fast2 and CreateDate without it).

    -api QuickTimeUTC=1 is always on: QuickTime dates are UTC by spec, and without this the
    library files a midnight clip under the wrong day (12-13 h off in NZ). Note it converts
    CreateDate/MediaCreateDate to THIS machine's local zone at scan time, so the day a clip is
    filed under depends on where it was scanned; the tz-aware QuickTime CreationDate (preferred
    when present) is capture-local and passes through unconverted. The tradeoff: devices that
    write local time into mvhd despite the spec (some Android phones, GoPros, camcorders) come
    out shifted by the local offset - still a net win across the library, but a known one.
    """
    out: dict[str, dict] = {}
    for i in range(0, len(paths), batch_size):
        batch = paths[i : i + batch_size]
        with tempfile.NamedTemporaryFile("w", suffix=".args", delete=False, encoding="utf-8") as af:
            af.write("-j\n-n\n")
            if fast:
                af.write("-fast2\n")
            af.write("-api\nQuickTimeUTC=1\n-charset\nfilename=utf8\n")
            for t in EXIF_TAGS:
                af.write(t + "\n")
            for p in batch:
                af.write(p + "\n")
            argfile = af.name
        try:
            res = subprocess.run(
                ["exiftool", "-@", argfile],
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
            )
            if res.stdout.strip():
                for rec in json.loads(res.stdout):
                    # exiftool emits SourceFile with forward slashes even on
                    # Windows; normalize to the OS-native form so callers can
                    # look records up by the paths they passed in. (Without
                    # this, EXIF silently never lands in the manifest on
                    # Windows - latent bug inherited from the prototype.)
                    out[str(Path(rec.get("SourceFile", "")))] = rec
        except (json.JSONDecodeError, OSError) as e:
            print(f"  exiftool batch failed: {e}", file=sys.stderr)
        finally:
            os.unlink(argfile)
    return out


def read_keywords(paths: list[str], batch_size: int = 200) -> dict[str, set[str]]:
    """Read existing XMP dc:Subject + IPTC:Keywords for each path, as a set per path.

    Used by enrich apply to union new tags/people with what's already on the file (the
    provenance folder keywords apply wrote, plus any user edits) so the write is a superset
    and re-applying is idempotent. Missing tags are simply absent (exiftool omits them).
    """
    out: dict[str, set[str]] = {}
    for i in range(0, len(paths), batch_size):
        batch = paths[i : i + batch_size]
        with tempfile.NamedTemporaryFile("w", suffix=".args", delete=False, encoding="utf-8") as af:
            af.write("-j\n-charset\nfilename=utf8\n-XMP-dc:Subject\n-IPTC:Keywords\n")
            for p in batch:
                af.write(p + "\n")
            argfile = af.name
        try:
            res = subprocess.run(
                ["exiftool", "-@", argfile],
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
            )
            if res.stdout.strip():
                for rec in json.loads(res.stdout):
                    key = str(Path(rec.get("SourceFile", "")))
                    kws: set[str] = set()
                    for field in ("Subject", "Keywords"):
                        v = rec.get(field)
                        if isinstance(v, str):
                            kws.add(v)
                        elif isinstance(v, list):
                            kws.update(str(x) for x in v)
                    out[key] = kws
        except (json.JSONDecodeError, OSError) as e:
            print(f"  exiftool keyword read failed: {e}", file=sys.stderr)
        finally:
            os.unlink(argfile)
    return out


def exiftool_apply_argfile(lines: list[str]):
    """Run one exiftool process over a prepared -execute argfile (fast batching)."""
    if not lines:
        return
    with tempfile.NamedTemporaryFile("w", suffix=".args", delete=False, encoding="utf-8") as af:
        af.write("\n".join(lines) + "\n")
        argfile = af.name
    try:
        subprocess.run(
            ["exiftool", "-@", argfile, "-charset", "filename=utf8"], capture_output=True, text=True
        )
    finally:
        os.unlink(argfile)


def merge_metadata(donor_path: str, keeper_path: str) -> None:
    """Fill keeper's missing tags from donor. -wm cg = create-only, never overwrite."""
    subprocess.run(
        [
            "exiftool",
            "-overwrite_original",
            "-wm",
            "cg",
            "-tagsfromfile",
            donor_path,
            "-all:all",
            keeper_path,
        ],
        capture_output=True,
    )
