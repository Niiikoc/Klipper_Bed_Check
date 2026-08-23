"""Config loading/saving. Plain dicts with defaults merged in — keeps the
web UI free to PATCH arbitrary sub-keys without a schema migration dance."""
from __future__ import annotations

import copy
import os
import threading
from typing import Any

import yaml

CONFIG_PATH = os.environ.get("BEDCHECK_CONFIG", "/config/config.yaml")
DATA_DIR = os.environ.get("BEDCHECK_DATA", "/data")

PRINTER_DEFAULTS: dict[str, Any] = {
    "enabled": True,
    "snapshot_url": "",
    "moonraker_url": "",
    "moonraker_api_key": None,
    "macro": "BED_STATE",
    "geometry": {
        "bed_size_mm": [235, 235],
        "corners_px": None,
        "px_per_mm": 2.0,
        "edge_margin_mm": 8.0,
        "exclude_zones_mm": [],
    },
    "capture": {"frames": 5, "frame_delay_s": 0.15, "timeout_s": 5.0},
    "detect": {
        "z_threshold": 6.0,
        "min_area_mm2": 100.0,
        "int_std_floor": 2.0,
        "edge_std_floor": 3.0,
        "chroma_std_floor": 1.5,
        "noise_gain": 2.0,
        "confirm_delay_s": 1.5,
    },
    "pose_gate": {"require_homed": False, "park_xy": None, "tolerance_mm": 5.0},
    "watch": {
        "interval_s": 5.0,
        "heartbeat_s": 120.0,
        "adapt": True,
        "adapt_decay": 0.98,
        "history": 40,
    },
}

ARBITER_DEFAULTS: dict[str, Any] = {
    "enabled": True,
    "model": "openai/clip-vit-base-patch32",
    # Overrule only when CLIP is confident the region is an empty bed.
    "confirm_threshold": 0.25,
    # A blob larger than this is never overruled — it cannot be a shadow.
    "max_override_area_mm2": 2000.0,
    "crop_context_mm": 70.0,
    "prompts": {
        "occupied": ["a 3d printed plastic object sitting on a 3d printer bed"],
        "clear": ["an empty 3d printer bed"],
    },
}

_lock = threading.Lock()


def _merge(base: dict, override: dict) -> dict:
    out = copy.deepcopy(base)
    for k, v in (override or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _merge(out[k], v)
        else:
            out[k] = v
    return out


def load() -> dict:
    with _lock:
        try:
            with open(CONFIG_PATH, "r", encoding="utf-8") as fh:
                raw = yaml.safe_load(fh) or {}
        except FileNotFoundError:
            raw = {}
    cfg = {
        # The data directory comes from BEDCHECK_DATA (set by the image);
        # reported here so /api/config shows what is actually in use.
        "server": {**(raw.get("server") or {}), "data_dir": DATA_DIR},
        "arbiter": _merge(ARBITER_DEFAULTS, raw.get("arbiter") or {}),
        "printers": [_merge(PRINTER_DEFAULTS, p) for p in (raw.get("printers") or [])],
    }
    return cfg


def save(cfg: dict) -> None:
    with _lock:
        tmp = CONFIG_PATH + ".tmp"
        os.makedirs(os.path.dirname(CONFIG_PATH), exist_ok=True)
        with open(tmp, "w", encoding="utf-8") as fh:
            yaml.safe_dump(cfg, fh, sort_keys=False, allow_unicode=True)
        os.replace(tmp, CONFIG_PATH)


def printer(cfg: dict, name: str) -> dict:
    for p in cfg["printers"]:
        if p.get("name") == name:
            return p
    raise KeyError(name)


def update_printer(name: str, patch: dict) -> dict:
    cfg = load()
    for i, p in enumerate(cfg["printers"]):
        if p.get("name") == name:
            cfg["printers"][i] = _merge(p, patch)
            save(cfg)
            return cfg["printers"][i]
    raise KeyError(name)


def data_path(name: str, *parts: str) -> str:
    p = os.path.join(DATA_DIR, name, *parts)
    os.makedirs(os.path.dirname(p), exist_ok=True)
    return p
