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


def grab(url: str, frames: int = 5, delay: float = 0.15,
         timeout: float = 5.0) -> np.ndarray:
    """Return a median-combined BGR frame."""
    if frames <= 1:
        return _one(url, timeout)
    stack = []
    for i in range(frames):
        stack.append(_one(url, timeout))
        if i < frames - 1:
            time.sleep(delay)
    shape = stack[0].shape
    if any(f.shape != shape for f in stack):
        raise CameraError("snapshot resolution changed mid-capture")
    return np.median(np.stack(stack), axis=0).astype(np.uint8)


def encode_jpg(img: np.ndarray, quality: int = 85) -> bytes:
    ok, buf = cv2.imencode(".jpg", img, [cv2.IMWRITE_JPEG_QUALITY, quality])
    if not ok:
        raise CameraError("jpeg encode failed")
    return buf.tobytes()
