#!/usr/bin/env bash
# bed-check installer.
#
# Safe to re-run: Moonraker's update_manager calls this after every update, so
# it must never clobber an existing config or reinstall what is already there.
set -Eeuo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SERVICE="bed-check"
VENV="${REPO_DIR}/.venv"
PYTHON="${VENV}/bin/python"
PORT="${BEDCHECK_PORT:-8790}"
RUN_USER="${SUDO_USER:-$(id -un)}"
CLIP_MODEL="openai/clip-vit-base-patch32"
HF_DIR="${REPO_DIR}/data/hf"

ARBITER=""            # yes | no | "" = decide below
SETUP_SERVICE="yes"
ASSUME_YES="no"
FRESH_CONFIG="no"
ALLOW_ROOT="no"
SUDO="sudo"

if [[ ! -f "${REPO_DIR}/lib/ui.sh" ]]; then
    echo "lib/ui.sh is missing - this is not a complete bed-check checkout." >&2
    exit 1
fi
# shellcheck source=lib/ui.sh
source "${REPO_DIR}/lib/ui.sh"
ui_trap_err

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
        *) msg_err "unknown option: $1"; usage; exit 1 ;;
    esac
done

if [[ "$(id -u)" -eq 0 ]]; then
    # Already root: sudo is unnecessary and often absent in a container image.
    SUDO=""
    if [[ -z "${SUDO_USER:-}" && "$ALLOW_ROOT" != "yes" ]]; then
        msg_err "Run this as your normal user (e.g. pi), not as root."
        msg_err "It calls sudo itself where it needs to."
        msg_err "Inside an LXC or container, pass --allow-root instead."
        exit 1
    fi
elif ! command -v sudo >/dev/null 2>&1; then
    msg_err "sudo is not installed and you are not root."
    exit 1
fi

# ---------------------------------------------------------------- packages
msg_step "System packages"
MISSING=()
for pkg in python3-venv python3-dev libglib2.0-0; do
    dpkg -s "$pkg" >/dev/null 2>&1 || MISSING+=("$pkg")
