import subprocess
from pathlib import Path

import pytest

from photoflow.xmp import embed_args, xmp_sidecar


def test_embed_args_exact_lines():
    assert embed_args("d.jpg", "desc", ["k1", "k2"]) == [
        "-P",
        "-overwrite_original",
        "-XMP-dc:Description=desc",
        "-XMP-dc:Subject=k1",
        "-XMP-dc:Subject=k2",
        "d.jpg",
        "-execute",
    ]


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
