#!/usr/bin/env bash
# Remove the bed-check service. Your config, reference model and the repo
# itself are kept unless you pass --purge.
set -euo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SERVICE="bed-check"
PURGE="no"
[[ "${1:-}" == "--purge" ]] && PURGE="yes"
if [[ "$(id -u)" -eq 0 ]]; then SUDO=""; else SUDO="sudo"; fi

if systemctl list-unit-files 2>/dev/null | grep -q "^${SERVICE}.service"; then
    ${SUDO} systemctl stop "$SERVICE" || true
    ${SUDO} systemctl disable "$SERVICE" || true
    ${SUDO} rm -f "/etc/systemd/system/${SERVICE}.service"
    ${SUDO} systemctl daemon-reload
    echo "service removed"
else
    echo "service not installed"
fi

for candidate in "${HOME}/printer_data/config/moonraker.conf" \
                 "${HOME}/klipper_config/moonraker.conf"; do
    if [[ -f "$candidate" ]] && grep -q "update_manager ${SERVICE}" "$candidate"; then
        echo "NOTE: remove the [update_manager ${SERVICE}] block from ${candidate}"
    fi
done

echo "NOTE: [include bed_check.cfg] in printer.cfg is left in place."

if [[ "$PURGE" == "yes" ]]; then
    rm -rf "${REPO_DIR}/.venv" "${REPO_DIR}/data" "${REPO_DIR}/config/config.yaml"
    echo "venv, data/ and config/config.yaml removed"
else
    echo "Kept: .venv, data/, config/config.yaml  (use --purge to delete)"
fi
