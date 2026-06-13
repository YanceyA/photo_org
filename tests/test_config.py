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


def test_enrich_defaults():
    c = Config()
    assert c.enrich_tagger == "ram"
    assert c.enrich_min_cluster_size == 5
    assert 0.0 < c.enrich_cluster_prob_floor < 1.0
    assert c.tag_score_review < c.tag_score_accept
    assert c.face_device in ("auto", "cuda", "cpu")
    assert c.write_mwg_regions is True
    assert c.write_iptc_keywords is True


def test_enrich_toml_override(tmp_path: Path):
    (tmp_path / "photoflow.toml").write_text(
        'enrich_tagger = "clip"\nenrich_min_cluster_size = 8\nwrite_mwg_regions = false\n',
        encoding="utf-8",
    )
    c = load_config(tmp_path)
    assert c.enrich_tagger == "clip"
    assert c.enrich_min_cluster_size == 8
    assert c.write_mwg_regions is False
    assert c.near_dupe_threshold == 5  # untouched legacy key keeps default


def test_load_missing_file_gives_defaults(tmp_path: Path):
    assert load_config(tmp_path) == Config()


def test_toml_overrides(tmp_path: Path):
    (tmp_path / "photoflow.toml").write_text(
        "near_dupe_threshold = 8\nburst_window_s = 4\n", encoding="utf-8"
    )
    c = load_config(tmp_path)
    assert c.near_dupe_threshold == 8
    assert c.burst_window_s == 4
    assert c.min_year == 1990  # untouched keys keep defaults


def test_unknown_key_rejected(tmp_path: Path):
    (tmp_path / "photoflow.toml").write_text("near_dup_threshold = 8\n", encoding="utf-8")
    with pytest.raises(SystemExit):
        load_config(tmp_path)  # typo'd key must not be silently ignored
