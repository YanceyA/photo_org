"""exiftool wrapper: availability check, batched JSON reads, batched argfile writes."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile

EXIFTOOL_BATCH = 200

EXIF_TAGS = ["-DateTimeOriginal", "-CreateDate", "-MediaCreateDate",
             "-Model", "-ImageWidth", "-ImageHeight"]


def exiftool_available() -> bool:
    return shutil.which("exiftool") is not None


def exiftool_json(paths: list[str]) -> dict[str, dict]:
    """Run exiftool on a batch of paths, return {path: tags}."""
    out: dict[str, dict] = {}
    for i in range(0, len(paths), EXIFTOOL_BATCH):
        batch = paths[i:i + EXIFTOOL_BATCH]
        with tempfile.NamedTemporaryFile("w", suffix=".args", delete=False,
                                         encoding="utf-8") as af:
            af.write("-j\n-n\n-fast2\n-charset\nfilename=utf8\n")
            for t in EXIF_TAGS:
                af.write(t + "\n")
            for p in batch:
                af.write(p + "\n")
            argfile = af.name
        try:
            res = subprocess.run(["exiftool", "-@", argfile],
                                 capture_output=True, text=True,
                                 encoding="utf-8", errors="replace")
            if res.stdout.strip():
                for rec in json.loads(res.stdout):
                    out[rec.get("SourceFile", "")] = rec
        except (json.JSONDecodeError, OSError) as e:
            print(f"  exiftool batch failed: {e}", file=sys.stderr)
        finally:
            os.unlink(argfile)
    return out


def exiftool_apply_argfile(lines: list[str]):
    """Run one exiftool process over a prepared -execute argfile (fast batching)."""
    if not lines:
        return
    with tempfile.NamedTemporaryFile("w", suffix=".args", delete=False,
                                     encoding="utf-8") as af:
        af.write("\n".join(lines) + "\n")
        argfile = af.name
    try:
        subprocess.run(["exiftool", "-@", argfile, "-charset", "filename=utf8"],
                       capture_output=True, text=True)
    finally:
        os.unlink(argfile)


def merge_metadata(donor_path: str, keeper_path: str) -> None:
    """Fill keeper's missing tags from donor. -wm cg = create-only, never overwrite."""
    subprocess.run(["exiftool", "-overwrite_original", "-wm", "cg",
                    "-tagsfromfile", donor_path, "-all:all", keeper_path],
                   capture_output=True)
