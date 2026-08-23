"""FastAPI app: REST API, static web UI, and the per-printer watch loops."""
from __future__ import annotations

import asyncio
import copy
import logging
import os
from contextlib import asynccontextmanager

from fastapi import Body, FastAPI, HTTPException, Query
from fastapi.responses import FileResponse, HTMLResponse, Response

from . import arbiter, config, geometry
from .service import PrinterService

logging.basicConfig(
    level=os.environ.get("BEDCHECK_LOGLEVEL", "INFO"),
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)
log = logging.getLogger("bedcheck")

STATIC = os.path.join(os.path.dirname(__file__), "static")

_services: dict[str, PrinterService] = {}
_tasks: dict[str, asyncio.Task] = {}


def service(name: str) -> PrinterService:
    if name not in _services:
        try:
            config.printer(config.load(), name)
        except KeyError:
            raise HTTPException(404, f"unknown printer '{name}'")
        _services[name] = PrinterService(name)
    return _services[name]


def _locked(svc: PrinterService, fn, *args, **kwargs):
    """Serialise everything that touches the camera for a given printer."""
    with svc.lock:
        return fn(*args, **kwargs)


async def run_locked(svc: PrinterService, fn, *args, **kwargs):
    return await asyncio.to_thread(_locked, svc, fn, *args, **kwargs)


# --------------------------------------------------------------- watchers
async def watch_loop(name: str) -> None:
    svc = service(name)
    log.info("[%s] watcher started", name)
    while True:
        try:
            cfg = svc.cfg
        except KeyError:
            log.info("[%s] removed from config, watcher stopping", name)
            return
        interval = float(cfg["watch"]["interval_s"])
        if not cfg.get("enabled", True):
            await asyncio.sleep(max(interval, 5.0))
            continue
        try:
            await run_locked(svc, svc.watch_tick)
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("[%s] watch tick failed", name)
        await asyncio.sleep(interval)


async def supervisor() -> None:
    """Start/stop watchers as printers appear or disappear from the config."""
    while True:
        try:
            names = {p["name"] for p in config.load()["printers"]}
        except Exception:
            log.exception("config reload failed")
            names = set(_tasks)

        for name in names - set(_tasks):
            _tasks[name] = asyncio.create_task(watch_loop(name))
        for name in set(_tasks) - names:
            _tasks.pop(name).cancel()
        for name, task in list(_tasks.items()):
            if task.done():
                _tasks.pop(name)
        await asyncio.sleep(10)


@asynccontextmanager
async def lifespan(app: FastAPI):
    sup = asyncio.create_task(supervisor())
    yield
    sup.cancel()
    for task in _tasks.values():
        task.cancel()


app = FastAPI(title="bed-check", lifespan=lifespan)


# -------------------------------------------------------------------- API
@app.get("/api/health")
def health():
    return {
        "ok": True,
        "arbiter_available": arbiter.available(),
        "printers": [p["name"] for p in config.load()["printers"]],
        "watchers": sorted(_tasks),
    }


@app.get("/api/config")
def get_config():
    return config.load()


@app.get("/api/printers")
def list_printers():
    out = []
    for p in config.load()["printers"]:
        svc = service(p["name"])
        out.append({
            "name": p["name"],
            "enabled": p.get("enabled", True),
            "calibrated": bool(p["geometry"].get("corners_px")),
            "has_reference": svc.has_reference(),
            "last": svc.last,
        })
    return out


@app.get("/api/printers/{name}/config")
def printer_config(name: str):
    try:
        return config.printer(config.load(), name)
    except KeyError:
        raise HTTPException(404, f"unknown printer '{name}'")


