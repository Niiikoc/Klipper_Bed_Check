"""Detection pipeline: features -> alignment -> z-score -> blobs.

Design notes that matter for false alarms:
  * Intensity is normalised by a heavily blurred copy of itself, which removes
    low-frequency illumination (LED brightness, daylight gradients, shadows)
    before anything is compared.
  * A Scharr gradient channel runs alongside it, so an object whose colour
    happens to match the bed is still caught by its outline.
  * Two chroma channels (Lab a/b, each with its own large-scale blur removed)
    catch the very common case of coloured filament that happens to match the
    plate's *luminance* — invisible in grayscale, obvious in colour. Removing
    the low-frequency component is what makes them survive white-balance drift.
  * ECC alignment absorbs small camera shifts instead of reporting them as a
    bed-wide change.
  * Deviation is measured in per-pixel sigma, not absolute difference, so noisy
    regions self-tolerate. The sigma floor additionally rises with whatever
    noise the current frame is carrying.
  * Only contiguous blobs above a physical area survive.

Reported blob area is the *detection footprint*: the object plus a halo of
roughly the smoothing scale (~3mm at 2 px/mm), so a 5mm part measures around
100mm2. This is deliberate — min_area_mm2 exists to reject noise, not to wave
through real debris, and eroding the halo away would delete genuinely thin
objects such as a leftover skirt ring. Use `tune` to set the threshold from the
measured noise floor rather than from an object size.
"""
from __future__ import annotations

from dataclasses import dataclass, field

import cv2
import numpy as np

from . import geometry

CHANNELS = ("int", "edge", "a", "b")

# Which config key supplies each channel's sigma floor.
FLOOR_KEYS = {
    "int": "int_std_floor",
    "edge": "edge_std_floor",
    "a": "chroma_std_floor",
    "b": "chroma_std_floor",
}
FLOOR_FALLBACK = {"int": 2.0, "edge": 3.0, "a": 1.5, "b": 1.5}


@dataclass
class Blob:
    area_mm2: float
    bbox_mm: tuple[float, float, float, float]
    bbox_px: tuple[int, int, int, int]
    centroid_mm: tuple[float, float]
    peak_z: float
    channel: str


@dataclass
class DetectResult:
    occupied: bool
    blobs: list[Blob] = field(default_factory=list)
    max_z: float = 0.0
    largest_area_mm2: float = 0.0
    aligned: bool = True
    warped: np.ndarray | None = None
    zmap: np.ndarray | None = None
    binary: np.ndarray | None = None


# ---------------------------------------------------------------- features
def features(bgr: np.ndarray, illum_sigma: float = 41.0) -> dict[str, np.ndarray]:
    gray = cv2.cvtColor(bgr, cv2.COLOR_BGR2GRAY).astype(np.float32)
    gray = cv2.GaussianBlur(gray, (0, 0), 1.2)
    base = cv2.GaussianBlur(gray, (0, 0), illum_sigma)
    norm = np.clip(128.0 * gray / np.maximum(base, 1.0), 0.0, 255.0)

    gx = cv2.Scharr(norm, cv2.CV_32F, 1, 0)
    gy = cv2.Scharr(norm, cv2.CV_32F, 0, 1)
    edge = cv2.GaussianBlur(cv2.magnitude(gx, gy), (0, 0), 1.5)

    out = {"int": norm.astype(np.float32), "edge": edge.astype(np.float32)}

    lab = cv2.cvtColor(bgr, cv2.COLOR_BGR2Lab).astype(np.float32)
    for idx, name in ((1, "a"), (2, "b")):
        ch = cv2.GaussianBlur(lab[:, :, idx], (0, 0), 1.5)
        # Subtracting the large-scale component removes a global colour cast,
        # which is what a white-balance change looks like.
        out[name] = (ch - cv2.GaussianBlur(ch, (0, 0), illum_sigma) + 128.0
                     ).astype(np.float32)
    return out


def noise_sigma(residual: np.ndarray) -> float:
    """Robust estimate of the high-frequency mismatch between a frame and the
    model. Measured on the *difference*, so stable bed texture cancels out and
    only genuine per-frame noise is left. The median keeps a real object from
    moving the estimate."""
    hf = residual - cv2.GaussianBlur(residual, (0, 0), 2.0)
    return float(1.4826 * np.median(np.abs(hf)))


def align(anchor: np.ndarray | None, feats: dict[str, np.ndarray]
          ) -> tuple[dict[str, np.ndarray], bool]:
    """Euclidean-align the current frame onto the reference anchor."""
    ref = feats["int"]
    if anchor is None or anchor.shape != ref.shape:
        return feats, True

    h, w = ref.shape
    scale = min(1.0, 400.0 / max(h, w))

    def small(im):
        return cv2.resize(im, None, fx=scale, fy=scale,
                          interpolation=cv2.INTER_AREA)

    warp = np.eye(2, 3, dtype=np.float32)
    try:
        criteria = (cv2.TERM_CRITERIA_EPS | cv2.TERM_CRITERIA_COUNT, 60, 1e-5)
        _, warp = cv2.findTransformECC(
            small(anchor), small(ref), warp, cv2.MOTION_EUCLIDEAN,
            criteria, None, 5,
        )
    except cv2.error:
        return feats, False

    warp[0, 2] /= scale
    warp[1, 2] /= scale
    if float(np.hypot(warp[0, 2], warp[1, 2])) > 0.15 * max(h, w):
        # An implausible solution means ECC latched onto something wrong.
        return feats, False

    remapped = {
        k: cv2.warpAffine(v, warp, (w, h),
                          flags=cv2.INTER_LINEAR | cv2.WARP_INVERSE_MAP,
                          borderMode=cv2.BORDER_REPLICATE)
        for k, v in feats.items()
    }
    return remapped, True


