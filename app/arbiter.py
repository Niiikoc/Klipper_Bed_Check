"""Zero-shot semantic arbiter.

The classical detector localises *something changed here*. CLIP answers the
only question that is left: is that region a printed part, or is it a shadow /
reflection / lighting artefact? It runs only when the detector already raised a
flag, so its cost is irrelevant in practice.

No training and no labelled data are involved — the prompts are the model."""
from __future__ import annotations

import logging
import threading

import cv2
import numpy as np

log = logging.getLogger("bedcheck.arbiter")

_lock = threading.Lock()
_state: dict = {"model": None, "processor": None, "name": None, "error": None}


def available() -> bool:
    """True only if every dependency judge() needs is importable."""
    try:
        import torch  # noqa: F401
        from PIL import Image  # noqa: F401
        from transformers import CLIPModel, CLIPProcessor  # noqa: F401
    except Exception:
        return False
    return True


def _ensure(name: str):
    with _lock:
        if _state["model"] is not None and _state["name"] == name:
            return _state["model"], _state["processor"]
        from transformers import CLIPModel, CLIPProcessor

        log.info("loading CLIP arbiter %s", name)
        model = CLIPModel.from_pretrained(name)
        model.eval()
        processor = CLIPProcessor.from_pretrained(name)
        _state.update(model=model, processor=processor, name=name, error=None)
        return model, processor


def _crop(warped: np.ndarray, bbox_px, ppm: float, context_mm: float):
    h, w = warped.shape[:2]
    x, y, bw, bh = bbox_px
    cx, cy = x + bw / 2.0, y + bh / 2.0
    side = max(bw, bh, context_mm * ppm)
    half = side / 2.0
    x0 = int(max(0, min(w - 1, cx - half)))
    y0 = int(max(0, min(h - 1, cy - half)))
    x1 = int(max(x0 + 1, min(w, cx + half)))
    y1 = int(max(y0 + 1, min(h, cy + half)))
    return warped[y0:y1, x0:x1]


def judge(warped: np.ndarray, blobs, ppm: float, cfg: dict) -> dict:
    """Return {'ran', 'p_occupied', 'per_blob', 'error'}."""
    out = {"ran": False, "p_occupied": None, "per_blob": [], "error": None}
    if not cfg.get("enabled") or not blobs:
        return out
    if not available():
        out["error"] = "arbiter deps not installed in this image"
        return out

    try:
        import torch
        from PIL import Image

        model, processor = _ensure(cfg["model"])
        occupied = list(cfg["prompts"]["occupied"])
        clear = list(cfg["prompts"]["clear"])
        prompts = occupied + clear

        crops = [
            Image.fromarray(cv2.cvtColor(
                _crop(warped, b.bbox_px, ppm, float(cfg["crop_context_mm"])),
                cv2.COLOR_BGR2RGB))
            for b in blobs[:4]
        ]
        inputs = processor(text=prompts, images=crops,
                           return_tensors="pt", padding=True)
        with torch.no_grad():
            probs = model(**inputs).logits_per_image.softmax(dim=1).numpy()

        per_blob = [float(row[:len(occupied)].sum()) for row in probs]
        out.update(ran=True, per_blob=per_blob, p_occupied=max(per_blob))
    except Exception as exc:  # fail open: never block a print on our own bug
        log.exception("arbiter failed")
        out["error"] = str(exc)
    return out
