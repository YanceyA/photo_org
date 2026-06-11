from photoflow.config import Config
from photoflow.models import classify


def test_classify():
    cfg = Config()
    assert classify(".jpg", cfg) == "image"
    assert classify(".cr2", cfg) == "raw"
    assert classify(".mp4", cfg) == "video"
    assert classify(".xmp", cfg) == "sidecar"
    assert classify(".txt", cfg) == "other"
