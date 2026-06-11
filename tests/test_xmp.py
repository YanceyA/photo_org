import subprocess
from pathlib import Path

import pytest

from photoflow.xmp import xmp_sidecar


def test_sidecar_path_and_content(tmp_path: Path):
    dest = tmp_path / "photo.dng"
    dest.touch()
    xmp_sidecar(dest, "photoflow src: A/b.jpg | C/d.jpg", ["Holiday 2015", "A&B"])
    sc = tmp_path / "photo.dng.xmp"
    assert sc.exists()
    text = sc.read_text(encoding="utf-8")
    assert "photoflow src: A/b.jpg | C/d.jpg" in text
    assert "<rdf:li>Holiday 2015</rdf:li>" in text
    assert "A&amp;B" in text  # html-escaped


@pytest.mark.exiftool
def test_sidecar_parses_with_exiftool(tmp_path: Path):
    dest = tmp_path / "photo.dng"
    dest.touch()
    xmp_sidecar(dest, "desc here", ["kw1"])
    out = subprocess.run(
        [
            "exiftool",
            "-j",
            "-XMP-dc:Description",
            "-XMP-dc:Subject",
            str(tmp_path / "photo.dng.xmp"),
        ],
        capture_output=True,
        text=True,
        check=True,
    )
    assert "desc here" in out.stdout and "kw1" in out.stdout
