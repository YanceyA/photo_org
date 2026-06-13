"""Content tagging: RAM++ (primary) with CLIP/SigLIP zero-shot fallback.

Heavy imports (torch, open_clip, ram) are lazy - this module imports cleanly with none of
them installed, so the pure helpers below (classify_tag, vocab, checkpoint path) stay
CI-testable. Model wrappers carry skip-marked smoke tests.

RAM++ `inference_ram` returns thresholded tags with NO per-tag score, so RamTagger yields
(tag, None) and those are trusted as 'auto'. CLIP/SigLIP returns calibrated per-tag scores
that the scan step bands into auto/review/dropped via classify_tag. SigLIP (the default,
ViT-SO400M-16-SigLIP2-384/webli) is used for tagging because its sigmoid loss gives genuinely
independent multi-label probabilities - softmax-over-vocab would force a single winner.

Each tag is scored with a SINGLE prompt (`cfg.clip_prompt`), not prompt-ensembling: averaging
normalized text embeddings moves them toward the prompts' centroid, lowering the image cosine
and breaking SigLIP's logit_bias calibration (a clear "cat" fell from ~0.20 to ~0.09).
"""

from __future__ import annotations

from pathlib import Path

RAM_CHECKPOINT_NAME = "ram_plus_swin_large_14m.pth"
RAM_HF_REPO = "xinyu1205/recognize-anything-plus-model"

# Family-photo zero-shot vocabulary for the CLIP fallback (pure data).
FAMILY_VOCAB: dict[str, list[str]] = {
    "scene": [
        "beach",
        "mountains",
        "forest",
        "city street",
        "indoors",
        "kitchen",
        "living room",
        "backyard",
        "park",
        "snow",
        "desert",
        "lake",
        "sunset",
        "night",
        "garden",
    ],
    "event": [
        "birthday party",
        "wedding",
        "graduation",
        "holiday celebration",
        "christmas",
        "halloween",
        "vacation",
        "road trip",
        "sports game",
        "concert",
        "picnic",
        "barbecue",
    ],
    "people": [
        "baby",
        "toddler",
        "child",
        "group of people",
        "family portrait",
        "couple",
        "self portrait",
        "crowd",
    ],
    "animal": ["dog", "cat", "bird", "horse", "fish", "wildlife"],
    "object": [
        "food",
        "birthday cake",
        "car",
        "boat",
        "bicycle",
        "flowers",
        "balloons",
        "christmas tree",
        "fireworks",
        "toys",
        "books",
    ],
    "activity": [
        "swimming",
        "hiking",
        "skiing",
        "dancing",
        "cooking",
        "playing music",
        "opening presents",
        "blowing out candles",
    ],
    "format": ["selfie", "screenshot", "document", "scanned photo", "black and white photo"],
}


def vocab_tags() -> list[str]:
    """Flat, deduped, sorted list of all candidate CLIP tags."""
    return sorted({t for tags in FAMILY_VOCAB.values() for t in tags})


def classify_tag(score: float | None, accept: float, review: float) -> str | None:
    """Band a tag by confidence: None/>=accept -> 'auto'; [review,accept) -> 'review';
    below review -> dropped (None)."""
    if score is None or score >= accept:
        return "auto"
    if score >= review:
        return "review"
    return None


def ram_checkpoint_path(cfg, workdir: Path) -> Path:
    """Resolve the RAM++ checkpoint path: cfg.ram_checkpoint if set, else workdir/models/."""
    if cfg.ram_checkpoint:
        return Path(cfg.ram_checkpoint)
    return Path(workdir) / "models" / RAM_CHECKPOINT_NAME


def ensure_ram_checkpoint(path: Path) -> Path:
    """Download the 3 GB RAM++ checkpoint from Hugging Face if it isn't already on disk."""
    path = Path(path)
    if path.exists():
        return path
    from huggingface_hub import hf_hub_download

    path.parent.mkdir(parents=True, exist_ok=True)
    downloaded = hf_hub_download(
        repo_id=RAM_HF_REPO, filename=RAM_CHECKPOINT_NAME, local_dir=str(path.parent)
    )
    return Path(downloaded)


