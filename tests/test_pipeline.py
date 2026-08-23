"""Synthetic end-to-end exercise of the bed-check detection pipeline.

Builds a fake textured bed, learns a reference from it, then checks that the
detector fires on real objects and stays quiet for the things that normally
cause false alarms.
"""
import os
import sys

import cv2
import numpy as np

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from app import detect  # noqa: E402
from app.model import BackgroundModel  # noqa: E402

PPM = 2.0
BED = 350
SIZE = int(BED * PPM)
GEOM = {"px_per_mm": PPM, "bed_size_mm": [BED, BED],
        "edge_margin_mm": 8, "exclude_zones_mm": []}
PARAMS = {"z_threshold": 6.0, "min_area_mm2": 100.0,
          "int_std_floor": 2.0, "edge_std_floor": 3.0,
          "chroma_std_floor": 1.5, "noise_gain": 2.0, "confirm_delay_s": 0}

rng = np.random.default_rng(7)


def base_bed():
    """A PEI-ish textured plate with a couple of permanent smudges."""
    tex = rng.normal(118, 9, (SIZE, SIZE)).astype(np.float32)
    tex = cv2.GaussianBlur(tex, (0, 0), 1.4)
    # a vignette, like real camera + lighting
    yy, xx = np.mgrid[0:SIZE, 0:SIZE].astype(np.float32)
    r = np.hypot(xx - SIZE / 2, yy - SIZE / 2) / (SIZE / 2)
    tex *= (1.0 - 0.28 * r ** 2)
    # permanent marks that must never be reported
    cv2.circle(tex, (int(90 * PPM), int(120 * PPM)), int(9 * PPM), 100, -1)
    cv2.line(tex, (int(40 * PPM), int(300 * PPM)),
             (int(300 * PPM), int(295 * PPM)), 104, 2)
    return cv2.cvtColor(np.clip(tex, 0, 255).astype(np.uint8), cv2.COLOR_GRAY2BGR)


BASE = base_bed()


def capture(gain=1.0, noise=2.0, shift=(0, 0), wb=(1.0, 1.0, 1.0)):
    img = BASE.astype(np.float32) * gain
    img *= np.array(wb, np.float32)
    img += rng.normal(0, noise, img.shape)
    img = np.clip(img, 0, 255).astype(np.uint8)
    if shift != (0, 0):
        m = np.float32([[1, 0, shift[0]], [0, 1, shift[1]]])
        img = cv2.warpAffine(img, m, (SIZE, SIZE), borderMode=cv2.BORDER_REPLICATE)
    return img


def with_object(size_mm, colour=(60, 70, 190), at=(180, 170)):
    img = capture()
    half = int(size_mm * PPM / 2)
    cx, cy = int(at[0] * PPM), int(at[1] * PPM)
    cv2.rectangle(img, (cx - half, cy - half), (cx + half, cy + half), colour, -1)
    cv2.rectangle(img, (cx - half, cy - half), (cx + half, cy + half),
                  (30, 30, 90), 2)
    return img


def build_model(n=12):
    model = None
    for _ in range(n):
        feats = detect.features(capture())
        if model is None:
            model = BackgroundModel(feats["int"].shape, detect.CHANNELS)
        else:
            feats, _ = detect.align(model.anchor, feats)
        model.add(feats)
    return model


def run(img, model, params=None):
    return detect.detect(img, model, GEOM, params or PARAMS)


