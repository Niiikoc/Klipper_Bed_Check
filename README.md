# Klipper_Bed_Check

Detects an object left behind on the bed, before Klipper homes or starts a new
print.

It runs as a systemd service — on the printer itself, or in an LXC / another
machine on the network. All Klipper needs is **one `.cfg` with two macros**: no
python dependency on the printer, no `gcode_shell_command`.

---

## How it works

```
         ┌─────── bed-check (systemd or container) ───────┐
         │                                                │
 snapshot│   ┌────────────┐   flag   ┌────────────────┐   │
────────►│   │ CV         ├─────────►│ CLIP arbiter   │   │
crowsnest│   │ background │          │ (only when a   │   │
         │   │ model      │          │  flag is up)   │   │
         │   └──────┬─────┘          └───────┬────────┘   │
         │          │ clear                  ▼            │
         │          ▼                                     │
         │      valid / occupied / area  (verdict)        │
         └───────────────────────┬────────────────────────┘
                                 │ SET_GCODE_VARIABLE
                                 ▼  (only while the printer is idle)
                       ┌────────────────────┐
                       │ Klipper BED_STATE  │
                       └─────────┬──────────┘
                                 │ read instantly
                                 ▼
                       PRINT_START → BED_CLEAR_GATE
```

**Why a watcher instead of a check inside `PRINT_START`:** `RUN_SHELL_COMMAND`
raises no gcode error on a non-zero exit code or on a timeout, so it cannot
abort a print. And if a script tries to send gcode back to Klipper while the
macro is running, it blocks on the gcode mutex. The watcher writes the state
**while the printer is idle**, and `PRINT_START` just reads a value that is
already there: zero delay, zero deadlock.

### What the CV does
- **Perspective rectification** to a top-down view, so the thresholds are in
  real mm² and not in pixels.
- **4 channels**: normalized brightness, gradient, and two colour channels
  (Lab a/b). The colour ones catch the very common "coloured filament with the
  same brightness as the bed" case, which in grayscale is **invisible**.
- The low-frequency component is subtracted from every channel → immunity to
  lighting changes, shadows and white balance.
- **Per-pixel sigma**: glossy spots and reflections learn a large sigma and
  stop raising alarms on their own.
- The sigma floor **rises automatically** with the noise of the current frame
  (a camera that pushes its gain up at night).
- **ECC alignment** absorbs small camera movements.
- A second confirmation capture: a hand or a passing shadow does not repeat.

### What the arbiter does
CLIP runs **only when the CV raises a flag** — that is, rarely — and answers a
binary question about the crop: empty bed, or printed part?

Two safeguards, because turning a true positive into a false negative is
**worse** than a false alarm:
- It overrides a detection only if `P(occupied) < confirm_threshold` (default
  0.25) — that is, it needs positive evidence of an empty bed, not merely weak
  evidence of an object.
- It **never overrides** a blob larger than `max_override_area_mm2` (default
  2000mm²). Something that big is not a shadow.

---

## Installation

### Proxmox — automatic LXC creation

Run this **in the shell of the Proxmox node**. It builds the container from
scratch, sets it up, and hands you a working service:

```bash
bash -c "$(curl -fsSL https://raw.githubusercontent.com/Niiikoc/Klipper_Bed_Check/main/proxmox/create-lxc.sh)"
```

With a pre-filled config, so that no manual editing is needed at all:

```bash
./create-lxc.sh -y \
  --printer-name kratos \
  --snapshot-url "http://192.168.1.50/webcam/?action=snapshot" \
  --moonraker-url "http://192.168.1.50:7125" \
  --bed-size "350,350"
```

Defaults: unprivileged, Debian 12, 2 cores, 3GB RAM, 12GB disk, DHCP. With
`--no-arbiter` it drops to 1GB RAM / 6GB disk. The next free CTID and the
storages are detected on their own.

```
--ctid 210 --hostname bedcheck        # specific ID/name
--ip 192.168.1.60/24 --gw 192.168.1.1 # static network
--ram 4096 --disk 16 --cores 4        # resources
--local /root/bed-check               # from a local copy, without git
--no-arbiter                          # classical CV only
```

`--local` is useful for testing it **before** pushing to GitHub: `scp -r` the
folder onto the node and point at it.

If something breaks halfway through, the script tells you exactly how to clean
up the half-built container.

<details>
<summary>Manual installation (Pi, existing LXC, any Debian)</summary>

```bash
cd ~
git clone https://github.com/Niiikoc/Klipper_Bed_Check.git
cd Klipper_Bed_Check
./install.sh
```

The script sets up a venv, installs the dependencies, creates a systemd
service, and — if it finds a `moonraker.conf` on the same machine — adds an
`update_manager` entry and copies `bed_check.cfg` over. It is idempotent:
Moonraker re-runs it on every update without touching your config.

```
./install.sh --no-arbiter     # classical CV only, without torch (~150MB)
./install.sh --allow-root     # inside a container, where you run as root
./install.sh --port 9000
./install.sh -y               # no questions
./uninstall.sh                # remove the service (keeps config + reference)
```
</details>

The UI: `http://<host>:8790`

<details>
<summary>Alternatively with Docker</summary>

