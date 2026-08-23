#!/usr/bin/env bash
# Create a Proxmox LXC and install bed-check inside it.
#
# Run this ON THE PROXMOX HOST (shell of the node), not inside a container:
#
#   bash -c "$(curl -fsSL https://raw.githubusercontent.com/Niiikoc/Klipper_Bed_Check/main/proxmox/create-lxc.sh)"
#
# or, to test a working copy that is already on the node:
#
#   ./proxmox/create-lxc.sh --local /root/bed-check
set -euo pipefail

# ---- override with BEDCHECK_REPO or --repo ---------------------------------
REPO_URL="${BEDCHECK_REPO:-https://github.com/Niiikoc/Klipper_Bed_Check.git}"
# ---------------------------------------------------------------------------

APP="bed-check"
INSTALL_DIR="/opt/bed-check"
TEMPLATE_MATCH="debian-12-standard"

CTID=""
HOSTNAME_="bed-check"
STORAGE=""
TPL_STORAGE=""
CORES="2"
RAM=""
SWAP="512"
DISK=""
BRIDGE="vmbr0"
IPCONF="dhcp"
GATEWAY=""
NAMESERVER=""
PASSWORD=""
UNPRIVILEGED="1"
ARBITER="yes"
PORT="8790"
LOCAL_SRC=""
ASSUME_YES="no"

PRINTER_NAME=""
SNAPSHOT_URL=""
MOONRAKER_URL=""
BED_SIZE=""

c_ok()   { printf '\033[32m%s\033[0m\n' "$*"; }
c_warn() { printf '\033[33m%s\033[0m\n' "$*"; }
c_err()  { printf '\033[31m%s\033[0m\n' "$*" >&2; }
step()   { printf '\n\033[1m==> %s\033[0m\n' "$*"; }

usage() {
    cat <<'USAGE'
Usage: create-lxc.sh [options]        (run on the Proxmox host)

Container
  --ctid N              Container ID (default: next free)
  --hostname NAME       Hostname (default: bed-check)
  --storage NAME        Storage for the rootfs (default: first with rootdir)
  --template-storage N  Storage holding templates (default: first with vztmpl)
  --cores N             vCPUs (default 2)
  --ram MB              Memory (default 3072 with arbiter, 1024 without)
  --swap MB             Swap (default 512)
  --disk GB             Rootfs size (default 12 with arbiter, 6 without)
  --bridge NAME         Network bridge (default vmbr0)
  --ip CIDR|dhcp        e.g. 192.168.1.60/24 (default dhcp)
  --gw IP               Gateway, required with a static --ip
  --dns IP              Nameserver (default: inherit from the host)
  --password PASS       root password inside the CT (default: none, use pct enter)
  --privileged          Create a privileged container (default: unprivileged)

Application
  --arbiter             Install the CLIP arbiter (default)
  --no-arbiter          Classical CV only: smaller container, less RAM
  --port N              Web UI port (default 8790)
  --repo URL            Git repo to clone
  --local PATH          Install from a directory on this node instead of git

Pre-fill the config (optional, saves editing config.yaml afterwards)
  --printer-name NAME   e.g. voron
  --snapshot-url URL    e.g. http://192.168.1.50/webcam/?action=snapshot
  --moonraker-url URL   e.g. http://192.168.1.50:7125
  --bed-size "X,Y"      e.g. "350,350"

  -y, --yes             No prompts
  -h, --help            This text
USAGE
}

while [[ $# -gt 0 ]]; do
    case "$1" in
        --ctid) CTID="$2"; shift 2 ;;
        --hostname) HOSTNAME_="$2"; shift 2 ;;
        --storage) STORAGE="$2"; shift 2 ;;
        --template-storage) TPL_STORAGE="$2"; shift 2 ;;
        --cores) CORES="$2"; shift 2 ;;
        --ram) RAM="$2"; shift 2 ;;
        --swap) SWAP="$2"; shift 2 ;;
        --disk) DISK="$2"; shift 2 ;;
        --bridge) BRIDGE="$2"; shift 2 ;;
        --ip) IPCONF="$2"; shift 2 ;;
        --gw) GATEWAY="$2"; shift 2 ;;
        --dns) NAMESERVER="$2"; shift 2 ;;
        --password) PASSWORD="$2"; shift 2 ;;
        --privileged) UNPRIVILEGED="0"; shift ;;
        --arbiter) ARBITER="yes"; shift ;;
        --no-arbiter) ARBITER="no"; shift ;;
        --port) PORT="$2"; shift 2 ;;
        --repo) REPO_URL="$2"; shift 2 ;;
        --local) LOCAL_SRC="$2"; shift 2 ;;
        --printer-name) PRINTER_NAME="$2"; shift 2 ;;
        --snapshot-url) SNAPSHOT_URL="$2"; shift 2 ;;
        --moonraker-url) MOONRAKER_URL="$2"; shift 2 ;;
        --bed-size) BED_SIZE="$2"; shift 2 ;;
        -y|--yes) ASSUME_YES="yes"; shift ;;
        -h|--help) usage; exit 0 ;;
        *) c_err "unknown option: $1"; usage; exit 1 ;;
    esac
