"""Content and perceptual hashing with optional-dependency detection."""

from __future__ import annotations

import hashlib
from pathlib import Path

from photoflow.bktree import hamming  # noqa: F401

try:
    from PIL import Image

    HAVE_PIL = True
except ImportError:
    HAVE_PIL = False

try:
    import imagehash

    HAVE_IMAGEHASH = HAVE_PIL
except ImportError:
    HAVE_IMAGEHASH = False

try:
    import pillow_heif

    pillow_heif.register_heif_opener()
    HAVE_HEIF = True
except ImportError:
    HAVE_HEIF = False


def content_hash(path: Path) -> str:
    h = hashlib.blake2b(digest_size=20)
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def perceptual_hash(path: Path) -> str | None:
    if not HAVE_IMAGEHASH:
        return None
    try:
        with Image.open(path) as im:
            return str(imagehash.phash(im))
    except Exception:
        return None