class RamTagger:
    """RAM++ (swin_l, 14M) tagger. Pure torch; runs on cfg.enrich_device."""

    source = "ram"

    def __init__(self, cfg, workdir):
        import torch
        from ram import get_transform, inference_ram
        from ram.models import ram_plus

        from photoflow.enrich.deps import torch_device

        ckpt = ensure_ram_checkpoint(ram_checkpoint_path(cfg, workdir))
        self.device = torch_device(cfg.enrich_device)
        model = ram_plus(pretrained=str(ckpt), image_size=cfg.ram_image_size, vit="swin_l")
        model.eval()
        self.model = model.to(self.device)
        self.transform = get_transform(image_size=cfg.ram_image_size)
        self._inference = inference_ram
        self._torch = torch

    def tag(self, pil_image) -> list[tuple[str, None]]:
        img = self.transform(pil_image.convert("RGB")).unsqueeze(0).to(self.device)
        with self._torch.no_grad():
            english, _chinese = self._inference(img, self.model)
        return [(t.strip(), None) for t in english.split("|") if t.strip()]


class ClipTagger:
    """open_clip zero-shot multi-label tagger. SigLIP models give calibrated sigmoid scores;
    plain CLIP falls back to raw cosine similarity."""

    source = "clip"

    def __init__(self, cfg):
        import open_clip
        import torch

        from photoflow.enrich.deps import torch_device

        self.device = torch_device(cfg.enrich_device)
        model, _, preprocess = open_clip.create_model_and_transforms(
            cfg.clip_model, pretrained=cfg.clip_pretrained
        )
        self.model = model.to(self.device).eval()
        self.preprocess = preprocess
        self.tokenizer = open_clip.get_tokenizer(cfg.clip_model)
        self.tags = vocab_tags()
        self.prompt = cfg.clip_prompt
        self.is_siglip = hasattr(model, "logit_bias") and model.logit_bias is not None
        self._torch = torch
        self.text_bank = self._build_text_bank()

    def _build_text_bank(self):
        # One prompt per tag, encoded in a single batched forward pass; normalized once.
        torch = self._torch
        with torch.no_grad():
            toks = self.tokenizer([self.prompt.format(t) for t in self.tags]).to(self.device)
            e = self.model.encode_text(toks)
            e = e / e.norm(dim=-1, keepdim=True)
        return e

    def tag(self, pil_image) -> list[tuple[str, float]]:
        torch = self._torch
        img = self.preprocess(pil_image.convert("RGB")).unsqueeze(0).to(self.device)
        with torch.no_grad():
            f = self.model.encode_image(img)
            f = f / f.norm(dim=-1, keepdim=True)
            cos = (f @ self.text_bank.T).squeeze(0)
            if self.is_siglip:
                scores = torch.sigmoid(self.model.logit_scale.exp() * cos + self.model.logit_bias)
            else:
                scores = cos
        return [(tag, float(s)) for tag, s in zip(self.tags, scores)]  # noqa: B905


def build_tagger(cfg, workdir=None):
    """Construct the configured tagger, falling back RAM++ -> CLIP on failure.

    Returns a tagger exposing .tag(pil_image) -> list[(tag, score|None)], or None if no
    tagger could be built (caller logs the reason and skips content tagging). A RAM++ load
    failure (e.g. its `transformers` dep isn't installed) never crashes the scan: it prints
    an actionable note and falls back to CLIP. enrich_tagger='clip' forces CLIP and skips RAM.
    """
    from photoflow.enrich.deps import HAVE_CLIP, HAVE_RAM

    pref = cfg.enrich_tagger
    if pref in ("ram", "auto") and HAVE_RAM:
        try:
            return RamTagger(cfg, workdir)
        except Exception as e:
            print(f"NOTE: RAM++ tagger could not load ({type(e).__name__}: {e}).")
            if HAVE_CLIP:
                print(
                    "      Falling back to CLIP/SigLIP. (RAM++ needs an old transformers "
                    "~4.25 that won't run on Python 3.14 - see README.)"
                )
            else:
                print(
                    "      RAM++ needs transformers ~4.25 (see README) or set enrich_tagger='clip'."
                )
    if pref in ("ram", "clip", "auto") and HAVE_CLIP:
        try:
            return ClipTagger(cfg)
        except Exception as e:
            print(f"NOTE: CLIP tagger could not load ({type(e).__name__}: {e}).")
    return None