# --------------------------------------------------------------- detection
def _floor(params: dict, channel: str) -> float:
    return float(params.get(FLOOR_KEYS[channel], FLOOR_FALLBACK[channel]))


def detect(warped: np.ndarray, model, geom: dict, params: dict,
           mask: np.ndarray | None = None) -> DetectResult:
    if mask is None:
        mask = geometry.mask(geom)

    feats, aligned = align(getattr(model, "anchor", None), features(warped))

    gain = float(params.get("noise_gain", 2.0))
    zmap = None
    winner = np.zeros(feats["int"].shape, np.uint8)  # which channel fired
    for idx, channel in enumerate(model.channels):
        mean, std = model.stats(channel)
        diff = feats[channel] - mean
        floor = max(_floor(params, channel), gain * noise_sigma(diff))
        z = np.abs(diff) / np.maximum(std, floor)
        if zmap is None:
            zmap = z
            winner[:] = idx
        else:
            better = z > zmap
            zmap = np.where(better, z, zmap)
            winner[better] = idx
    zmap[mask == 0] = 0.0

    # `core` is what actually exceeded threshold and is what areas are measured
    # on. `grouped` only joins fragments of one object into one component, so
    # closing never inflates the reported size. The opening kernel is kept at
    # 3x3: anything larger erases genuinely thin objects like a skirt ring.
    core = ((zmap > float(params["z_threshold"])) * 255).astype(np.uint8)
    core = cv2.morphologyEx(
        core, cv2.MORPH_OPEN,
        cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (3, 3)))
    core = cv2.bitwise_and(core, mask)
    grouped = cv2.morphologyEx(
        core, cv2.MORPH_CLOSE,
        cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (11, 11)))
    grouped = cv2.bitwise_and(grouped, mask)

    ppm = float(geom["px_per_mm"])
    ppm2 = ppm * ppm
    min_area = float(params["min_area_mm2"])

    count, labels, stats, centroids = cv2.connectedComponentsWithStats(grouped, 8)
    core_bool = core > 0
    blobs: list[Blob] = []
    for i in range(1, count):
        member = labels == i
        pixels = member & core_bool
        area_mm2 = float(np.count_nonzero(pixels)) / ppm2
        if area_mm2 < min_area:
            continue
        x, y = int(stats[i, cv2.CC_STAT_LEFT]), int(stats[i, cv2.CC_STAT_TOP])
        w, h = int(stats[i, cv2.CC_STAT_WIDTH]), int(stats[i, cv2.CC_STAT_HEIGHT])
        fired = winner[pixels]
        channel = (model.channels[int(np.bincount(fired).argmax())]
                   if fired.size else "?")
        blobs.append(Blob(
            area_mm2=area_mm2,
            bbox_mm=(x / ppm, y / ppm, w / ppm, h / ppm),
            bbox_px=(x, y, w, h),
            centroid_mm=(float(centroids[i][0]) / ppm,
                         float(centroids[i][1]) / ppm),
            peak_z=float(zmap[member].max()),
            channel=channel,
        ))

    blobs.sort(key=lambda b: b.area_mm2, reverse=True)
    return DetectResult(
        occupied=bool(blobs),
        blobs=blobs,
        max_z=float(zmap.max()),
        largest_area_mm2=blobs[0].area_mm2 if blobs else 0.0,
        aligned=aligned,
        warped=warped,
        zmap=zmap,
        binary=core,
    )


# ------------------------------------------------------------------ debug
def annotate(result: DetectResult, verdict: str, note: str = "") -> np.ndarray:
    img = result.warped.copy()
    colour = {"occupied": (0, 0, 220),
              "clear": (0, 170, 0)}.get(verdict, (0, 165, 235))

    for b in result.blobs:
        x, y, w, h = b.bbox_px
        cv2.rectangle(img, (x, y), (x + w, y + h), colour, 2)
        cv2.putText(img,
                    f"{b.area_mm2:.0f}mm2 {b.bbox_mm[2]:.0f}x{b.bbox_mm[3]:.0f}mm "
                    f"z{b.peak_z:.1f} [{b.channel}]",
                    (x, max(12, y - 5)), cv2.FONT_HERSHEY_SIMPLEX, 0.45,
                    colour, 1, cv2.LINE_AA)

    bar = 26
    cv2.rectangle(img, (0, 0), (img.shape[1], bar), colour, -1)
    label = f"{verdict.upper()}  max_z={result.max_z:.1f}"
    if result.largest_area_mm2:
        label += f"  largest={result.largest_area_mm2:.0f}mm2"
    if note:
        label += f"  {note}"
    if not result.aligned:
        label += "  [align failed]"
    cv2.putText(img, label, (6, 18), cv2.FONT_HERSHEY_SIMPLEX, 0.5,
                (255, 255, 255), 1, cv2.LINE_AA)
    return img


def heatmap(result: DetectResult, z_threshold: float) -> np.ndarray:
    scaled = np.clip(result.zmap / max(z_threshold, 1e-6) * 128.0, 0, 255)
    return cv2.applyColorMap(scaled.astype(np.uint8), cv2.COLORMAP_TURBO)