done

# ------------------------------------------------------------ sanity checks
if ! command -v pct >/dev/null 2>&1 || ! command -v pveam >/dev/null 2>&1; then
    c_err "This must run on a Proxmox VE host (pct/pveam not found)."
    exit 1
fi
[[ "$(id -u)" -eq 0 ]] || { c_err "Run as root on the Proxmox host."; exit 1; }

if [[ "$IPCONF" != "dhcp" && -z "$GATEWAY" ]]; then
    c_err "A static --ip needs --gw as well."
    exit 1
fi
if [[ -n "$LOCAL_SRC" && ! -f "${LOCAL_SRC}/install.sh" ]]; then
    c_err "--local ${LOCAL_SRC} does not look like the bed-check repo."
    exit 1
fi
if [[ -z "$LOCAL_SRC" && "$REPO_URL" == *CHANGEME* ]]; then
    c_err "Set REPO_URL at the top of this script, or pass --repo / --local."
    exit 1
fi

# defaults that depend on the arbiter choice
[[ -z "$RAM"  ]] && { [[ "$ARBITER" == "yes" ]] && RAM=3072  || RAM=1024; }
[[ -z "$DISK" ]] && { [[ "$ARBITER" == "yes" ]] && DISK=12   || DISK=6; }

# ------------------------------------------------------------- id & storage
[[ -z "$CTID" ]] && CTID="$(pvesh get /cluster/nextid)"
if pct status "$CTID" >/dev/null 2>&1; then
    c_err "CTID ${CTID} already exists."
    exit 1
fi

pick_storage() {  # $1 = content type
    pvesm status --content "$1" 2>/dev/null | awk 'NR>1 && $3=="active" {print $1; exit}'
}
[[ -z "$STORAGE"     ]] && STORAGE="$(pick_storage rootdir)"
[[ -z "$TPL_STORAGE" ]] && TPL_STORAGE="$(pick_storage vztmpl)"
[[ -n "$STORAGE"     ]] || { c_err "No active storage with 'rootdir' content."; exit 1; }
[[ -n "$TPL_STORAGE" ]] || { c_err "No active storage with 'vztmpl' content."; exit 1; }

# ----------------------------------------------------------------- summary
cat <<SUMMARY

  Container    ${CTID}  (${HOSTNAME_})
  Resources    ${CORES} cores, ${RAM}MB RAM, ${SWAP}MB swap, ${DISK}GB disk
  Storage      ${STORAGE}   (templates: ${TPL_STORAGE})
  Network      ${BRIDGE}, ip=${IPCONF}${GATEWAY:+, gw=${GATEWAY}}
  Type         $([[ "$UNPRIVILEGED" == "1" ]] && echo unprivileged || echo privileged)
  Arbiter      ${ARBITER}
  Source       ${LOCAL_SRC:-$REPO_URL}
  Web UI port  ${PORT}

SUMMARY
if [[ "$ASSUME_YES" != "yes" ]]; then
    read -rp "Create it? [Y/n] " reply
    [[ "${reply:-y}" =~ ^[Yy]$ ]] || exit 0
fi

# ---------------------------------------------------------------- template
step "Template"
pveam update >/dev/null 2>&1 || c_warn "pveam update failed, using the cached list"
TEMPLATE="$(pveam available --section system 2>/dev/null \
            | awk -v m="$TEMPLATE_MATCH" '$2 ~ m {print $2}' | sort -V | tail -1)"
[[ -n "$TEMPLATE" ]] || { c_err "No ${TEMPLATE_MATCH} template available."; exit 1; }

if pveam list "$TPL_STORAGE" 2>/dev/null | grep -q "$TEMPLATE"; then
    c_ok "${TEMPLATE} already downloaded"
else
    echo "Downloading ${TEMPLATE} ..."
    pveam download "$TPL_STORAGE" "$TEMPLATE"
fi

# ----------------------------------------------------------------- create
step "Creating container ${CTID}"
NET="name=eth0,bridge=${BRIDGE},ip=${IPCONF}"
[[ -n "$GATEWAY" ]] && NET="${NET},gw=${GATEWAY}"

CREATE_ARGS=(
    "$CTID" "${TPL_STORAGE}:vztmpl/${TEMPLATE}"
    --hostname "$HOSTNAME_"
    --cores "$CORES" --memory "$RAM" --swap "$SWAP"
    --rootfs "${STORAGE}:${DISK}"
    --net0 "$NET"
    --unprivileged "$UNPRIVILEGED"
    --features nesting=1
    --onboot 1
    --ostype debian
    --description "bed-check - camera bed-clear detection for Klipper"
)
[[ -n "$NAMESERVER" ]] && CREATE_ARGS+=(--nameserver "$NAMESERVER")
[[ -n "$PASSWORD"   ]] && CREATE_ARGS+=(--password "$PASSWORD")

cleanup_on_fail() {
    c_err "Setup failed. Remove the half-built container with:"
    c_err "    pct stop ${CTID} ; pct destroy ${CTID}"
}
trap cleanup_on_fail ERR

