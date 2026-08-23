"""Perspective rectification to a top-down bed view.

Working in bed millimetres rather than image pixels is what lets thresholds be
physical ("ignore anything under 10x10mm") instead of camera-dependent."""
from __future__ import annotations

import cv2
import numpy as np


class GeometryError(RuntimeError):
    pass


def warp_size(geom: dict) -> tuple[int, int]:
    ppm = float(geom["px_per_mm"])
    w = int(round(float(geom["bed_size_mm"][0]) * ppm))
    h = int(round(float(geom["bed_size_mm"][1]) * ppm))
    return w, h


def homography(geom: dict) -> np.ndarray:
    corners = geom.get("corners_px")
    if not corners or len(corners) != 4:
        raise GeometryError("bed corners are not calibrated yet")
    src = np.float32(corners)
    w, h = warp_size(geom)
    dst = np.float32([[0, 0], [w, 0], [w, h], [0, h]])
    return cv2.getPerspectiveTransform(src, dst)


def warp(img: np.ndarray, geom: dict) -> np.ndarray:
    m = homography(geom)
    w, h = warp_size(geom)
    return cv2.warpPerspective(img, m, (w, h), flags=cv2.INTER_LINEAR)


def mask(geom: dict) -> np.ndarray:
    """255 where detections count, 0 where they are ignored."""
    ppm = float(geom["px_per_mm"])
    w, h = warp_size(geom)
    m = np.full((h, w), 255, np.uint8)

    margin = int(round(float(geom.get("edge_margin_mm", 0)) * ppm))
    if margin > 0:
        m[:margin, :] = 0
        m[-margin:, :] = 0
        m[:, :margin] = 0
        m[:, -margin:] = 0

    for zone in geom.get("exclude_zones_mm") or []:
        x0, y0, x1, y1 = (int(round(v * ppm)) for v in zone)
        cv2.rectangle(m, (x0, y0), (x1, y1), 0, -1)
    return m


def draw_grid(img: np.ndarray, ppm: float, step_mm: float = 25.0) -> np.ndarray:
    out = img.copy()
    h, w = out.shape[:2]
    step = max(1, int(round(step_mm * ppm)))
    for x in range(0, w, step):
        cv2.line(out, (x, 0), (x, h), (0, 200, 0), 1)
        cv2.putText(out, str(int(x / ppm)), (x + 2, 14),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.35, (0, 200, 0), 1, cv2.LINE_AA)
    for y in range(0, h, step):
        cv2.line(out, (0, y), (w, y), (0, 200, 0), 1)
        cv2.putText(out, str(int(y / ppm)), (2, y - 3),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.35, (0, 200, 0), 1, cv2.LINE_AA)
    return out
