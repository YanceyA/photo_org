"""Pure helpers inside the model wrappers (crop geometry, vocab, checkpoint path).

The model-dependent paths are exercised by @pytest.mark.enrich smoke tests that skip
unless the [enrich] stack is installed.
"""

from pathlib import Path

import pytest
from PIL import Image

from photoflow.config import Config
from photoflow.enrich.faces import face_crop
from photoflow.enrich.tagger import FAMILY_VOCAB, ram_checkpoint_path, vocab_tags


def test_face_crop_expands_by_pad_and_clamps():
    img = Image.new("RGB", (100, 100))
    # centered 20x20 box, pad 0.5 -> +10 each side -> (30,30,70,70) -> 40x40 crop
    assert face_crop(img, (40, 40, 60, 60), pad=0.5).size == (40, 40)
    # box at the corner clamps to the image edge instead of going negative
    assert face_crop(img, (0, 0, 20, 20), pad=0.5).size == (30, 30)


def test_face_crop_zero_pad():
    img = Image.new("RGB", (100, 100))
    assert face_crop(img, (10, 20, 50, 80), pad=0.0).size == (40, 60)


def test_vocab_tags_flat_unique_nonempty():
    tags = vocab_tags()
    assert "beach" in tags and "dog" in tags and "birthday cake" in tags
    assert len(tags) == len(set(tags))  # deduped
    assert len(tags) > 30
    assert all(isinstance(t, str) and t for t in tags)


def test_vocab_has_expected_categories():
    assert {"scene", "event", "animal"} <= set(FAMILY_VOCAB)


def test_ram_checkpoint_path_default_and_override(tmp_path: Path):
    cfg = Config()
    assert ram_checkpoint_path(cfg, tmp_path) == tmp_path / "models" / "ram_plus_swin_large_14m.pth"
    cfg2 = Config(ram_checkpoint=str(tmp_path / "custom.pth"))
    assert ram_checkpoint_path(cfg2, tmp_path) == tmp_path / "custom.pth"


@pytest.mark.enrich
def test_face_detector_constructs():
    # Smoke: the InsightFace wrapper builds and exposes detect(); buffalo_l auto-downloads.
    from photoflow.enrich.faces import FaceDetector

    det = FaceDetector(Config())
    assert hasattr(det, "detect")


@pytest.mark.enrich
def test_clip_tagger_tags_a_blank_image():
    from photoflow.enrich.tagger import build_tagger

    tagger = build_tagger(Config(enrich_tagger="clip"))
    assert tagger is not None
    out = tagger.tag(Image.new("RGB", (224, 224), (120, 120, 120)))
    assert isinstance(out, list)
    for tag, score in out:
        assert isinstance(tag, str)
        assert score is None or 0.0 <= float(score) <= 1.0
