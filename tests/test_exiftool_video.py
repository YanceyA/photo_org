"""Video metadata reads: -fast2 must not be used for QuickTime, and dates are UTC."""

import types
from datetime import UTC, datetime
from pathlib import Path

import pytest
from conftest import make_minimal_mp4

from photoflow.exiftool import EXIF_TAGS, exiftool_json

UTC_CREATION = datetime(2010, 9, 3, 16, 3, 31, tzinfo=UTC)


def test_creation_date_tag_is_requested():
    # QuickTime Keys:CreationDate is what iPhones write, and it carries a real tz offset.
    assert "-CreationDate" in EXIF_TAGS


@pytest.mark.exiftool
def test_fast_mode_misses_the_trailing_moov_and_slow_mode_finds_it(tmp_path: Path):
    clip = tmp_path / "clip.mp4"
    assert make_minimal_mp4(clip, UTC_CREATION) == 160

    fast = exiftool_json([str(clip)], 200, fast=True)
    assert "CreateDate" not in fast[str(clip)], "-fast2 must not be used for video (H1)"

    slow = exiftool_json([str(clip)], 200, fast=False)
    # QuickTimeUTC converts the stored UTC time to THIS machine's local zone, so compute
    # the expectation rather than hard-coding an offset.
    expect = UTC_CREATION.astimezone().strftime("%Y:%m:%d %H:%M:%S")
    assert str(slow[str(clip)]["CreateDate"]).startswith(expect)


def test_argfile_omits_fast2_when_fast_is_false_and_always_sets_quicktimeutc(monkeypatch):
    seen: list[str] = []

    def fake_run(argv, **kw):
        seen.append(Path(argv[argv.index("-@") + 1]).read_text(encoding="utf-8"))
        return types.SimpleNamespace(stdout="[]", stderr="", returncode=0)

    import photoflow.exiftool as et

    monkeypatch.setattr(et.subprocess, "run", fake_run)
    et.exiftool_json(["a.mp4"], 200, fast=False)
    et.exiftool_json(["b.jpg"], 200, fast=True)

    video_args, image_args = seen
    assert "-fast2\n" not in video_args
    assert "-fast2\n" in image_args
    for args in (video_args, image_args):
        assert "-api\nQuickTimeUTC=1\n" in args
        assert "-CreationDate\n" in args
