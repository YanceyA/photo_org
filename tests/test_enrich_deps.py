"""Optional-dependency gates, device selection, and tag-score thresholding (pure)."""

import subprocess
import sys

from conftest import pf

from photoflow.enrich import deps
from photoflow.enrich.tagger import classify_tag


def test_have_flags_are_bools():
    for name in (
        "HAVE_TORCH",
        "HAVE_INSIGHTFACE",
        "HAVE_ONNXRUNTIME",
        "HAVE_OPENCLIP",
        "HAVE_SKLEARN",
        "HAVE_RAM",
        "HAVE_CV2",
    ):
        assert isinstance(getattr(deps, name), bool)


def test_face_providers_defaults_to_cpu_on_this_rig():
    # 'auto' must mean CPU: Pascal (1080 Ti) + Py3.14 + onnxruntime-gpu>=1.26 crashes (#27588).
    assert deps.face_providers("auto") == ["CPUExecutionProvider"]
    assert deps.face_providers("cpu") == ["CPUExecutionProvider"]
    assert deps.face_providers("cuda") == ["CUDAExecutionProvider", "CPUExecutionProvider"]


def test_torch_device_cpu_needs_no_torch():
    # Explicit 'cpu' short-circuits before importing torch, so it works with no torch present.
    assert deps.torch_device("cpu") == "cpu"


def test_classify_tag_bands():
    # CLIP/SigLIP scores fall into three bands.
    assert classify_tag(0.80, accept=0.5, review=0.32) == "auto"
    assert classify_tag(0.40, accept=0.5, review=0.32) == "review"
    assert classify_tag(0.20, accept=0.5, review=0.32) is None  # dropped
    # Boundaries are inclusive at the lower edge of each accepted band.
    assert classify_tag(0.50, accept=0.5, review=0.32) == "auto"
    assert classify_tag(0.32, accept=0.5, review=0.32) == "review"


def test_classify_tag_none_score_auto_accepts():
    # RAM++ inference returns tags without scores; those are trusted (RAM self-thresholds).
    assert classify_tag(None, accept=0.5, review=0.32) == "auto"


def test_core_cli_imports_without_numpy_or_sklearn():
    """Core commands must not hard-require the [enrich] stack.

    `pip install photoflow` pulls only pillow-heif, but cli.py used to import all seven
    enrich command modules at load time and enrich/clustering.py imported numpy at module
    level, so `photoflow status` died with ModuleNotFoundError before argparse ran (R4).
    Blocking the modules in a subprocess reproduces a bare install without uninstalling
    anything.
    """
    code = (
        "import sys;"
        " sys.modules['numpy'] = None;"
        " sys.modules['sklearn'] = None;"
        " import photoflow.cli;"
        " print('ok')"
    )
    proc = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True)
    assert proc.returncode == 0, proc.stderr
    assert "ok" in proc.stdout


def test_status_still_dispatches(tmp_path):
    # The lazy-import refactor must not break the enrich dispatch or the core dispatch.
    # cmd_status prints "by status:" / "by role:" / "by date source:" (no literal "manifest").
    assert "by status" in pf(tmp_path / "wd", "status").stdout.lower()
    assert "faces" in pf(tmp_path / "wd", "enrich", "status").stdout.lower()
