"""Optional-dependency gates + device/provider selection for the enrich models.

Gating mirrors hashing.HAVE_PIL: presence is probed with importlib (no heavy import at
module load), and callers degrade gracefully when a model library is absent. Heavy
libraries (torch, insightface, onnxruntime, open_clip, ram) are imported lazily, only
inside the functions that actually run inference.
"""

from __future__ import annotations

import importlib.util


def _have(module: str) -> bool:
    try:
        return importlib.util.find_spec(module) is not None
    except (ImportError, ValueError):
        return False


HAVE_TORCH = _have("torch")
HAVE_INSIGHTFACE = _have("insightface")
HAVE_ONNXRUNTIME = _have("onnxruntime")  # the gpu variant also imports as 'onnxruntime'
HAVE_OPENCLIP = _have("open_clip")
HAVE_SKLEARN = _have("sklearn")
HAVE_RAM = _have("ram")
HAVE_CV2 = _have("cv2")

HAVE_FACES = HAVE_INSIGHTFACE and HAVE_ONNXRUNTIME and HAVE_CV2
HAVE_CLIP = HAVE_TORCH and HAVE_OPENCLIP


def face_providers(pref: str = "auto") -> list[str]:
    """onnxruntime execution providers for InsightFace.

    'auto' resolves to CPU on purpose: this project's rig (GTX 1080 Ti / Pascal sm_61 +
    Python 3.14, which forces onnxruntime-gpu>=1.26) hits the CUDA crash in onnxruntime
    issue #27588. Opt into GPU explicitly with face_device='cuda' once on a working stack.
    """
    if pref == "cuda":
        return ["CUDAExecutionProvider", "CPUExecutionProvider"]
    return ["CPUExecutionProvider"]


def torch_device(pref: str = "auto") -> str:
    """Resolve a torch device string for RAM++/CLIP. 'cpu' short-circuits without torch."""
    if pref == "cpu":
        return "cpu"
    if not HAVE_TORCH:
        return "cpu"
    import torch

    if pref == "cuda":
        return "cuda"
    return "cuda" if torch.cuda.is_available() else "cpu"