```bash
mkdir -p config data
cp config/config.example.yaml config/config.yaml
docker compose up -d --build
docker compose build --build-arg WITH_ARBITER=false   # lean image
```

Docker does **not** give you Moonraker `update_manager` integration.
</details>

### Updates

If bed-check runs **on the same host as Moonraker**, it shows up in the
Mainsail/Fluidd update list and updates with one click. Otherwise:

```bash
cd ~/Klipper_Bed_Check && git pull && ./install.sh
```

### 2. Moonraker

Give the container access — in `moonraker.conf`:

```ini
[authorization]
trusted_clients:
    192.168.1.0/24        # or just the IP of the LXC
```

Alternatively, set `moonraker_api_key` in the config.

### 3. Klipper

If `install.sh` ran on the printer, `bed_check.cfg` has already been copied.
Otherwise:

```bash
scp klipper/bed_check.cfg pi@printer:~/printer_data/config/
```

Either way, in `printer.cfg`:
```ini
[include bed_check.cfg]
```

And as the **first line** of `PRINT_START`, before any movement or heating:
```gcode
[gcode_macro PRINT_START]
gcode:
    BED_CLEAR_GATE
    ...
```

### 4. Camera — the most important step

Lock exposure and white balance. A camera on auto changes its exposure when you
put your hand in the frame, and that is the number one source of false alarms.
In `crowsnest.conf`:

```ini
[cam 1]
v4l2ctl: auto_exposure=1,exposure_time_absolute=250,white_balance_automatic=0,white_balance_temperature=4600
```

The control names differ per driver — check yours with:

```bash
v4l2-ctl -d /dev/video0 --list-ctrls
```

Ideally add a fixed LED that is always on.

---

## Setup from the web UI

1. **Corners** — click the 4 bed corners in the order TL → TR → BR → BL. Enter
   the bed size and press Save.
2. **Preview** — the bed must look rectangular and fill the frame. The grid is
   in mm; use it for `exclude_zones_mm` if you want to ignore clips or the
   toolhead parking position.
3. **Reference** — clear the bed **completely** and press "New reference".
   Later, with **Append**, add captures under other lighting conditions (e.g.
   midday and night) — sigma widens exactly where the scene really changes.
4. **Tune** — with an empty bed press "Measure noise floor". It suggests
   thresholds from measured noise instead of guesswork. Press "Apply".
5. **Test it** — put a part on the bed, press "Check now", look at the debug
   image. Repeat with small and dark objects.

### Calibrating the arbiter
The history shows `P(occupied)` for every detection. Collect a few real cases
and set `confirm_threshold` so that real parts sit clearly above it. The 0.25
default is conservative on purpose.

---

## Behaviour when it cannot see

**Fail-open** by default: if the camera drops out, there is no reference, or the
toolhead is not parked, `valid` becomes 0, `PRINT_START` prints a warning and
**continues**. It never blocks a print of yours because of its own problem.

For fail-closed, in `bed_check.cfg`:
```ini
[gcode_macro BED_CLEAR_GATE]
variable_on_unknown: 'abort'
```

---

## Settings worth paying attention to

| Key | Default | What it does |
|---|---|---|
| `detect.z_threshold` | 6.0 | How many sigma above the reference counts as a change. Set it from `tune`. |
| `detect.min_area_mm2` | 100 | Minimum detection **footprint**. It includes a ~3mm halo, so a 5mm part measures ~100mm². |
| `detect.noise_gain` | 2.0 | Tolerance for a camera that gets noisy. |
| `detect.confirm_delay_s` | 1.5 | Second capture before calling it occupied. 0 to disable. |
| `pose_gate.park_xy` | null | Accept a measurement only with the toolhead here. **Required on a bedslinger**, where the bed's Y position changes the frame. |
| `watch.adapt` | true | Slow adaptation to PEI wear and stains. |
| `watch.interval_s` | 5 | Check frequency while the printer is idle. |

**Rebuild the reference** whenever you change the build plate, move the camera,
or change `px_per_mm` / the corners.

---

## Troubleshooting

| Symptom | Cause |
|---|---|
| `valid` stays 0 | Check `/api/printers/<name>/status` — usually `moonraker unreachable` (trusted_clients) or `toolhead not parked`. |
| False alarms at night | Auto-exposure is on. Lock it, and `Append` a reference under the night-time conditions. |
| Dark parts on dark PEI are missed | Raise `px_per_mm` to 3.0 and rebuild the reference. |
| A flag always in the same spot | The toolhead parking position is in the frame — add that area to `exclude_zones_mm`. |
| CLIP overrides correct detections | Lower `confirm_threshold`, or set `arbiter.enabled: false`. The area rail already covers you for large parts. |

---

## Tests

```bash
python -m venv .venv && ./.venv/bin/pip install -r requirements.txt
./.venv/bin/python tests/run_all.py
```

No printer and no camera required — they build a synthetic bed, a fake camera
and a fake Moonraker. They cover: illumination / white-balance invariance,
sensor noise, camera movement, isoluminant coloured parts, a thin skirt ring,
exclude zones, the exact gcode that reaches Klipper, staying silent during a
print, resynchronising after a `FIRMWARE_RESTART`, and the arbiter safeguards.

`test_arbiter.py` downloads CLIP (~600MB) the first time.
