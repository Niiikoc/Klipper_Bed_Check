"""Per-printer orchestration: capture -> detect -> arbitrate -> publish."""
from __future__ import annotations

import logging
import os
import threading
import time
import uuid
from collections import deque

import cv2
import numpy as np

from . import arbiter, camera, config, detect, geometry
from .model import BackgroundModel, ModelError
from .moonraker import IDLE_STATES, Moonraker, MoonrakerError

# Change-detection thumbnails are built at this resolution: one pixel per 2mm
# of bed. Fine enough to see anything near min_area_mm2, coarse enough that the
# comparison is free.
THUMB_PX_PER_MM = 0.5

log = logging.getLogger("bedcheck.service")


class PrinterService:
    def __init__(self, name: str):
        self.name = name
        self.lock = threading.RLock()
        self.history: deque = deque(maxlen=200)
        self.last: dict = {
            "verdict": "unknown",
            "reason": "not checked yet",
            "at": None,
        }
        self._published: tuple | None = None
        self._published_at = 0.0
        # Thumbnail of the bed as it looked at the last *full* check, plus
        # when that was. Comparing against the last verified state rather than
        # against the previous tick is what stops a slow change - a part eased
        # down over ten seconds, or light drifting - from creeping past the
        # gate one imperceptible step at a time.
        self._thumb = None
        self._thumb_at = 0.0

    # ------------------------------------------------------------ helpers
    @property
    def cfg(self) -> dict:
        return config.printer(config.load(), self.name)

    @property
    def arb_cfg(self) -> dict:
        return config.load()["arbiter"]

    def model_path(self) -> str:
        return config.data_path(self.name, "reference.npz")

    def load_model(self) -> BackgroundModel:
        return BackgroundModel.load(self.model_path())

    def moonraker(self, cfg: dict | None = None) -> Moonraker:
        cfg = cfg or self.cfg
        return Moonraker(cfg["moonraker_url"], cfg.get("moonraker_api_key"))

    def _grab(self, cfg: dict, frames: int, delay: float):
        cap = cfg["capture"]
        return camera.grab(cfg["snapshot_url"], frames, delay, cap["timeout_s"],
                           cap.get("rotate", 0), cap.get("flip", ""))

    def _grab_warped(self, cfg: dict):
        cap = cfg["capture"]
        raw = self._grab(cfg, cap["frames"], cap["frame_delay_s"])
        return raw, geometry.warp(raw, cfg["geometry"])

    # -------------------------------------------------------- calibration
    def snapshot_jpg(self) -> bytes:
        cfg = self.cfg
        return camera.encode_jpg(self._grab(cfg, 1, 0))

    def warp_jpg(self, grid: bool = True) -> bytes:
        cfg = self.cfg
        raw = self._grab(cfg, 1, 0)
        warped = geometry.warp(raw, cfg["geometry"])
        if grid:
            m = geometry.mask(cfg["geometry"])
            warped[m == 0] = (warped[m == 0] * 0.35).astype(warped.dtype)
            warped = geometry.draw_grid(warped, float(cfg["geometry"]["px_per_mm"]))
        return camera.encode_jpg(warped)

    def build_reference(self, captures: int = 12, mode: str = "reset") -> dict:
        """Capture the empty bed.

        Run it again with mode='append' under other lighting conditions to
        widen sigma where the scene legitimately varies."""
        cfg = self.cfg
        with self.lock:
            path = self.model_path()
            model = None
            if mode == "append" and os.path.exists(path):
                model = BackgroundModel.load(path)

            warped = None
            for i in range(captures):
                _, warped = self._grab_warped(cfg)
                feats = detect.features(warped)
                if model is None:
                    model = BackgroundModel(feats["int"].shape, detect.CHANNELS)
                else:
                    feats, _ = detect.align(model.anchor, feats)
                model.add(feats)
                if i < captures - 1:
                    time.sleep(0.25)

            model.save(path)
            cv2.imwrite(config.data_path(self.name, "reference.jpg"), warped)
            return {"samples": model.samples, "mode": mode,
                    "channels": list(model.channels),
                    "shape": list(model.shape)}

    def has_reference(self) -> bool:
        return os.path.exists(self.model_path())

    # ------------------------------------------------------------- checks
    def pose_status(self, cfg: dict) -> tuple[bool, str, dict]:
        gate = cfg["pose_gate"]
        try:
            pose = self.moonraker(cfg).pose(cfg.get("macro"))
        except MoonrakerError as exc:
            return False, f"moonraker unreachable: {exc}", {}

        if pose["state"] not in IDLE_STATES:
            return False, f"printer is {pose['state']}", pose
        if gate.get("require_homed") and "xyz" not in pose.get("homed_axes", ""):
            return False, "axes not homed", pose
        park = gate.get("park_xy")
        if park:
            pos = pose.get("position") or []
            if len(pos) < 2:
                return False, "toolhead position unknown", pose
            tol = float(gate.get("tolerance_mm", 5.0))
            if abs(pos[0] - park[0]) > tol or abs(pos[1] - park[1]) > tol:
                return False, "toolhead not parked", pose
        return True, "ok", pose

    def run_check(self, adapt: bool = False, params: dict | None = None) -> dict:
        """One full check.

        Never raises: an internal failure yields verdict 'unknown', which the
        Klipper gate treats as fail-open."""
        started = time.time()
        cfg = self.cfg
        geom = cfg["geometry"]
        params = params or cfg["detect"]

        result = {
            "printer": self.name, "at": started, "verdict": "unknown",
            "reason": "", "area_mm2": 0.0, "max_z": 0.0, "blobs": 0,
            "arbiter": None, "image": None, "took_s": 0.0,
        }

        try:
            model = self.load_model()
        except ModelError as exc:
            result["reason"] = str(exc)
            return self._finish(result, started)

        try:
            mask = geometry.mask(geom)
            _, warped = self._grab_warped(cfg)
            first = detect.detect(warped, model, geom, params, mask)
        except (camera.CameraError, geometry.GeometryError, ModelError) as exc:
            result["reason"] = str(exc)
            return self._finish(result, started)

        final, note = first, ""

        # Second look: a transient (a hand, a passing shadow) will not repeat.
        if first.occupied and float(params.get("confirm_delay_s", 0)) > 0:
            time.sleep(float(params["confirm_delay_s"]))
            try:
                _, warped2 = self._grab_warped(cfg)
                second = detect.detect(warped2, model, geom, params, mask)
            except camera.CameraError as exc:
                result["reason"] = str(exc)
                return self._finish(result, started)
            if not second.occupied:
                final, note = second, "transient, ignored"
            else:
                final = second

        verdict = "occupied" if final.occupied else "clear"

        # Semantic arbitration only where the detector already flagged.
        if final.occupied:
            arb = arbiter.judge(final.warped, final.blobs,
                                float(geom["px_per_mm"]), self.arb_cfg)
            result["arbiter"] = arb
            if arb["ran"]:
                acfg = self.arb_cfg
                p = float(arb["p_occupied"])
                rail = float(acfg.get("max_override_area_mm2", 2000.0))
                # Turning a true positive into a false negative is the worse
                # error: you would drive into the part believing you are
                # protected. So a big blob is never a shadow, and overruling
                # takes positive evidence of an empty bed, not just weak
                # evidence of an object.
                if final.largest_area_mm2 > rail:
                    note = "arbiter advisory p={:.2f} (blob too large to overrule)".format(p)
                    arb["overruled"] = False
                    arb["rail_hit"] = True
                elif p < float(acfg["confirm_threshold"]):
                    verdict = "clear"
                    note = "arbiter overruled p={:.2f}".format(p)
                    arb["overruled"] = True
                else:
                    note = "arbiter confirmed p={:.2f}".format(p)
                    arb["overruled"] = False

        result.update(
            verdict=verdict,
            reason=note or ("object detected" if verdict == "occupied"
                            else "bed clear"),
            area_mm2=round(final.largest_area_mm2, 1),
            max_z=round(final.max_z, 2),
            blobs=len(final.blobs),
        )

        # Slow adaptation to plate wear/staining, only on a confidently clear bed.
        if adapt and verdict == "clear" and not final.blobs:
            try:
                with self.lock:
                    feats, _ = detect.align(model.anchor,
                                            detect.features(final.warped))
                    model.add(feats, decay=float(cfg["watch"]["adapt_decay"]))
                    model.save(self.model_path())
            except Exception:
                log.exception("adaptive update failed")

        result["image"] = self._store_debug(final, verdict, note, cfg)
        return self._finish(result, started)

    def _store_debug(self, result, verdict: str, note: str, cfg: dict) -> str:
        img = detect.annotate(result, verdict, note)
        name = "{}-{}.jpg".format(int(time.time()), uuid.uuid4().hex[:6])
        cv2.imwrite(config.data_path(self.name, "history", name), img)
        self._prune_history(int(cfg["watch"]["history"]))
        return name

    def _prune_history(self, keep: int) -> None:
        d = os.path.dirname(config.data_path(self.name, "history", "x"))
        try:
            files = sorted(os.listdir(d))
        except FileNotFoundError:
            return
        for f in files[:-max(keep, 1)]:
            try:
                os.remove(os.path.join(d, f))
            except OSError:
                pass

    def _finish(self, result: dict, started: float) -> dict:
        result["took_s"] = round(time.time() - started, 2)
        self.last = result
        self.history.append(result)
        return result

    # -------------------------------------------------------------- tuning
    def tune(self, rounds: int = 15, delay: float = 2.0) -> dict:
        """Measure the noise floor against a bed you know is clear, and derive
        thresholds from it instead of guessing."""
        cfg = self.cfg
        params = dict(cfg["detect"])
        params["min_area_mm2"] = 0.0
        params["confirm_delay_s"] = 0.0
        model = self.load_model()
        geom = cfg["geometry"]
        mask = geometry.mask(geom)

        zs, areas = [], []
        for i in range(rounds):
            _, warped = self._grab_warped(cfg)
            r = detect.detect(warped, model, geom, params, mask)
            zs.append(r.max_z)
            areas.append(r.largest_area_mm2)
            if i < rounds - 1:
                time.sleep(delay)

        worst_z, worst_area = max(zs), max(areas)
        return {
            "rounds": rounds,
            "max_z_observed": round(worst_z, 2),
            "max_blob_mm2_observed": round(worst_area, 1),
            "suggested": {
                "z_threshold": round(max(4.0, worst_z * 1.3), 1),
                "min_area_mm2": round(max(50.0, worst_area * 2.0), 0),
            },
            "note": "Run this with an EMPTY bed. The values are your noise floor.",
        }

    # --------------------------------------------------------------- watch
    def _thumbnail(self, cfg: dict):
        """A cheap, geometry-aware view of the bed, for change detection only.

        One frame instead of the median stack, warped so the threshold can be
        stated in bed mm2, then shrunk until a pixel covers ~2mm. Costs a few
        milliseconds against roughly a quarter second for the real pipeline.
        """
        warped = geometry.warp(self._grab(cfg, 1, 0), cfg["geometry"])
        scale = THUMB_PX_PER_MM / float(cfg["geometry"]["px_per_mm"])
        small = cv2.resize(warped, None, fx=scale, fy=scale,
                           interpolation=cv2.INTER_AREA)
        return cv2.GaussianBlur(cv2.cvtColor(small, cv2.COLOR_BGR2GRAY), (3, 3), 0)

    def _scene_changed(self, cfg: dict):
        """(changed, why, thumbnail) - whether the full pipeline needs to run."""
        w = cfg["watch"]
        try:
            thumb = self._thumbnail(cfg)
        except Exception as exc:
            # If even a single frame cannot be had, let the full path run and
            # report the failure properly instead of silently coasting.
            self._thumb = None
            return True, f"preview failed: {exc}", None

        if self._thumb is None or self._thumb.shape != thumb.shape:
            return True, "no baseline", thumb
        if time.time() - self._thumb_at > float(w.get("max_skip_s", 60.0)):
            return True, "max_skip_s elapsed", thumb

        delta = int(w.get("change_delta", 8))
        moved_px = int(np.count_nonzero(cv2.absdiff(thumb, self._thumb) > delta))
        moved_mm2 = moved_px / (THUMB_PX_PER_MM ** 2)
        # A quarter of the smallest blob the detector would report: a wide
        # margin, so the gate errs towards doing the work.
        trigger = 0.25 * float(cfg["detect"]["min_area_mm2"])
        if moved_mm2 >= trigger:
            return True, f"{moved_mm2:.0f}mm2 moved", thumb
        return False, f"{moved_mm2:.0f}mm2 moved (under {trigger:.0f})", thumb

    def watch_tick(self) -> dict:
        cfg = self.cfg
        macro = cfg["macro"]

        ok, reason, pose = self.pose_status(cfg)
        actual = (pose or {}).get("macro_state")
        if not ok:
            if reason.startswith("printer is "):
                # Mid-print: stay completely out of the gcode queue.
                self.last = {"verdict": "skipped", "reason": reason,
                             "at": time.time()}
                return self.last
            self._publish(cfg, macro, False, False, 0.0, actual)
            self.last = {"verdict": "unknown", "reason": reason,
                         "at": time.time()}
            return self.last

        if not self.has_reference():
            self._publish(cfg, macro, False, False, 0.0, actual)
            self.last = {"verdict": "unknown", "reason": "no reference captured",
                         "at": time.time()}
            return self.last

        thumb = None
        if bool(cfg["watch"].get("change_gate", True)):
            changed, why, thumb = self._scene_changed(cfg)
            if not changed and self.last.get("verdict") in ("clear", "occupied"):
                # Nothing moved since the last real check, so its verdict still
                # stands. Publish anyway: _publish only reaches Klipper when
                # something differs or the heartbeat is due, and that heartbeat
                # is what stops the Klipper-side watchdog expiring the state.
                self._publish(cfg, macro, True,
                              self.last["verdict"] == "occupied",
                              float(self.last.get("area_mm2") or 0.0), actual)
                self.last = {**self.last, "at": time.time(), "skipped": why}
                return self.last

        res = self.run_check(adapt=bool(cfg["watch"]["adapt"]))
        if res["verdict"] == "unknown":
            self._publish(cfg, macro, False, False, 0.0, actual)
        else:
            self._publish(cfg, macro, True, res["verdict"] == "occupied",
                          res["area_mm2"], actual)
            if thumb is not None:
                self._thumb, self._thumb_at = thumb, time.time()
        return res

    @staticmethod
    def _significant_area_change(a: float, b: float) -> bool:
        """Measured area jitters by a few mm2 between frames. Republishing on
        that would spam the Klipper console every tick for as long as an object
        sits on the bed."""
        return abs(a - b) > max(25.0, 0.2 * max(a, b))

    def _needs_publish(self, desired: tuple, actual: dict | None) -> bool:
        if actual is None:
            return desired != self._published
        if (actual["valid"], actual["occupied"]) != desired[:2]:
            return True
        return self._significant_area_change(actual["area"], desired[2])

    def _publish(self, cfg: dict, macro: str, valid: bool, occupied: bool,
                 area: float, actual: dict | None = None) -> None:
        desired = (int(valid), int(occupied), round(area, 1))
        heartbeat = float(cfg["watch"]["heartbeat_s"])
        stale = (time.time() - self._published_at) > heartbeat
        if not stale and not self._needs_publish(desired, actual):
            return
        # Three heartbeats of slack, so ordinary jitter never expires a state
        # that is simply unchanged.
        ttl = float(cfg["watch"].get("state_ttl_s") or 0.0) or 3.0 * heartbeat
        try:
            self.moonraker(cfg).publish(macro, valid, occupied, area, ttl)
            self._published = desired
            self._published_at = time.time()
        except MoonrakerError as exc:
            log.warning("[%s] publish failed: %s", self.name, exc)
