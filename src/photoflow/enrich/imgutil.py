"""Small image helpers shared by enrich scan/review: HEIC-aware open + thumbnail."""

from __future__ import annotations

from pathlib import Path

_HEIF_REGISTERED = False


def _register_heif() -> None:
    global _HEIF_REGISTERED
    if _HEIF_REGISTERED:
        return
    try:
        import pillow_heif

        pillow_heif.register_heif_opener()
    except Exception:
        pass
    _HEIF_REGISTERED = True


def open_rgb(path: str):
    """Open an image as RGB (registers the HEIC opener if pillow-heif is present)."""
    from PIL import Image

    _register_heif()
    return Image.open(path).convert("RGB")


def make_thumb(src_path: str, dest_path, size: int = 256) -> None:
    """Write a downscaled JPEG thumbnail of src_path to dest_path (skips if it exists)."""
    from PIL import Image

    if Path(dest_path).exists():
        return
    _register_heif()
    with Image.open(src_path) as im:
        im = im.convert("RGB")
        im.thumbnail((size, size))
        im.save(dest_path, "JPEG", quality=80)
