"""InsightFace wrapper: detect faces -> 512-d L2-normalized embeddings + pixel bboxes.

Heavy imports (insightface, cv2/numpy) are lazy so the module imports without the
[enrich] stack. `face_crop` is pure Pillow and unit-tested without any models.

Device note: face_providers() defaults to CPU because this rig (GTX 1080 Ti / Pascal +
Python 3.14, forcing onnxruntime-gpu>=1.26) hits the CUDA crash in onnxruntime #27588.
RAM++/CLIP still use the GPU via torch; only the onnxruntime face models run on CPU.
"""

from __future__ import annotations

from photoflow.enrich.deps import face_providers


def face_crop(img, bbox, pad: float):
    """Crop a padded face box out of a PIL image, clamped to the image bounds.

    `bbox` = (x1, y1, x2, y2) pixels; `pad` is a fraction of the box size added on each side
    (a little context makes the review thumbnail recognizable).
    """
    x1, y1, x2, y2 = bbox
    x1, x2 = sorted((x1, x2))
    y1, y2 = sorted((y1, y2))
    dx, dy = (x2 - x1) * pad, (y2 - y1) * pad
    left = int(max(0, round(x1 - dx)))
    top = int(max(0, round(y1 - dy)))
    right = int(min(img.width, round(x2 + dx)))
    bottom = int(min(img.height, round(y2 + dy)))
    return img.crop((left, top, right, bottom))


class FaceDetector:
    """Lazy InsightFace buffalo_l detector. Construct once, reuse across images."""

    def __init__(self, cfg):
        from insightface.app import FaceAnalysis

        providers = face_providers(cfg.face_device)
        self.app = FaceAnalysis(name="buffalo_l", providers=providers)
        ctx_id = 0 if cfg.face_device == "cuda" else -1
        self.app.prepare(ctx_id=ctx_id, det_size=(640, 640))
        self.min_score = cfg.enrich_face_min_score

    def detect(self, rgb) -> list[dict]:
        """Detect faces in an RGB numpy image. Returns dicts with a float32 L2-normalized
        512-d embedding, a pixel bbox (x1,y1,x2,y2), and the detection score."""
        import numpy as np

        bgr = np.ascontiguousarray(rgb[:, :, ::-1])  # InsightFace expects OpenCV BGR order
        out = []
        for f in self.app.get(bgr):
            if float(f.det_score) < self.min_score:
                continue
            out.append(
                {
                    "embedding": f.normed_embedding.astype("float32"),
                    "bbox": tuple(float(v) for v in f.bbox),
                    "det_score": float(f.det_score),
                }
            )
        return out