done
if [[ ${#MISSING[@]} -gt 0 ]]; then
    msg_info "Installing ${MISSING[*]}"
    ui_run ${SUDO} apt-get update -qq
    ui_run ${SUDO} apt-get install -y --no-install-recommends "${MISSING[@]}"
    msg_ok "Installed ${MISSING[*]}"
else
    msg_ok "Already present"
fi

# ----------------------------------------------------------- arbiter choice
if [[ -z "$ARBITER" ]]; then
    if [[ -x "$PYTHON" ]] && "$PYTHON" -c "import torch" >/dev/null 2>&1; then
        ARBITER="yes"                      # keep what a previous run set up
        msg_ok "Arbiter already installed, keeping it"
    elif [[ "$ASSUME_YES" == "yes" || ! -t 0 ]]; then
        ARBITER="no"
    else
        RAM_MB="$(awk '/MemTotal/ {print int($2/1024)}' /proc/meminfo)"
        echo
        echo "The CLIP arbiter double-checks flagged detections semantically and"
        echo "cuts false alarms. It costs ~2GB disk and ~1GB RAM while running."
        echo "This machine has ${RAM_MB}MB RAM."
        if [[ "$RAM_MB" -lt 2048 ]]; then
            msg_warn "Below 2GB: classical CV only is recommended here."
        fi
        read -rp "Install the CLIP arbiter? [y/N] " reply
        if [[ "$reply" =~ ^[Yy]$ ]]; then ARBITER="yes"; else ARBITER="no"; fi
    fi
fi

# ------------------------------------------------------------------- venv
msg_step "Python environment"
if [[ ! -x "$PYTHON" ]]; then
    msg_info "Creating virtualenv"
    ui_run python3 -m venv "$VENV"
    msg_ok "Created ${VENV}"
fi
msg_info "Upgrading pip" "$VENV"
ui_run "$PYTHON" -m pip install --upgrade pip
msg_ok "pip up to date"

msg_info "Installing core dependencies (numpy, opencv, fastapi)" "$VENV"
ui_run "$PYTHON" -m pip install -r "${REPO_DIR}/requirements.txt"
msg_ok "Core dependencies installed"

if [[ "$ARBITER" == "yes" ]]; then
    msg_step "CLIP arbiter"
    if ! "$PYTHON" -c "import torch" >/dev/null 2>&1; then
        # The pytorch CPU wheel index only carries x86_64; on arm64 the plain
        # PyPI wheels are already CPU-only.
        msg_info "Installing torch, CPU build (~200MB download)" "$VENV"
        if [[ "$(uname -m)" == "x86_64" ]]; then
            ui_run "$PYTHON" -m pip install \
                --index-url https://download.pytorch.org/whl/cpu torch
        else
            ui_run "$PYTHON" -m pip install torch
        fi
        msg_ok "torch installed"
    else
        msg_ok "torch already installed"
    fi

    msg_info "Installing transformers" "$VENV"
    ui_run "$PYTHON" -m pip install -r "${REPO_DIR}/requirements-arbiter.txt"
    msg_ok "transformers installed"

    mkdir -p "$HF_DIR"
    msg_info "Fetching CLIP weights (~600MB, once)" "$HF_DIR"
    ui_run env "HF_HOME=${HF_DIR}" "$PYTHON" -c "
from transformers import CLIPModel, CLIPProcessor
CLIPModel.from_pretrained('${CLIP_MODEL}')
CLIPProcessor.from_pretrained('${CLIP_MODEL}')
"
    msg_ok "CLIP weights ready ($(ui_size "$HF_DIR"))"
fi

# ----------------------------------------------------------------- config
msg_step "Configuration"
mkdir -p "${REPO_DIR}/config" "${REPO_DIR}/data"
if [[ -f "${REPO_DIR}/config/config.yaml" ]]; then
    msg_ok "config/config.yaml exists, left untouched"
else
    cp "${REPO_DIR}/config/config.example.yaml" "${REPO_DIR}/config/config.yaml"
    FRESH_CONFIG="yes"
    msg_warn "created config/config.yaml from the example - you must edit it"
fi

if [[ "$ARBITER" == "no" && "$FRESH_CONFIG" == "yes" ]]; then
    "$PYTHON" -c "
import io, re
p = '${REPO_DIR}/config/config.yaml'
s = io.open(p, encoding='utf-8').read()
s = re.sub(r'(^arbiter:\s*\n(?:[ \t].*\n|\n)*?[ \t]+enabled:[ \t]*)true',
           r'\1false', s, count=1, flags=re.M)
io.open(p, 'w', encoding='utf-8', newline='\n').write(s)
" || msg_warn "could not auto-disable the arbiter in config.yaml; set arbiter.enabled: false yourself"
fi

# ---------------------------------------------------------------- service
if [[ "$SETUP_SERVICE" == "yes" ]]; then
    msg_step "systemd service"
    msg_info "Writing ${SERVICE}.service"
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
Environment=HF_HOME=${HF_DIR}
ExecStart=${VENV}/bin/python -m uvicorn app.main:app --host 0.0.0.0 --port ${PORT}
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
UNIT
    ui_run ${SUDO} systemctl daemon-reload
    ${SUDO} systemctl enable "$SERVICE" >/dev/null 2>&1 || true
    ui_run ${SUDO} systemctl restart "$SERVICE"
    sleep 2
    if systemctl is-active --quiet "$SERVICE"; then
        msg_ok "${SERVICE} running on port ${PORT}"
    else
        msg_err "${SERVICE} failed to start:"
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
    msg_step "Moonraker integration (${MOONRAKER_CONF})"
    if grep -q "update_manager ${SERVICE}" "$MOONRAKER_CONF"; then
        msg_ok "update_manager entry already present"
    else
        ORIGIN="$(git -C "$REPO_DIR" remote get-url origin 2>/dev/null || true)"
        if [[ -z "$ORIGIN" ]]; then
            msg_warn "no git remote found - skipping the update_manager entry."
            msg_warn "Push to GitHub, then re-run this script to add it."
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
                msg_ok "added - restart Moonraker to pick it up"
            fi
        fi
    fi

    CFG_DIR="$(dirname "$MOONRAKER_CONF")"
    if [[ ! -e "${CFG_DIR}/bed_check.cfg" ]]; then
        cp "${REPO_DIR}/klipper/bed_check.cfg" "${CFG_DIR}/"
        msg_ok "copied bed_check.cfg into ${CFG_DIR}"
        msg_warn "add [include bed_check.cfg] to printer.cfg, then FIRMWARE_RESTART"
    else
        msg_ok "bed_check.cfg already in ${CFG_DIR}"
    fi
else
    msg_step "Moonraker integration"
    echo "No moonraker.conf on this host, so bed-check is running remotely."
    echo "Copy klipper/bed_check.cfg to the printer yourself, and allow this"
    echo "machine in Moonraker's [authorization] trusted_clients."
fi

# ------------------------------------------------------------------- done
IP="$(hostname -I 2>/dev/null | awk '{print $1}')"
msg_step "Done"
echo "  Web UI:   http://${IP:-localhost}:${PORT}"
echo "  Logs:     ${SUDO:+sudo }journalctl -u ${SERVICE} -f"
echo "  Restart:  ${SUDO:+sudo }systemctl restart ${SERVICE}"
echo "  Arbiter:  ${ARBITER}"
echo "  Install log: ${UI_LOG}"
if [[ "$FRESH_CONFIG" == "yes" ]]; then
    echo
    msg_warn "NEXT: edit config/config.yaml (snapshot_url, moonraker_url, bed size),"
    msg_warn "      ${SUDO:+sudo }systemctl restart ${SERVICE}, then calibrate in the web UI."
fi
