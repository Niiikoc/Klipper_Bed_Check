#!/usr/bin/env bash
# bed-check installer.
#
# Safe to re-run: Moonraker's update_manager calls this after every update, so
# it must never clobber an existing config or reinstall what is already there.
set -euo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SERVICE="bed-check"
VENV="${REPO_DIR}/.venv"
PYTHON="${VENV}/bin/python"
PORT="${BEDCHECK_PORT:-8790}"
RUN_USER="${SUDO_USER:-$(id -un)}"
CLIP_MODEL="openai/clip-vit-base-patch32"

ARBITER=""            # yes | no | "" = decide below
SETUP_SERVICE="yes"
ASSUME_YES="no"
FRESH_CONFIG="no"
ALLOW_ROOT="no"
SUDO="sudo"

c_ok()   { printf '\033[32m%s\033[0m\n' "$*"; }
c_warn() { printf '\033[33m%s\033[0m\n' "$*"; }
c_err()  { printf '\033[31m%s\033[0m\n' "$*" >&2; }
step()   { printf '\n\033[1m==> %s\033[0m\n' "$*"; }

usage() {
    cat <<'USAGE'
Usage: ./install.sh [options]

  --arbiter        Install the CLIP arbiter (~2GB disk, ~1GB RAM when it runs)
  --no-arbiter     Classical CV only (small and fast)
  --no-service     Do not create or enable the systemd service
  --allow-root     Permit running as root (normal inside an LXC/container)
  --port N         Listen port (default 8790)
  -y, --yes        Never prompt; use defaults
  -h, --help       This text

Re-running keeps your existing config/config.yaml and your earlier arbiter
choice unless a flag overrides it.
USAGE
}

while [[ $# -gt 0 ]]; do
    case "$1" in
        --arbiter)    ARBITER="yes"; shift ;;
        --no-arbiter) ARBITER="no"; shift ;;
        --no-service) SETUP_SERVICE="no"; shift ;;
        --allow-root) ALLOW_ROOT="yes"; shift ;;
        --port)       PORT="$2"; shift 2 ;;
        -y|--yes)     ASSUME_YES="yes"; shift ;;
        -h|--help)    usage; exit 0 ;;
        *) c_err "unknown option: $1"; usage; exit 1 ;;
    esac
done

if [[ "$(id -u)" -eq 0 ]]; then
    # Already root: sudo is unnecessary and often absent in a container image.
    SUDO=""
    if [[ -z "${SUDO_USER:-}" && "$ALLOW_ROOT" != "yes" ]]; then
        c_err "Run this as your normal user (e.g. pi), not as root."
        c_err "It calls sudo itself where it needs to."
        c_err "Inside an LXC or container, pass --allow-root instead."
        exit 1
    fi
elif ! command -v sudo >/dev/null 2>&1; then
    c_err "sudo is not installed and you are not root."
    exit 1
fi

# ---------------------------------------------------------------- packages
step "System packages"
MISSING=()
for pkg in python3-venv python3-dev libglib2.0-0; do
    dpkg -s "$pkg" >/dev/null 2>&1 || MISSING+=("$pkg")
