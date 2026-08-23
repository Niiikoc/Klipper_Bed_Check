"""Snapshot capture. A median stack of N frames kills sensor noise and JPEG
artefacts, which is the single cheapest false-alarm reduction available."""
from __future__ import annotations

import time

import cv2
import numpy as np
import requests


class CameraError(RuntimeError):
    pass


def _one(url: str, timeout: float) -> np.ndarray:
    try:
        resp = requests.get(url, timeout=timeout)
        resp.raise_for_status()
    except requests.RequestException as exc:
        raise CameraError(f"snapshot fetch failed: {exc}") from exc
    buf = np.frombuffer(resp.content, np.uint8)
    img = cv2.imdecode(buf, cv2.IMREAD_COLOR)
    if img is None:
        raise CameraError("snapshot could not be decoded as an image")
    return img


_ROTATIONS = {
    90: cv2.ROTATE_90_CLOCKWISE,
    180: cv2.ROTATE_180,
    270: cv2.ROTATE_90_COUNTERCLOCKWISE,
}


def orient(img: np.ndarray, rotate: int = 0, flip: str = "") -> np.ndarray:
    """Rotate/flip a frame into the orientation the bed is calibrated in.

    Applied to every capture path, so the corners clicked in the web UI, the
    reference model and the live checks all share one coordinate system.
    """
    deg = int(rotate) % 360
    if deg not in (0, 90, 180, 270):
        raise CameraError(f"rotate must be 0, 90, 180 or 270, not {rotate!r}")
    if deg:
        img = cv2.rotate(img, _ROTATIONS[deg])

    mode = (flip or "").lower()
    if mode in ("h", "horizontal"):
        img = cv2.flip(img, 1)
    elif mode in ("v", "vertical"):
        img = cv2.flip(img, 0)
    elif mode not in ("", "none"):
        raise CameraError(f"flip must be '', 'h' or 'v', not {flip!r}")
    return img


def grab(url: str, frames: int = 5, delay: float = 0.15,
         timeout: float = 5.0, rotate: int = 0, flip: str = "") -> np.ndarray:
    """Return a median-combined BGR frame in the configured orientation."""
    if frames <= 1:
        return orient(_one(url, timeout), rotate, flip)
    stack = []
    for i in range(frames):
        stack.append(_one(url, timeout))
        if i < frames - 1:
            time.sleep(delay)
    shape = stack[0].shape
    if any(f.shape != shape for f in stack):
        raise CameraError("snapshot resolution changed mid-capture")
    # Rotating once after the stack, not per frame: a rotation only permutes
    # pixels, so it commutes with the per-pixel median.
    med = np.median(np.stack(stack), axis=0).astype(np.uint8)
    return orient(med, rotate, flip)


def encode_jpg(img: np.ndarray, quality: int = 85) -> bytes:
    ok, buf = cv2.imencode(".jpg", img, [cv2.IMWRITE_JPEG_QUALITY, quality])
    if not ok:
        raise CameraError("jpeg encode failed")
    return buf.tobytes()