def main():
    print("building reference from 12 clear captures ...")
    model = build_model()
    print(f"  samples={model.samples:.0f} shape={model.shape} "
          f"channels={model.channels}")

    cases = [
        ("clear bed",                      capture(),                       False),
        ("clear, sensor noise x3",         capture(noise=6.0),              False),
        ("clear, LED dimmed 25%",          capture(gain=0.75),              False),
        ("clear, LED brightened 30%",      capture(gain=1.30),              False),
        ("clear, camera nudged 3px",       capture(shift=(3, -2)),          False),
        ("clear, warm white balance",      capture(wb=(0.88, 1.0, 1.12)),   False),
        ("clear, cool white balance",      capture(wb=(1.14, 1.0, 0.9)),    False),
        ("5mm speck (debris, must fire)",  with_object(5),                  True),
        ("PART 15mm",                      with_object(15),                 True),
        ("PART 30mm",                      with_object(30),                 True),
        ("PART 60mm dark grey",            with_object(60, (55, 55, 55)),   True),
        ("PART 25mm, bed-coloured",        with_object(25, (118, 118, 118)), True),
        ("PART 30mm ISOLUMINANT blue",     with_object(30, (200, 80, 70)),  True),
        ("PART 30mm ISOLUMINANT red",      with_object(30, (70, 80, 200)),  True),
        ("PART 40mm + LED dimmed",         None,                            True),
    ]

    failures = 0
    for label, img, expect in cases:
        if img is None:  # combined case
            img = with_object(40)
            img = np.clip(img.astype(np.float32) * 0.75, 0, 255).astype(np.uint8)
        r = run(img, model)
        ok = r.occupied == expect
        failures += not ok
        print(f"  [{'ok ' if ok else 'FAIL'}] {label:<32} "
              f"occupied={str(r.occupied):<5} "
              f"area={r.largest_area_mm2:7.1f}mm2 max_z={r.max_z:6.2f} "
              f"aligned={r.aligned}")

    # min_area_mm2 must actually work as a knob.
    strict = dict(PARAMS, min_area_mm2=400.0)
    r = run(with_object(5), model, strict)
    ok = not r.occupied
    failures += not ok
    print(f"  [{'ok ' if ok else 'FAIL'}] {'5mm speck, min_area=400':<32} "
          f"occupied={str(r.occupied):<5} area={r.largest_area_mm2:7.1f}mm2")
    r = run(with_object(30), model, strict)
    ok = r.occupied
    failures += not ok
    print(f"  [{'ok ' if ok else 'FAIL'}] {'30mm part, min_area=400':<32} "
          f"occupied={str(r.occupied):<5} area={r.largest_area_mm2:7.1f}mm2")

    # A leftover skirt ring: thin, but must not be erased.
    img = capture()
    cv2.circle(img, (int(175 * PPM), int(175 * PPM)), int(45 * PPM),
               (70, 80, 200), max(1, int(1.2 * PPM)))
    r = run(img, model)
    ok = r.occupied
    failures += not ok
    print(f"  [{'ok ' if ok else 'FAIL'}] {'leftover skirt ring (1.2mm)':<32} "
          f"occupied={str(r.occupied):<5} area={r.largest_area_mm2:7.1f}mm2")

    # An object inside an excluded zone must be ignored.
    geom_ex = dict(GEOM, exclude_zones_mm=[[150, 140, 210, 200]])
    r = detect.detect(with_object(30), model, geom_ex, PARAMS)
    ok = not r.occupied
    failures += not ok
    print(f"  [{'ok ' if ok else 'FAIL'}] {'PART inside exclude zone':<32} "
          f"occupied={str(r.occupied):<5} area={r.largest_area_mm2:7.1f}mm2")

    # Debug rendering must not blow up.
    out = detect.annotate(run(with_object(30), model), "occupied", "arbiter p=0.91")
    cv2.imwrite(os.path.join(os.path.dirname(__file__), "debug_render.jpg"), out)
    print(f"  [ok ] debug render written ({out.shape[1]}x{out.shape[0]})")

    # Model round-trip.
    p = os.path.join(os.path.dirname(__file__), "m.npz")
    model.save(p)
    again = BackgroundModel.load(p)
    same = np.allclose(again.stats("a")[0], model.stats("a")[0])
    failures += not same
    print(f"  [{'ok ' if same else 'FAIL'}] model save/load round-trip")

    print("\nFAILURES:", failures)
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
