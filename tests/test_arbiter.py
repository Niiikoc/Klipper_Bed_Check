"""Exercise the CLIP arbiter: plumbing, crop bounds, and the overrule rule."""
import os, sys
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import numpy as np
from test_pipeline import capture, with_object, PPM
from app import arbiter, detect
from app.model import BackgroundModel

CFG = {
    "enabled": True,
    "model": "openai/clip-vit-base-patch32",
    "confirm_threshold": 0.60,
    "crop_context_mm": 70.0,
    "prompts": {
        "occupied": ["a 3d printed plastic object sitting on a 3d printer bed",
                     "a finished 3d printed part left on the print surface"],
        "clear": ["an empty 3d printer bed",
                  "a clean empty print surface with nothing on it"],
    },
}
GEOM = {"px_per_mm": PPM, "bed_size_mm": [350, 350],
        "edge_margin_mm": 8, "exclude_zones_mm": []}
PARAMS = {"z_threshold": 6.0, "min_area_mm2": 100.0, "int_std_floor": 2.0,
          "edge_std_floor": 3.0, "chroma_std_floor": 1.5, "noise_gain": 2.0}

res = []
def ck(label, cond, detail=""):
    res.append(cond); print(f"  [{'ok ' if cond else 'FAIL'}] {label}" + (f"  {detail}" if detail else ""))

# crop geometry must stay inside the frame even at the corners
img = np.zeros((700, 700, 3), np.uint8)
for bbox in [(0, 0, 10, 10), (690, 690, 10, 10), (340, 0, 20, 20), (0, 340, 5, 900)]:
    c = arbiter._crop(img, bbox, PPM, 70.0)
    ck(f"crop in-bounds for {bbox}", c.size > 0 and c.shape[0] > 0 and c.shape[1] > 0,
       f"-> {c.shape[1]}x{c.shape[0]}")

# disabled / no blobs must be no-ops
ck("disabled -> no run", arbiter.judge(img, [], PPM, dict(CFG, enabled=False))["ran"] is False)
ck("no blobs -> no run", arbiter.judge(img, [], PPM, CFG)["ran"] is False)

print("  loading CLIP (first run downloads ~600MB) ...")
model = None
for _ in range(8):
    f = detect.features(capture())
    if model is None: model = BackgroundModel(f["int"].shape, detect.CHANNELS)
    else: f, _ = detect.align(model.anchor, f)
    model.add(f)

part = with_object(45, (60, 70, 190))
r = detect.detect(part, model, GEOM, PARAMS)
ck("detector found the part", r.occupied, f"area={r.largest_area_mm2:.0f}mm2")

out = arbiter.judge(r.warped, r.blobs, PPM, CFG)
ck("arbiter ran", out["ran"], f"error={out['error']}")
ck("p_occupied is a probability",
   out["p_occupied"] is not None and 0.0 <= out["p_occupied"] <= 1.0,
   f"p={out['p_occupied']}")
ck("per-blob list matches blob count",
   len(out["per_blob"]) == min(len(r.blobs), 4), f"{out['per_blob']}")

# a bogus "blob" over clean bed should score lower than the real part
class FakeBlob:
    bbox_px = (int(60*PPM), int(60*PPM), int(20*PPM), int(20*PPM))
clean = arbiter.judge(capture(), [FakeBlob()], PPM, CFG)
ck("scores both crops without error", clean["ran"] and out["ran"],
   f"clean={clean['p_occupied']:.3f} part={out['p_occupied']:.3f}")
print("  NOTE: CLIP semantics are meaningless on synthetic noise images;")
print("        these numbers only prove the plumbing. Calibrate")
print("        confirm_threshold against real captures in the web UI.")

print("\nFAILURES:", res.count(False))
raise SystemExit(1 if res.count(False) else 0)
