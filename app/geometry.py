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


def order_corners(pts) -> np.ndarray:
    """Sort four clicked points into top-left, top-right, bottom-right,
    bottom-left order.

    getPerspectiveTransform maps src[i] onto dst[i] by position, so it trusts
    the click order completely. Click the corners in any other cyclic order -
    or click them on a camera whose image is rotated - and the quad crosses
    itself. warpPerspective then folds the image over and renders the classic
    bowtie: two black triangles meeting along a diagonal, which is what a
    miscalibrated bed looks like on screen.

    Sorting by angle around the centroid makes the click order irrelevant. The
    image y axis points down, so ascending atan2 walks the quad clockwise;
    rolling the sequence to start at the corner nearest the image origin lands
    on top-left.
    """
    p = np.asarray(pts, dtype=np.float32).reshape(-1, 2)
    if p.shape[0] != 4:
        raise GeometryError(f"need exactly 4 corners, got {p.shape[0]}")

    centre = p.mean(axis=0)
    angles = np.arctan2(p[:, 1] - centre[1], p[:, 0] - centre[0])
    p = p[np.argsort(angles)]
    p = np.roll(p, -int(np.argmin(p.sum(axis=1))), axis=0)

    # Shoelace: catches three corners clicked on top of each other, or all
    # four in a line, before they turn into an unhelpful OpenCV error.
    area = 0.5 * abs(np.dot(p[:, 0], np.roll(p[:, 1], -1))
                     - np.dot(p[:, 1], np.roll(p[:, 0], -1)))
    if area < 100.0:
        raise GeometryError(
            f"the four corners enclose almost no area ({area:.0f}px2) - "
            "click the actual bed corners, spread apart")
    return p


def homography(geom: dict) -> np.ndarray:
    corners = geom.get("corners_px")
    if not corners or len(corners) != 4:
        raise GeometryError("bed corners are not calibrated yet")
    src = order_corners(corners)
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