pct create "${CREATE_ARGS[@]}"
c_ok "created"

pct start "$CTID"
echo -n "Waiting for network "
for _ in $(seq 1 60); do
    if pct exec "$CTID" -- getent hosts deb.debian.org >/dev/null 2>&1; then
        echo; c_ok "network up"; break
    fi
    echo -n "."
    sleep 2
done
pct exec "$CTID" -- getent hosts deb.debian.org >/dev/null 2>&1 \
    || { echo; c_err "No network inside the container. Check bridge/IP settings."; exit 1; }

# -------------------------------------------------------------- provision
step "Base packages"
pct exec "$CTID" -- bash -c \
    "export DEBIAN_FRONTEND=noninteractive; apt-get update -qq && \
     apt-get install -y --no-install-recommends ca-certificates curl git >/dev/null"
c_ok "done"

step "Fetching bed-check"
if [[ -n "$LOCAL_SRC" ]]; then
    TARBALL="$(mktemp /tmp/bed-check-XXXXXX.tar.gz)"
    tar czf "$TARBALL" -C "$LOCAL_SRC" \
        --exclude=.git --exclude=.venv --exclude=data --exclude=__pycache__ .
    pct exec "$CTID" -- mkdir -p "$INSTALL_DIR"
    pct push "$CTID" "$TARBALL" "/tmp/bed-check.tar.gz"
    pct exec "$CTID" -- tar xzf /tmp/bed-check.tar.gz -C "$INSTALL_DIR"
    pct exec "$CTID" -- rm -f /tmp/bed-check.tar.gz
    rm -f "$TARBALL"
    c_ok "copied from ${LOCAL_SRC}"
else
    pct exec "$CTID" -- git clone --depth 1 "$REPO_URL" "$INSTALL_DIR"
    c_ok "cloned ${REPO_URL}"
fi
pct exec "$CTID" -- chmod +x "${INSTALL_DIR}/install.sh" "${INSTALL_DIR}/uninstall.sh"

step "Installing (this takes a while with the arbiter)"
ARB_FLAG="--arbiter"; [[ "$ARBITER" == "no" ]] && ARB_FLAG="--no-arbiter"
pct exec "$CTID" -- bash -c \
    "cd ${INSTALL_DIR} && ./install.sh -y --allow-root ${ARB_FLAG} --port ${PORT}"

# ------------------------------------------------------------ pre-fill cfg
if [[ -n "$PRINTER_NAME$SNAPSHOT_URL$MOONRAKER_URL$BED_SIZE" ]]; then
    step "Pre-filling config.yaml"
    CFG="${INSTALL_DIR}/config/config.yaml"
    [[ -n "$PRINTER_NAME" ]] && pct exec "$CTID" -- \
        sed -i "s|^\( *- name:\).*|\1 ${PRINTER_NAME}|" "$CFG"
    [[ -n "$SNAPSHOT_URL" ]] && pct exec "$CTID" -- \
        sed -i "s|^\( *snapshot_url:\).*|\1 ${SNAPSHOT_URL}|" "$CFG"
    [[ -n "$MOONRAKER_URL" ]] && pct exec "$CTID" -- \
        sed -i "s|^\( *moonraker_url:\).*|\1 ${MOONRAKER_URL}|" "$CFG"
    if [[ -n "$BED_SIZE" ]]; then
        BX="${BED_SIZE%%,*}"; BY="${BED_SIZE##*,}"
        pct exec "$CTID" -- sed -i \
            "s|^\( *bed_size_mm:\).*|\1 [${BX// /}, ${BY// /}]|" "$CFG"
    fi
    pct exec "$CTID" -- systemctl restart "$APP"
    c_ok "written"
fi

trap - ERR

# ------------------------------------------------------------------ result
sleep 2
CT_IP="$(pct exec "$CTID" -- hostname -I 2>/dev/null | awk '{print $1}')"
HEALTH="$(pct exec "$CTID" -- curl -fsS "http://127.0.0.1:${PORT}/api/health" 2>/dev/null || echo '')"

step "Done"
echo "  Container   ${CTID} (${HOSTNAME_})"
echo "  Web UI      http://${CT_IP}:${PORT}"
echo "  Shell       pct enter ${CTID}"
echo "  Logs        pct exec ${CTID} -- journalctl -u ${APP} -f"
echo "  Config      ${INSTALL_DIR}/config/config.yaml"
if [[ -n "$HEALTH" ]]; then
    c_ok "  Service is answering: ${HEALTH}"
else
    c_warn "  Service did not answer /api/health yet - check the logs."
fi
cat <<NEXT

NEXT STEPS
  1. Allow this container in Moonraker's [authorization] trusted_clients:
         trusted_clients:
             ${CT_IP}/32
  2. Copy klipper/bed_check.cfg to the printer and add
     [include bed_check.cfg] to printer.cfg, then FIRMWARE_RESTART.
  3. Open the web UI, calibrate the bed corners, capture a reference
     with an empty bed, and run "tune".
NEXT
