"""Minimal Moonraker client: read machine pose, push state back into Klipper.

Publishing happens only while the printer is idle, which is what keeps this
clear of Klipper's gcode mutex — a blocking round-trip from inside a running
macro would deadlock until the shell-command timeout."""
from __future__ import annotations

import logging

import requests

log = logging.getLogger("bedcheck.moonraker")

IDLE_STATES = {"standby", "complete", "cancelled", "error"}


class MoonrakerError(RuntimeError):
    pass


class Moonraker:
    def __init__(self, base_url: str, api_key: str | None = None,
                 timeout: float = 5.0):
        self.base = (base_url or "").rstrip("/")
        self.timeout = timeout
        self.headers = {"X-Api-Key": api_key} if api_key else {}

    def _get(self, path: str, **params):
        try:
            r = requests.get(self.base + path, params=params,
                             headers=self.headers, timeout=self.timeout)
            r.raise_for_status()
            return r.json().get("result", {})
        except requests.RequestException as exc:
            raise MoonrakerError(str(exc)) from exc

    def _post(self, path: str, payload: dict):
        try:
            r = requests.post(self.base + path, json=payload,
                              headers=self.headers, timeout=self.timeout)
            r.raise_for_status()
            return r.json().get("result", {})
        except requests.RequestException as exc:
            raise MoonrakerError(str(exc)) from exc

    def pose(self, macro: str | None = None) -> dict:
        """Machine state needed to decide whether the view is trustworthy.

        Also reads back the macro variables we publish, so a Klipper restart
        (which resets them to 0) is noticed and corrected on the next tick
        rather than at the next heartbeat."""
        query = {"print_stats": "state", "toolhead": "position,homed_axes"}
        key = f"gcode_macro {macro}" if macro else None
        if key:
            query[key] = "valid,occupied,area"

        res = self._get("/printer/objects/query", **query)
        status = res.get("status", {})
        out = {
            "state": status.get("print_stats", {}).get("state", "unknown"),
            "position": status.get("toolhead", {}).get("position", []),
            "homed_axes": status.get("toolhead", {}).get("homed_axes", ""),
            "macro_state": None,
        }
        if key and isinstance(status.get(key), dict):
            m = status[key]
            try:
                out["macro_state"] = {
                    "valid": int(m.get("valid", 0)),
                    "occupied": int(m.get("occupied", 0)),
                    "area": float(m.get("area", 0.0)),
                }
            except (TypeError, ValueError):
                out["macro_state"] = None
        return out

    def publish(self, macro: str, valid: bool, occupied: bool,
                area_mm2: float) -> None:
        script = "\n".join([
            f"SET_GCODE_VARIABLE MACRO={macro} VARIABLE=valid VALUE={int(valid)}",
            f"SET_GCODE_VARIABLE MACRO={macro} VARIABLE=occupied VALUE={int(occupied)}",
            f"SET_GCODE_VARIABLE MACRO={macro} VARIABLE=area VALUE={area_mm2:.1f}",
        ])
        self._post("/printer/gcode/script", {"script": script})
