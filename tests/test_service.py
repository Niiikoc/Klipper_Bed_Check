"""Integration test: whole service against a fake camera and fake Moonraker.

Exercises config -> calibration -> reference -> check -> publish, and asserts
the exact gcode that lands in Klipper.
"""
import json
import os
import shutil
import sys
import tempfile
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs

import cv2
import numpy as np

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from test_pipeline import BASE, capture, with_object  # noqa: E402

# --------------------------------------------------------------- fake rig
STATE = {
    "scene": "clear",
    "printer_state": "standby",
    "position": [10.0, 10.0, 30.0, 0.0],
    "homed_axes": "xyz",
    "macro": {"valid": 0, "occupied": 0, "area": 0.0, "ttl": 0},
    "scripts": [],
}


def scene_jpg():
    img = capture() if STATE["scene"] == "clear" else with_object(35)
    return cv2.imencode(".jpg", img, [cv2.IMWRITE_JPEG_QUALITY, 92])[1].tobytes()


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _json(self, obj, code=200):
        body = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        u = urlparse(self.path)
        if u.path == "/webcam/":
            body = scene_jpg()
            self.send_response(200)
            self.send_header("Content-Type", "image/jpeg")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        if u.path == "/printer/objects/query":
            q = parse_qs(u.query)
            status = {}
            if "print_stats" in q:
                status["print_stats"] = {"state": STATE["printer_state"]}
            if "toolhead" in q:
                status["toolhead"] = {"position": STATE["position"],
                                      "homed_axes": STATE["homed_axes"]}
            for key in q:
                if key.startswith("gcode_macro "):
                    status[key] = dict(STATE["macro"])
            self._json({"result": {"status": status, "eventtime": 1.0}})
            return
        self._json({"error": "not found"}, 404)

    def do_POST(self):
        u = urlparse(self.path)
        n = int(self.headers.get("Content-Length", 0))
        payload = json.loads(self.rfile.read(n) or b"{}")
        if u.path == "/printer/gcode/script":
            script = payload.get("script", "")
            STATE["scripts"].append(script)
            for line in script.splitlines():
                parts = dict(p.split("=", 1) for p in line.split() if "=" in p)
                var, val = parts.get("VARIABLE"), parts.get("VALUE")
                if var in STATE["macro"]:
                    STATE["macro"][var] = float(val) if var == "area" else int(val)
            self._json({"result": "ok"})
            return
        self._json({"error": "not found"}, 404)


def start_server():
    srv = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv, srv.server_address[1]


# ------------------------------------------------------------------ tests
results = []


def check(label, cond, detail=""):
    results.append(cond)
    print(f"  [{'ok ' if cond else 'FAIL'}] {label}" + (f"  {detail}" if detail else ""))


