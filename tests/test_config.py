from pathlib import Path

import pytest

from photoflow.config import Config, load_config


def test_defaults_match_legacy_constants():
    c = Config()
    assert c.near_dupe_threshold == 5
    assert c.burst_window_s == 10
    assert c.min_year == 1990
    assert c.slug_max == 40
    assert c.exiftool_batch == 200
    assert ".jpg" in c.image_ext and ".cr2" in c.raw_ext and ".mp4" in c.video_ext


def test_load_missing_file_gives_defaults(tmp_path: Path):
    assert load_config(tmp_path) == Config()


def test_toml_overrides(tmp_path: Path):
    (tmp_path / "photoflow.toml").write_text(
        "near_dupe_threshold = 8\nburst_window_s = 4\n", encoding="utf-8")
    c = load_config(tmp_path)
    assert c.near_dupe_threshold == 8
    assert c.burst_window_s == 4
    assert c.min_year == 1990  # untouched keys keep defaults


def test_unknown_key_rejected(tmp_path: Path):
    (tmp_path / "photoflow.toml").write_text("near_dup_threshold = 8\n", encoding="utf-8")
    with pytest.raises(SystemExit):
        load_config(tmp_path)  # typo'd key must not be silently ignored
