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
    "-DateTimeOriginal",
    "-CreateDate",
    "-MediaCreateDate",
    "-Model",
    "-ImageWidth",
    "-ImageHeight",
]


def exiftool_available() -> bool:
    return shutil.which("exiftool") is not None


def exiftool_json(paths: list[str], batch_size: int = 200) -> dict[str, dict]:
    """Run exiftool on a batch of paths, return {path: tags}."""
    out: dict[str, dict] = {}
    for i in range(0, len(paths), batch_size):
        batch = paths[i : i + batch_size]
        with tempfile.NamedTemporaryFile("w", suffix=".args", delete=False, encoding="utf-8") as af:
            af.write("-j\n-n\n-fast2\n-charset\nfilename=utf8\n")
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