done
if [[ ${#MISSING[@]} -gt 0 ]]; then
    echo "Installing: ${MISSING[*]}"
    ${SUDO} apt-get update -qq
    ${SUDO} apt-get install -y --no-install-recommends "${MISSING[@]}"
else
    c_ok "already present"
fi

# ----------------------------------------------------------- arbiter choice
if [[ -z "$ARBITER" ]]; then
    if [[ -x "$PYTHON" ]] && "$PYTHON" -c "import torch" >/dev/null 2>&1; then
        ARBITER="yes"                      # keep what a previous run set up
        echo "Arbiter already installed, keeping it."
    elif [[ "$ASSUME_YES" == "yes" || ! -t 0 ]]; then
        ARBITER="no"
    else
        RAM_MB="$(awk '/MemTotal/ {print int($2/1024)}' /proc/meminfo)"
        echo
        echo "The CLIP arbiter double-checks flagged detections semantically and"
        echo "cuts false alarms. It costs ~2GB disk and ~1GB RAM while running."
        echo "This machine has ${RAM_MB}MB RAM."
        if [[ "$RAM_MB" -lt 2048 ]]; then
            c_warn "Below 2GB: classical CV only is recommended here."
        fi
        read -rp "Install the CLIP arbiter? [y/N] " reply
        if [[ "$reply" =~ ^[Yy]$ ]]; then ARBITER="yes"; else ARBITER="no"; fi
    fi
fi

# ------------------------------------------------------------------- venv
step "Python environment"
if [[ ! -x "$PYTHON" ]]; then
    python3 -m venv "$VENV"
    c_ok "created ${VENV}"
fi
"$PYTHON" -m pip install --upgrade pip -q
"$PYTHON" -m pip install -q -r "${REPO_DIR}/requirements.txt"
c_ok "core dependencies installed"

if [[ "$ARBITER" == "yes" ]]; then
    step "CLIP arbiter"
    if ! "$PYTHON" -c "import torch" >/dev/null 2>&1; then
        # The pytorch CPU wheel index only carries x86_64; on arm64 the plain
        # PyPI wheels are already CPU-only.
        if [[ "$(uname -m)" == "x86_64" ]]; then
            "$PYTHON" -m pip install -q \
                --index-url https://download.pytorch.org/whl/cpu torch
        else
            "$PYTHON" -m pip install -q torch
        fi
    fi
    "$PYTHON" -m pip install -q -r "${REPO_DIR}/requirements-arbiter.txt"
    echo "Fetching CLIP weights (~600MB, once) ..."
    HF_HOME="${REPO_DIR}/data/hf" "$PYTHON" -c "
from transformers import CLIPModel, CLIPProcessor
CLIPModel.from_pretrained('${CLIP_MODEL}')
CLIPProcessor.from_pretrained('${CLIP_MODEL}')
"
    c_ok "arbiter ready"
fi

# ----------------------------------------------------------------- config
step "Configuration"
mkdir -p "${REPO_DIR}/config" "${REPO_DIR}/data"
if [[ -f "${REPO_DIR}/config/config.yaml" ]]; then
    c_ok "config/config.yaml exists, left untouched"
else
    cp "${REPO_DIR}/config/config.example.yaml" "${REPO_DIR}/config/config.yaml"
    FRESH_CONFIG="yes"
    c_warn "created config/config.yaml from the example - you must edit it"
fi

if [[ "$ARBITER" == "no" && "$FRESH_CONFIG" == "yes" ]]; then
    "$PYTHON" -c "
import io, re
p = '${REPO_DIR}/config/config.yaml'
s = io.open(p, encoding='utf-8').read()
s = re.sub(r'(^arbiter:\s*\n(?:[ \t].*\n|\n)*?[ \t]+enabled:[ \t]*)true',
           r'\1false', s, count=1, flags=re.M)
io.open(p, 'w', encoding='utf-8', newline='\n').write(s)
" || c_warn "could not auto-disable the arbiter in config.yaml; set arbiter.enabled: false yourself"
fi

# ---------------------------------------------------------------- service
if [[ "$SETUP_SERVICE" == "yes" ]]; then
    step "systemd service"
    ${SUDO} tee "/etc/systemd/system/${SERVICE}.service" >/dev/null <<UNIT
[Unit]
Description=bed-check: camera bed-clear detection for Klipper
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=${RUN_USER}
WorkingDirectory=${REPO_DIR}
Environment=BEDCHECK_CONFIG=${REPO_DIR}/config/config.yaml
Environment=BEDCHECK_DATA=${REPO_DIR}/data
Environment=HF_HOME=${REPO_DIR}/data/hf
ExecStart=${VENV}/bin/python -m uvicorn app.main:app --host 0.0.0.0 --port ${PORT}
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
UNIT
    ${SUDO} systemctl daemon-reload
    ${SUDO} systemctl enable "$SERVICE" >/dev/null 2>&1 || true
    ${SUDO} systemctl restart "$SERVICE"
    sleep 2
    if systemctl is-active --quiet "$SERVICE"; then
        c_ok "${SERVICE} running on port ${PORT}"
    else
        c_err "${SERVICE} failed to start:"
        ${SUDO} journalctl -u "$SERVICE" -n 30 --no-pager || true
        exit 1
    fi
fi

# ------------------------------------------------- moonraker integration
MOONRAKER_CONF=""
for candidate in "${HOME}/printer_data/config/moonraker.conf" \
                 "${HOME}/klipper_config/moonraker.conf"; do
    if [[ -f "$candidate" ]]; then MOONRAKER_CONF="$candidate"; break; fi
done

if [[ -n "$MOONRAKER_CONF" ]]; then
    step "Moonraker integration (${MOONRAKER_CONF})"
    if grep -q "update_manager ${SERVICE}" "$MOONRAKER_CONF"; then
        c_ok "update_manager entry already present"
    else
        ORIGIN="$(git -C "$REPO_DIR" remote get-url origin 2>/dev/null || true)"
        if [[ -z "$ORIGIN" ]]; then
            c_warn "no git remote found - skipping the update_manager entry."
            c_warn "Push to GitHub, then re-run this script to add it."
        else
            SNIPPET="
[update_manager ${SERVICE}]
type: git_repo
path: ${REPO_DIR}
origin: ${ORIGIN}
virtualenv: ${VENV}
requirements: requirements.txt
install_script: install.sh
managed_services: ${SERVICE}
"
            if [[ "$ASSUME_YES" == "yes" ]]; then
                reply="y"
            else
                echo "$SNIPPET"
                read -rp "Append this to moonraker.conf? [Y/n] " reply
                reply="${reply:-y}"
            fi
            if [[ "$reply" =~ ^[Yy]$ ]]; then
                printf '%s\n' "$SNIPPET" >> "$MOONRAKER_CONF"
                c_ok "added - restart Moonraker to pick it up"
            fi
        fi
    fi

    CFG_DIR="$(dirname "$MOONRAKER_CONF")"
    if [[ ! -e "${CFG_DIR}/bed_check.cfg" ]]; then
        cp "${REPO_DIR}/klipper/bed_check.cfg" "${CFG_DIR}/"
        c_ok "copied bed_check.cfg into ${CFG_DIR}"
        c_warn "add [include bed_check.cfg] to printer.cfg, then FIRMWARE_RESTART"
    else
        c_ok "bed_check.cfg already in ${CFG_DIR}"
    fi
else
    step "Moonraker integration"
    echo "No moonraker.conf on this host, so bed-check is running remotely."
    echo "Copy klipper/bed_check.cfg to the printer yourself, and allow this"
    echo "machine in Moonraker's [authorization] trusted_clients."
fi

# ------------------------------------------------------------------- done
IP="$(hostname -I 2>/dev/null | awk '{print $1}')"
step "Done"
echo "  Web UI:   http://${IP:-localhost}:${PORT}"
echo "  Logs:     ${SUDO:+sudo }journalctl -u ${SERVICE} -f"
echo "  Restart:  ${SUDO:+sudo }systemctl restart ${SERVICE}"
echo "  Arbiter:  ${ARBITER}"
if [[ "$FRESH_CONFIG" == "yes" ]]; then
    echo
    c_warn "NEXT: edit config/config.yaml (snapshot_url, moonraker_url, bed size),"
    c_warn "      ${SUDO:+sudo }systemctl restart ${SERVICE}, then calibrate in the web UI."
fi
