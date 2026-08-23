"""Corner ordering and frame orientation.

The bowtie case is the one that matters: four corners clicked in a cyclic
order other than TL, TR, BR, BL make getPerspectiveTransform build a quad that
crosses itself, and warpPerspective renders it as two black triangles meeting
on a diagonal. It looks like a broken camera rather than a click-order mistake.
"""
import itertools
import os
import sys

import cv2
import numpy as np

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from app import camera, geometry  # noqa: E402

PPM = 2.0
BED = 200
GEOM = {"px_per_mm": PPM, "bed_size_mm": [BED, BED],
        "edge_margin_mm": 0, "exclude_zones_mm": []}

# A believable perspective view of a bed: not a rectangle, wider at the front.
TL, TR, BR, BL = (120, 90), (520, 96), (600, 430), (40, 420)
CANONICAL = [TL, TR, BR, BL]


def scene():
    """A frame whose bed area is filled with a gradient, black outside it."""
    img = np.zeros((520, 700, 3), np.uint8)
    cv2.fillPoly(img, [np.int32(CANONICAL)], (255, 255, 255))
    ys, xs = np.mgrid[0:520, 0:700]
    tint = np.dstack([(xs % 251).astype(np.uint8),
                      (ys % 251).astype(np.uint8),
                      np.full_like(xs, 180, np.uint8)])
    return np.where(img > 0, tint, 0).astype(np.uint8)


def black_fraction(img):
    return float(np.mean(np.all(img < 8, axis=2)))


def check(cond, label):
    print(f"  [{'ok ' if cond else 'FAIL'}] {label}")
    return 0 if cond else 1


def main():
    failures = 0
    img = scene()

    good = geometry.warp(img, {**GEOM, "corners_px": CANONICAL})
    base_black = black_fraction(good)
    failures += check(base_black < 0.02,
                      f"canonical order warps cleanly ({base_black:.1%} black)")

    # Every one of the 24 click orders must now land on the same warp. Before
    # order_corners, 16 of them produced a folded-over bowtie.
    worst, worst_order = 0.0, None
    mismatched = 0
    for order in itertools.permutations(CANONICAL):
        out = geometry.warp(img, {**GEOM, "corners_px": list(order)})
        frac = black_fraction(out)
        if frac > worst:
            worst, worst_order = frac, order
        # Rotations/reflections of the quad legitimately rotate the warp, so
        # compare the amount of bed captured rather than pixel equality.
        if abs(frac - base_black) > 0.02:
            mismatched += 1
    failures += check(mismatched == 0,
                      f"all 24 click orders warp without folding "
                      f"(worst {worst:.1%} black at {worst_order})")

    # The pre-fix failure mode, asserted directly: feeding a self-intersecting
    # quad straight to OpenCV must produce a lot of black.
    bowtie = np.float32([TL, BR, TR, BL])
    dst = np.float32([[0, 0], [400, 0], [400, 400], [0, 400]])
    folded = cv2.warpPerspective(img, cv2.getPerspectiveTransform(bowtie, dst),
                                 (400, 400))
    failures += check(black_fraction(folded) > 0.20,
                      f"an unsorted bowtie really does fold "
                      f"({black_fraction(folded):.1%} black) - the bug being fixed")

    ordered = geometry.order_corners([BR, TL, BL, TR])
    failures += check(np.allclose(ordered, np.float32(CANONICAL)),
                      "order_corners recovers TL, TR, BR, BL")

    for bad, label in [([(0, 0)] * 4, "four identical points"),
                       ([(0, 0), (10, 10), (20, 20), (30, 30)], "collinear points")]:
        try:
            geometry.order_corners(bad)
            failures += check(False, f"{label} rejected")
        except geometry.GeometryError:
            failures += check(True, f"{label} rejected")

    try:
        geometry.order_corners([(0, 0), (10, 0), (10, 10)])
        failures += check(False, "three corners rejected")
    except geometry.GeometryError:
        failures += check(True, "three corners rejected")

    # ------------------------------------------------------------ orientation
    probe = np.zeros((40, 60, 3), np.uint8)
    probe[0:5, 0:5] = (0, 0, 255)          # red marker in the top-left

    r90 = camera.orient(probe, 90)
    failures += check(r90.shape[:2] == (60, 40), "90 deg swaps the axes")
    failures += check(tuple(r90[0, -1]) == (0, 0, 255),
                      "90 deg CW moves top-left to top-right")

    r360 = camera.orient(camera.orient(probe, 180), 180)
    failures += check(np.array_equal(r360, probe), "180 twice is identity")

    failures += check(np.array_equal(camera.orient(probe, 0), probe),
                      "rotate 0 leaves the frame alone")
    failures += check(tuple(camera.orient(probe, 0, "h")[0, -1]) == (0, 0, 255),
                      "horizontal flip mirrors left to right")

    for bad in (45, "90deg"):
        try:
            camera.orient(probe, bad)
            failures += check(False, f"rotate={bad!r} rejected")
        except (camera.CameraError, ValueError):
            failures += check(True, f"rotate={bad!r} rejected")

    try:
        camera.orient(probe, 0, "diagonal")
        failures += check(False, "flip='diagonal' rejected")
    except camera.CameraError:
        failures += check(True, "flip='diagonal' rejected")

    print("\nFAILURES:", failures)
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