def main():
    srv, port = start_server()
    base = f"http://127.0.0.1:{port}"
    tmp = tempfile.mkdtemp(prefix="bedcheck-")
    cfg_path = os.path.join(tmp, "config.yaml")

    with open(cfg_path, "w", encoding="utf-8") as fh:
        fh.write(f"""
server:
  data_dir: {json.dumps(os.path.join(tmp, 'data'))}
arbiter:
  enabled: true
printers:
  - name: rig
    snapshot_url: {base}/webcam/
    moonraker_url: {base}
    macro: BED_STATE
    geometry:
      bed_size_mm: [350, 350]
      px_per_mm: 2.0
      edge_margin_mm: 8
    watch:
      interval_s: 1
      heartbeat_s: 3600
      adapt: false
""")

    os.environ["BEDCHECK_CONFIG"] = cfg_path
    os.environ["BEDCHECK_DATA"] = os.path.join(tmp, "data")

    from fastapi.testclient import TestClient
    from app.main import app

    c = TestClient(app)

    h = c.get("/api/health").json()
    check("health responds", h["ok"] and h["printers"] == ["rig"],
          f"arbiter_available={h['arbiter_available']}")

    # A check before calibration must fail open, not explode.
    r = c.post("/api/printers/rig/check").json()
    check("uncalibrated -> unknown", r["verdict"] == "unknown", r["reason"])

    # Calibrate corners.
    size = BASE.shape[0] - 1
    r = c.patch("/api/printers/rig/config", json={"geometry": {
        "corners_px": [[0, 0], [size, 0], [size, size], [0, size]]}})
    check("corners saved", r.status_code == 200)
    check("config persisted to disk",
          "corners_px" in open(cfg_path, encoding="utf-8").read())

    r = c.get("/api/printers/rig/warp.jpg")
    check("warp preview renders", r.status_code == 200 and
          r.headers["content-type"] == "image/jpeg", f"{len(r.content)} bytes")

    # Build the reference on a clear bed.
    r = c.post("/api/printers/rig/reference?captures=8&mode=reset").json()
    check("reference built", r.get("samples") == 8,
          f"channels={r.get('channels')}")

    r = c.post("/api/printers/rig/check").json()
    check("clear bed -> clear", r["verdict"] == "clear",
          f"area={r['area_mm2']} max_z={r['max_z']}")
    check("debug image stored", bool(r["image"]))
    img = c.get(f"/api/printers/rig/image/{r['image']}")
    check("debug image served", img.status_code == 200)

    check("path traversal blocked",
          c.get("/api/printers/rig/image/..%2F..%2Fconfig.yaml").status_code
          in (400, 404))

    # Append mode must extend, not restart.
    r = c.post("/api/printers/rig/reference?captures=4&mode=append").json()
    check("append extends model", r.get("samples") == 12, f"n={r.get('samples')}")

    # Now put an object on the bed.
    STATE["scene"] = "object"
    r = c.post("/api/printers/rig/check").json()
    check("object -> occupied", r["verdict"] == "occupied",
          f"area={r['area_mm2']}mm2 blobs={r['blobs']}")
    arb = r.get("arbiter") or {}
    # Whether or not CLIP is installed, a real object must survive this stage:
    # if absent it fails open, if present the area rail protects the verdict.
    check("verdict survives the arbiter stage", r["verdict"] == "occupied",
          f"ran={arb.get('ran')} p={arb.get('p_occupied')} "
          f"err={arb.get('error')}")

    # --- arbiter safety rails, with CLIP stubbed to "this is an empty bed" ---
    from app import service as service_mod

    real_judge = service_mod.arbiter.judge
    service_mod.arbiter.judge = lambda *a, **k: {
        "ran": True, "p_occupied": 0.05, "per_blob": [0.05], "error": None}
    try:
        # Blob is ~2300mm2, above the 2000mm2 rail -> must NOT be overruled.
        r = c.post("/api/printers/rig/check").json()
        check("large blob survives a wrong arbiter",
              r["verdict"] == "occupied", f"{r['reason']}")
        check("rail is reported", (r.get("arbiter") or {}).get("rail_hit") is True)

        # Raise the rail above the blob -> now the overrule is allowed.
        c.patch("/api/printers/rig/config", json={})
        import app.config as appcfg
        full = appcfg.load()
        full["arbiter"]["max_override_area_mm2"] = 50000.0
        appcfg.save(full)
        r = c.post("/api/printers/rig/check").json()
        check("arbiter can overrule below the rail",
              r["verdict"] == "clear" and (r["arbiter"] or {}).get("overruled"),
              r["reason"])

        # A confident arbiter must confirm, not suppress.
        service_mod.arbiter.judge = lambda *a, **k: {
            "ran": True, "p_occupied": 0.93, "per_blob": [0.93], "error": None}
        r = c.post("/api/printers/rig/check").json()
        check("confident arbiter confirms", r["verdict"] == "occupied", r["reason"])

        full["arbiter"]["max_override_area_mm2"] = 2000.0
        appcfg.save(full)
    finally:
        service_mod.arbiter.judge = real_judge

    # Publish path: exact gcode into Klipper.
    STATE["scripts"].clear()
    r = c.post("/api/printers/rig/publish").json()
    sent = "\n".join(STATE["scripts"])
    check("publish -> occupied gcode",
          "VARIABLE=occupied VALUE=1" in sent and "VARIABLE=valid VALUE=1" in sent,
          sent.replace("\n", " | "))
    check("klipper macro state updated",
          STATE["macro"]["occupied"] == 1 and STATE["macro"]["valid"] == 1)

    # No redundant traffic when nothing changed.
    STATE["scripts"].clear()
    c.post("/api/printers/rig/check")
    from app.main import service
    service("rig").watch_tick()
    check("no republish when unchanged", STATE["scripts"] == [],
          f"{len(STATE['scripts'])} scripts")

    # Klipper restarted: variables reset, watcher must notice and re-push.
    STATE["macro"] = {"valid": 0, "occupied": 0, "area": 0.0, "ttl": 0}
    STATE["scripts"].clear()
    service("rig").watch_tick()
    check("re-publishes after klipper restart",
          STATE["macro"]["occupied"] == 1, f"{len(STATE['scripts'])} scripts")

    # Bed cleared again.
    STATE["scene"] = "clear"
    STATE["scripts"].clear()
    service("rig").watch_tick()
    check("clearing bed -> occupied=0",
          STATE["macro"]["occupied"] == 0 and STATE["macro"]["valid"] == 1)

    # Freshness: a dead watcher must not leave a stale "clear" behind, so
    # every publish refills the countdown BED_CHECK_WATCHDOG ticks down.
    check("publish refills the klipper watchdog ttl",
          STATE["macro"]["ttl"] > 0, f"ttl={STATE['macro']['ttl']}s")

    svc = service("rig")

    # Change gate: an untouched bed must not cost a full CV pass.
    STATE["scripts"].clear()
    out = svc.watch_tick()
    check("unchanged scene -> gate skips the pipeline",
          out.get("skipped") is not None and out["verdict"] == "clear",
          out.get("skipped", "ran the full pipeline"))

    # ...but the heartbeat still has to reach Klipper, or the watchdog would
    # expire a state that is merely unchanged.
    svc._published_at = 0.0
    STATE["scripts"].clear()
    svc.watch_tick()
    check("skipped tick still heartbeats",
          any("VARIABLE=ttl" in s for s in STATE["scripts"]),
          f"{len(STATE['scripts'])} scripts")

    # A part appearing has to break through the gate.
    STATE["scene"] = "object"
    out = svc.watch_tick()
    check("new object breaks through the gate",
          out["verdict"] == "occupied" and out.get("skipped") is None,
          f"area={out.get('area_mm2')}mm2")

    # Safety valve: never coast past max_skip_s even if nothing looks different.
    svc.watch_tick()               # settle, so the baseline matches the scene
    svc._thumb_at = 0.0            # pretend the last real check was long ago
    out = svc.watch_tick()
    check("max_skip_s forces a real check", out.get("skipped") is None,
          "full pipeline ran")

    STATE["scene"] = "clear"
    svc.watch_tick()

    # Mid-print the watcher must not touch the gcode queue at all.
    STATE["printer_state"] = "printing"
    STATE["scripts"].clear()
    out = service("rig").watch_tick()
    check("skips while printing",
          out["verdict"] == "skipped" and STATE["scripts"] == [], out["reason"])
    STATE["printer_state"] = "standby"

    # Pose gate: toolhead parked away from where it should be.
    c.patch("/api/printers/rig/config",
            json={"pose_gate": {"park_xy": [175, 350], "tolerance_mm": 5}})
    STATE["scripts"].clear()
    out = service("rig").watch_tick()
    check("pose gate -> unknown + valid=0",
          out["verdict"] == "unknown" and STATE["macro"]["valid"] == 0,
          out["reason"])

    # Camera down must fail open, not crash the loop.
    c.patch("/api/printers/rig/config", json={
        "pose_gate": {"park_xy": None},
        "snapshot_url": f"http://127.0.0.1:{port}/nope"})
    out = service("rig").watch_tick()
    check("camera down -> unknown + valid=0",
          out["verdict"] == "unknown" and STATE["macro"]["valid"] == 0,
          out["reason"][:60])

    r = c.get("/api/printers/rig/history?limit=5").json()
    check("history returns entries", len(r) > 0, f"{len(r)} entries")

    srv.shutdown()
    shutil.rmtree(tmp, ignore_errors=True)
    failed = results.count(False)
    print("\nFAILURES:", failed)
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