@app.patch("/api/printers/{name}/config")
def patch_printer_config(name: str, patch: dict = Body(...)):
    try:
        before = config.printer(config.load(), name)
    except KeyError:
        raise HTTPException(404, f"unknown printer '{name}'")

    patch = copy.deepcopy(patch)
    warnings: list[str] = []

    # Rotating or flipping moves every pixel, so corners clicked in the old
    # orientation and the reference learned from it are both meaningless.
    # Keeping them would warp through a quad that no longer matches the image
    # and silently produce nonsense, so drop both and say so.
    cap = patch.get("capture") or {}
    old_cap = before.get("capture") or {}
    reoriented = any(
        key in cap and str(cap[key]) != str(old_cap.get(key, ""))
        for key in ("rotate", "flip")
    )
    if reoriented:
        svc = service(name)
        if before["geometry"].get("corners_px"):
            patch.setdefault("geometry", {})["corners_px"] = None
            warnings.append("corners cleared - recalibrate them")
        if svc.has_reference():
            try:
                os.remove(svc.model_path())
                warnings.append("reference discarded - capture a new one")
            except OSError as exc:
                log.warning("could not remove stale reference: %s", exc)

    # Normalise here as well as in geometry.homography so the stored order is
    # the canonical one and the UI redraws the dots the way they are used.
    geom = patch.get("geometry") or {}
    if geom.get("corners_px"):
        try:
            geom["corners_px"] = geometry.order_corners(geom["corners_px"]).tolist()
        except geometry.GeometryError as exc:
            raise HTTPException(400, str(exc))

    try:
        updated = config.update_printer(name, patch)
    except KeyError:
        raise HTTPException(404, f"unknown printer '{name}'")
    return {**updated, "warnings": warnings}


@app.get("/api/printers/{name}/status")
def status(name: str):
    svc = service(name)
    cfg = svc.cfg
    ok, reason, pose = svc.pose_status(cfg)
    return {
        "name": name,
        "pose_ok": ok,
        "pose_reason": reason,
        "pose": pose,
        "has_reference": svc.has_reference(),
        "calibrated": bool(cfg["geometry"].get("corners_px")),
        "last": svc.last,
    }


@app.get("/api/printers/{name}/history")
def history(name: str, limit: int = Query(20, ge=1, le=200)):
    return list(service(name).history)[-limit:][::-1]


def _jpg(data: bytes) -> Response:
    return Response(content=data, media_type="image/jpeg",
                    headers={"Cache-Control": "no-store"})


@app.get("/api/printers/{name}/snapshot.jpg")
async def snapshot(name: str):
    svc = service(name)
    try:
        return _jpg(await asyncio.to_thread(svc.snapshot_jpg))
    except Exception as exc:
        raise HTTPException(502, str(exc))


@app.get("/api/printers/{name}/warp.jpg")
async def warp(name: str, grid: bool = True):
    svc = service(name)
    try:
        return _jpg(await asyncio.to_thread(svc.warp_jpg, grid))
    except Exception as exc:
        raise HTTPException(400, str(exc))


@app.get("/api/printers/{name}/image/{filename}")
def debug_image(name: str, filename: str):
    if "/" in filename or "\\" in filename or ".." in filename:
        raise HTTPException(400, "bad filename")
    path = config.data_path(name, "history", filename)
    if not os.path.exists(path):
        raise HTTPException(404, "no such image")
    return FileResponse(path, media_type="image/jpeg",
                        headers={"Cache-Control": "no-store"})


@app.post("/api/printers/{name}/reference")
async def reference(name: str,
                    captures: int = Query(12, ge=3, le=60),
                    mode: str = Query("reset", pattern="^(reset|append)$")):
    svc = service(name)
    try:
        return await run_locked(svc, svc.build_reference, captures, mode)
    except Exception as exc:
        raise HTTPException(400, str(exc))


@app.post("/api/printers/{name}/check")
async def check(name: str, adapt: bool = False):
    svc = service(name)
    return await run_locked(svc, svc.run_check, adapt)


@app.post("/api/printers/{name}/tune")
async def tune(name: str,
               rounds: int = Query(15, ge=3, le=60),
               delay: float = Query(2.0, ge=0.2, le=30.0)):
    svc = service(name)
    try:
        return await run_locked(svc, svc.tune, rounds, delay)
    except Exception as exc:
        raise HTTPException(400, str(exc))


@app.post("/api/printers/{name}/publish")
async def publish_now(name: str):
    """Force a push of the current verdict into Klipper (useful after edits)."""
    svc = service(name)
    svc._published = None
    res = await run_locked(svc, svc.watch_tick)
    return res


# --------------------------------------------------------------------- UI
@app.get("/", response_class=HTMLResponse)
def index():
    with open(os.path.join(STATIC, "index.html"), encoding="utf-8") as fh:
        return HTMLResponse(fh.read())
