from photoflow.models import classify


def test_classify():
    assert classify(".jpg") == "image"
    assert classify(".cr2") == "raw"
    assert classify(".mp4") == "video"
    assert classify(".xmp") == "sidecar"
    assert classify(".txt") == "other"
