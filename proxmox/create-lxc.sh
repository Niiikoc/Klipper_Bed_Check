#!/usr/bin/env bash
# Create a Proxmox LXC and install bed-check inside it.
#
# Run this ON THE PROXMOX HOST (shell of the node), not inside a container:
#
#   bash -c "$(curl -fsSL https://raw.githubusercontent.com/Niiikoc/Klipper_Bed_Check/main/proxmox/create-lxc.sh)"
#
# or, to test a working copy that is already on the node:
#
#   ./proxmox/create-lxc.sh --local /root/Klipper_Bed_Check
set -Eeuo pipefail

# ---- override with BEDCHECK_REPO or --repo ---------------------------------
REPO_URL="${BEDCHECK_REPO:-https://github.com/Niiikoc/Klipper_Bed_Check.git}"
# ---------------------------------------------------------------------------

UI_LOG="/var/log/bed-check-lxc.log"

# The block below is lib/ui.sh, inlined because this script is executed
# straight from curl and has nothing to source. Do not edit it here: edit
# lib/ui.sh and re-run tools/sync-ui.py. tests/test_ui_sync.py enforces it.
# >>> lib/ui.sh >>>
# bed-check shell UI helpers: spinner, step markers, logging, error trap.
#
# Sourced by install.sh. The same block is embedded verbatim in
# proxmox/create-lxc.sh, which is executed straight from curl and therefore
# cannot source anything; tests/test_ui_sync.py fails if the copies drift.
#
# Every helper must survive `set -euo pipefail`, so no bare `[[ ]] && cmd` as
# the last statement of a function - that returns 1 and aborts the caller.

# ------------------------------------------------------------------ palette
if [[ -t 1 ]]; then
    UI_RESET=$'\033[0m'; UI_RED=$'\033[31m';  UI_GRN=$'\033[32m'
    UI_YEL=$'\033[33m';  UI_CYA=$'\033[36m';  UI_BLD=$'\033[1m'
    UI_DIM=$'\033[2m'
else
    UI_RESET=""; UI_RED=""; UI_GRN=""; UI_YEL=""; UI_CYA=""; UI_BLD=""; UI_DIM=""
fi
UI_TICK="${UI_GRN}✔${UI_RESET}"
UI_CROSS="${UI_RED}✘${UI_RESET}"
UI_ARROW="${UI_CYA}➜${UI_RESET}"
UI_BANG="${UI_YEL}▲${UI_RESET}"

# An array, not a string: ${#s} and ${s:i:1} count bytes rather than characters
# when the locale is C, which is what a fresh Proxmox node gives you.
UI_FRAMES=('⠋' '⠙' '⠹' '⠸' '⠼' '⠴' '⠦' '⠧' '⠇' '⠏')

UI_LOG="${UI_LOG:-/var/log/bed-check-install.log}"
: >"$UI_LOG" 2>/dev/null || UI_LOG="$(mktemp -t bed-check-XXXXXX.log)"

_ui_pid=""
_ui_msg=""
_ui_cmd=""
_ui_hint=""

# ------------------------------------------------------------- formatting
ui_dur() {  # seconds -> 45s | 3m07s
    local s="$1"
    if (( s < 60 )); then printf '%ds' "$s"
    else printf '%dm%02ds' $(( s / 60 )) $(( s % 60 )); fi
}

ui_size() {  # directory -> 412MB, empty when the path does not exist yet
    du -sb "$1" 2>/dev/null | awk '{
        b = $1
        if      (b < 1024)       printf "%dB",   b
        else if (b < 1048576)    printf "%dKB",  b / 1024
        else if (b < 1073741824) printf "%dMB",  b / 1048576
        else                     printf "%.1fGB", b / 1073741824
    }'
}

ui_cursor_hide() { if [[ -t 1 ]]; then printf '\033[?25l'; fi; }
ui_cursor_show() { if [[ -t 1 ]]; then printf '\033[?25h'; fi; }

# ---------------------------------------------------------------- spinner
# Redrawing with \r only makes sense on a terminal. Under `pct exec` stdout is
# a pipe, so there we print a fresh line every 15s instead - which also forces
# a flush, otherwise the pipe buffer would hold everything back until the end.
_ui_spin() {
    local watch="$1" start size="" i=0 tick=0
    start="$SECONDS"
    while :; do
        i=$(( (i + 1) % ${#UI_FRAMES[@]} ))
        if [[ -n "$watch" ]] && (( tick % 4 == 0 )); then
            size="$(ui_size "$watch")"
        fi
        tick=$(( tick + 1 ))
        printf '\r\033[2K %s %s%s  %s' \
            "${UI_CYA}${UI_FRAMES[i]}${UI_RESET}" \
            "$_ui_msg" \
            "${size:+  ${UI_BLD}${size}${UI_RESET}}" \
            "${UI_DIM}$(ui_dur $(( SECONDS - start )))${UI_RESET}"
        sleep 0.25
    done
}

_ui_ticker() {
    local watch="$1" start size=""
    start="$SECONDS"
    while :; do
        sleep 15
        if [[ -n "$watch" ]]; then size="$(ui_size "$watch")"; fi
        printf '   %s ...%s  %s\n' \
            "$_ui_msg" "${size:+  ${size}}" "$(ui_dur $(( SECONDS - start )))"
    done
}

_ui_stop() {
    if [[ -n "$_ui_pid" ]]; then
        kill "$_ui_pid" 2>/dev/null || true
        wait "$_ui_pid" 2>/dev/null || true
        _ui_pid=""
    fi
    if [[ -t 1 ]]; then printf '\r\033[2K'; fi
    ui_cursor_show
}

# msg_info "Installing torch" [directory-to-watch]
msg_info() {
    _ui_stop
    _ui_msg="$1"
    if [[ -t 1 ]]; then
        ui_cursor_hide
        _ui_spin "${2:-}" &
    else
        printf ' %s %s\n' "$UI_ARROW" "$_ui_msg"
        _ui_ticker "${2:-}" &
    fi
    _ui_pid=$!
}

# Clearing _ui_msg/_ui_cmd on success matters: it is what lets _ui_on_err tell
# the user which step died instead of quoting an unrelated earlier one.
msg_ok() {
    local label="${1:-$_ui_msg}"
    _ui_stop
    printf ' %s %s\n' "$UI_TICK" "$label"
    _ui_msg=""; _ui_cmd=""
}
msg_warn() { _ui_stop; printf ' %s %s\n' "$UI_BANG"  "$*"; }
msg_err()  { _ui_stop; printf ' %s %s\n' "$UI_CROSS" "$*" >&2; }
msg_step() {
    _ui_stop
    printf '\n%s%s%s\n' "$UI_BLD" "$*" "$UI_RESET"
    _ui_msg=""; _ui_cmd=""
}

# ------------------------------------------------------------------- log
ui_run() {   # noisy command -> log file only, remembered for the error report
    _ui_cmd="$*"
    "$@" >>"$UI_LOG" 2>&1
}

ui_dump_log() {
    local n="${1:-25}"
    if [[ -s "$UI_LOG" ]]; then
        printf '\n%s---- last %d lines of %s ----%s\n' \
            "$UI_DIM" "$n" "$UI_LOG" "$UI_RESET"
        tail -n "$n" "$UI_LOG"
        printf '%s%s%s\n' "$UI_DIM" "--------------------------------" "$UI_RESET"
    fi
}

# ----------------------------------------------------------- error trap
# ui_trap_err [name-of-function-printing-extra-cleanup-advice]
ui_trap_err() {
    # errtrace is not optional here. Without it the ERR trap is not inherited
    # by shell functions, so a command failing inside ui_run would take the
    # script down without printing anything at all.
    set -E
    _ui_hint="${1:-}"
    trap '_ui_on_err $? $LINENO "$BASH_COMMAND"' ERR
    trap '_ui_stop' EXIT
}

_ui_on_err() {
    local code="$1" line="$2" cmd="$3"
    _ui_stop
    if [[ -n "$_ui_msg" ]]; then
        msg_err "${_ui_msg}: failed with exit ${code}"
    else
        msg_err "failed at line ${line} with exit ${code}"
    fi
    printf '      command: %s\n' "${_ui_cmd:-$cmd}" >&2
    ui_dump_log 25
    if [[ -n "$_ui_hint" ]] && declare -F "$_ui_hint" >/dev/null 2>&1; then
        "$_ui_hint"
    fi
    exit "$code"
}

# ---------------------------------------------------------------- banner
ui_banner() {
    printf '%s' "$UI_CYA"
    cat <<'BANNER'
  ██████╗ ███████╗██████╗        ██████╗██╗  ██╗███████╗ ██████╗██╗  ██╗
  ██╔══██╗██╔════╝██╔══██╗      ██╔════╝██║  ██║██╔════╝██╔════╝██║ ██╔╝
  ██████╔╝█████╗  ██║  ██║█████╗██║     ███████║█████╗  ██║     █████╔╝
  ██╔══██╗██╔══╝  ██║  ██║╚════╝██║     ██╔══██║██╔══╝  ██║     ██╔═██╗
  ██████╔╝███████╗██████╔╝      ╚██████╗██║  ██║███████╗╚██████╗██║  ██╗
  ╚═════╝ ╚══════╝╚═════╝        ╚═════╝╚═╝  ╚═╝╚══════╝ ╚═════╝╚═╝  ╚═╝
BANNER
    printf '%s\n' "$UI_RESET"
}
# <<< lib/ui.sh <<<

APP="bed-check"
INSTALL_DIR="/opt/bed-check"
TEMPLATE_MATCH="debian-12-standard"
TUI_TITLE="bed-check LXC installer"

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
BUILT="no"

PRINTER_NAME=""
SNAPSHOT_URL=""
MOONRAKER_URL=""
BED_SIZE=""

usage() {
    cat <<'USAGE'
Usage: create-lxc.sh [options]        (run on the Proxmox host)

With no options it opens a whiptail menu. Pass -y to skip it entirely.

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

  -y, --yes             No prompts, no menu
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
        *) printf 'unknown option: %s\n' "$1" >&2; usage; exit 1 ;;
    esac
done

# ------------------------------------------------------------ sanity checks
if ! command -v pct >/dev/null 2>&1 || ! command -v pveam >/dev/null 2>&1; then
    msg_err "This must run on a Proxmox VE host (pct/pveam not found)."
    exit 1
fi
[[ "$(id -u)" -eq 0 ]] || { msg_err "Run as root on the Proxmox host."; exit 1; }

cleanup_hint() {
    if [[ "$BUILT" == "yes" || -z "$CTID" ]]; then return 0; fi
    if pct status "$CTID" >/dev/null 2>&1; then
        msg_warn "Container ${CTID} was left half-built. Inspect or remove it:"
        printf '      pct exec %s -- tail -50 /var/log/bed-check-install.log\n' "$CTID"
        printf '      pct stop %s ; pct destroy %s\n' "$CTID" "$CTID"
    fi
}
ui_trap_err cleanup_hint

# ------------------------------------------------------- defaults to offer
pick_storage() {  # $1 = content type
    # A dead NFS/CIFS entry in storage.cfg makes pvesm status block forever.
    # Give up rather than hang: the caller then asks for --storage explicitly.
    timeout 20 pvesm status --content "$1" 2>/dev/null \
        | awk 'NR>1 && $3=="active" {print $1; exit}' || true
}
[[ -z "$CTID"        ]] && CTID="$(pvesh get /cluster/nextid)"
[[ -z "$STORAGE"     ]] && STORAGE="$(pick_storage rootdir)"
[[ -z "$TPL_STORAGE" ]] && TPL_STORAGE="$(pick_storage vztmpl)"
[[ -z "$RAM"  ]] && { [[ "$ARBITER" == "yes" ]] && RAM=3072 || RAM=1024; }
[[ -z "$DISK" ]] && { [[ "$ARBITER" == "yes" ]] && DISK=12  || DISK=6; }

# ---------------------------------------------------------------- the menu
tui_available() {
    [[ -t 0 && -t 1 ]] && command -v whiptail >/dev/null 2>&1
}

tui_input() {   # prompt default -> value
    whiptail --title "$TUI_TITLE" --inputbox "$1" 9 70 "$2" 3>&1 1>&2 2>&3
}

tui_storage() {  # content-type default -> storage name
    local -a opts=()
    local name free
    while read -r name free; do
        opts+=("$name" "free ${free}")
    done < <(pvesm status --content "$1" 2>/dev/null |
             awk 'NR>1 && $3=="active" {printf "%s %.0fG\n", $1, $6/1048576}')
    if [[ ${#opts[@]} -le 2 ]]; then printf '%s' "$2"; return 0; fi
    whiptail --title "$TUI_TITLE" --menu "Storage for ${1}" 16 70 6 \
        "${opts[@]}" 3>&1 1>&2 2>&3
}

tui_advanced() {
    CTID="$(tui_input "Container ID" "$CTID")"           || exit 0
    HOSTNAME_="$(tui_input "Hostname" "$HOSTNAME_")"     || exit 0
    CORES="$(tui_input "CPU cores" "$CORES")"            || exit 0
    RAM="$(tui_input "Memory (MB)" "$RAM")"              || exit 0
    SWAP="$(tui_input "Swap (MB)" "$SWAP")"              || exit 0
    DISK="$(tui_input "Disk (GB)" "$DISK")"              || exit 0
    STORAGE="$(tui_storage rootdir "$STORAGE")"          || exit 0
    TPL_STORAGE="$(tui_storage vztmpl "$TPL_STORAGE")"   || exit 0
    BRIDGE="$(tui_input "Network bridge" "$BRIDGE")"     || exit 0

    if whiptail --title "$TUI_TITLE" --yesno \
        "Use DHCP?\n\nChoose No to enter a static address." 10 70; then
        IPCONF="dhcp"; GATEWAY=""
    else
        IPCONF="$(tui_input "Static address in CIDR form, e.g. 192.168.1.60/24" \
                            "192.168.1.60/24")" || exit 0
        GATEWAY="$(tui_input "Gateway" "192.168.1.1")"   || exit 0
        NAMESERVER="$(tui_input "Nameserver (empty = inherit from host)" \
                                "$NAMESERVER")" || exit 0
    fi

    PASSWORD="$(whiptail --title "$TUI_TITLE" --passwordbox \
        "root password inside the container\n\nLeave empty to use 'pct enter' only." \
        11 70 3>&1 1>&2 2>&3)" || exit 0

    if whiptail --title "$TUI_TITLE" --yesno \
        "Install the CLIP arbiter?\n\nIt double-checks flagged detections and cuts false alarms, at ~2GB disk and ~1GB RAM while it runs." 12 70; then
        ARBITER="yes"
    else
        ARBITER="no"
        [[ "$RAM"  -gt 1024 ]] && RAM=1024
        [[ "$DISK" -gt 6    ]] && DISK=6
    fi

    PORT="$(tui_input "Web UI port" "$PORT")" || exit 0

    if whiptail --title "$TUI_TITLE" --yesno \
        "Pre-fill config.yaml now?\n\nSaves editing it by hand afterwards." 10 70; then
        PRINTER_NAME="$(tui_input "Printer name" "${PRINTER_NAME:-voron}")" || exit 0
        SNAPSHOT_URL="$(tui_input "Camera snapshot URL" \
            "${SNAPSHOT_URL:-http://192.168.1.50/webcam/?action=snapshot}")" || exit 0
        MOONRAKER_URL="$(tui_input "Moonraker URL" \
            "${MOONRAKER_URL:-http://192.168.1.50:7125}")" || exit 0
        BED_SIZE="$(tui_input "Bed size in mm as X,Y" "${BED_SIZE:-350,350}")" || exit 0
    fi
}

if [[ "$ASSUME_YES" != "yes" ]] && tui_available; then
    clear
    ui_banner
    whiptail --title "$TUI_TITLE" --yesno \
        "This creates a new Debian 12 LXC on this node and installs bed-check into it.\n\nContinue?" \
        11 70 || exit 0
    MODE="$(whiptail --title "$TUI_TITLE" --menu \
        "\nHow do you want to configure it?" 13 70 2 \
        "default"  "CT ${CTID}, ${CORES} cores, ${RAM}MB, ${DISK}GB, DHCP" \
        "advanced" "Choose ID, resources, network, arbiter, config" \
        3>&1 1>&2 2>&3)" || exit 0
    if [[ "$MODE" == "advanced" ]]; then tui_advanced; fi
fi

# ------------------------------------------------- validate the final set
if [[ "$IPCONF" != "dhcp" && -z "$GATEWAY" ]]; then
    msg_err "A static --ip needs --gw as well."
    exit 1
fi
if [[ -n "$LOCAL_SRC" && ! -f "${LOCAL_SRC}/install.sh" ]]; then
    msg_err "--local ${LOCAL_SRC} does not look like the bed-check repo."
    exit 1
fi
if [[ -z "$LOCAL_SRC" && "$REPO_URL" == *CHANGEME* ]]; then
    msg_err "Set REPO_URL at the top of this script, or pass --repo / --local."
    exit 1
fi
if pct status "$CTID" >/dev/null 2>&1; then
    msg_err "CTID ${CTID} already exists."
    exit 1
fi
if [[ -z "$STORAGE" ]]; then
    msg_err "No active storage with 'rootdir' content was found."
    msg_err "If a storage is offline, pvesm status hangs - name one with --storage."
    exit 1
fi
if [[ -z "$TPL_STORAGE" ]]; then
    msg_err "No active storage with 'vztmpl' content was found."
    msg_err "Name one with --template-storage (usually 'local')."
    exit 1
fi

# ----------------------------------------------------------------- summary
SUMMARY="
  Container    ${CTID}  (${HOSTNAME_})
  Resources    ${CORES} cores, ${RAM}MB RAM, ${SWAP}MB swap, ${DISK}GB disk
  Storage      ${STORAGE}   (templates: ${TPL_STORAGE})
  Network      ${BRIDGE}, ip=${IPCONF}${GATEWAY:+, gw=${GATEWAY}}
  Type         $([[ "$UNPRIVILEGED" == "1" ]] && echo unprivileged || echo privileged)
  Arbiter      ${ARBITER}
  Source       ${LOCAL_SRC:-$REPO_URL}
  Web UI port  ${PORT}
"
if [[ "$ASSUME_YES" != "yes" ]]; then
    if tui_available; then
        whiptail --title "$TUI_TITLE" --yesno "Create it?\n${SUMMARY}" 18 70 || exit 0
        clear
        ui_banner
    else
        printf '%s\n' "$SUMMARY"
        read -rp "Create it? [Y/n] " reply
        [[ "${reply:-y}" =~ ^[Yy]$ ]] || exit 0
    fi
else
    ui_banner
fi
printf '%s\n' "$SUMMARY"

# ---------------------------------------------------------------- template
msg_step "Template"
msg_info "Refreshing the template list"
# No timeout here means a node that cannot reach download.proxmox.com sits
# here forever. The cached list is good enough to carry on with.
ui_run timeout 120 pveam update \
    || msg_warn "pveam update failed or timed out, using the cached list"
TEMPLATE="$(pveam available --section system 2>/dev/null \
            | awk -v m="$TEMPLATE_MATCH" '$2 ~ m {print $2}' | sort -V | tail -1)"
[[ -n "$TEMPLATE" ]] || { msg_err "No ${TEMPLATE_MATCH} template available."; exit 1; }
msg_ok "Using ${TEMPLATE}"

if pveam list "$TPL_STORAGE" 2>/dev/null | grep -q "$TEMPLATE"; then
    msg_ok "Template already downloaded"
else
    # pveam prints a real progress bar, so let it through instead of a spinner.
    printf ' %s Downloading the template\n' "$UI_ARROW"
    pveam download "$TPL_STORAGE" "$TEMPLATE"
    msg_ok "Template downloaded"
fi

# ----------------------------------------------------------------- create
msg_step "Container"
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

msg_info "Creating container ${CTID}"
ui_run pct create "${CREATE_ARGS[@]}"
msg_ok "Container ${CTID} created"

msg_info "Starting container"
ui_run pct start "$CTID"
msg_ok "Container started"

msg_info "Waiting for the network"
NET_OK="no"
for _ in $(seq 1 60); do
    if pct exec "$CTID" -- getent hosts deb.debian.org >/dev/null 2>&1; then
        NET_OK="yes"; break
    fi
    sleep 2
done
if [[ "$NET_OK" != "yes" ]]; then
    msg_err "No network inside the container. Check the bridge and IP settings."
    exit 1
fi
msg_ok "Network up"

# -------------------------------------------------------------- provision
msg_info "Installing base packages"
ui_run pct exec "$CTID" -- bash -c \
    "export DEBIAN_FRONTEND=noninteractive; apt-get update -qq && \
     apt-get install -y --no-install-recommends ca-certificates curl git"
msg_ok "Base packages installed"

if [[ -n "$LOCAL_SRC" ]]; then
    msg_info "Copying bed-check from ${LOCAL_SRC}"
    TARBALL="$(mktemp /tmp/bed-check-XXXXXX.tar.gz)"
    ui_run tar czf "$TARBALL" -C "$LOCAL_SRC" \
        --exclude=.git --exclude=.venv --exclude=data --exclude=__pycache__ .
    ui_run pct exec "$CTID" -- mkdir -p "$INSTALL_DIR"
    ui_run pct push "$CTID" "$TARBALL" "/tmp/bed-check.tar.gz"
    ui_run pct exec "$CTID" -- tar xzf /tmp/bed-check.tar.gz -C "$INSTALL_DIR"
    ui_run pct exec "$CTID" -- rm -f /tmp/bed-check.tar.gz
    rm -f "$TARBALL"
    msg_ok "Copied from ${LOCAL_SRC}"
else
    msg_info "Cloning ${REPO_URL}"
    ui_run pct exec "$CTID" -- git clone --depth 1 "$REPO_URL" "$INSTALL_DIR"
    msg_ok "Cloned ${REPO_URL}"
fi
ui_run pct exec "$CTID" -- chmod +x \
    "${INSTALL_DIR}/install.sh" "${INSTALL_DIR}/uninstall.sh"

# The inner installer prints its own progress. Let it through rather than
# swallowing it: with the arbiter this step downloads ~800MB and is by far the
# longest part of the run.
msg_step "Installing bed-check inside the container"
ARB_FLAG="--arbiter"; [[ "$ARBITER" == "no" ]] && ARB_FLAG="--no-arbiter"
pct exec "$CTID" -- bash -c \
    "cd ${INSTALL_DIR} && ./install.sh -y --allow-root ${ARB_FLAG} --port ${PORT}"
BUILT="yes"

# ------------------------------------------------------------ pre-fill cfg
if [[ -n "$PRINTER_NAME$SNAPSHOT_URL$MOONRAKER_URL$BED_SIZE" ]]; then
    msg_step "Configuration"
    msg_info "Pre-filling config.yaml"
    CFG="${INSTALL_DIR}/config/config.yaml"
    [[ -n "$PRINTER_NAME" ]] && ui_run pct exec "$CTID" -- \
        sed -i "s|^\( *- name:\).*|\1 ${PRINTER_NAME}|" "$CFG"
    [[ -n "$SNAPSHOT_URL" ]] && ui_run pct exec "$CTID" -- \
        sed -i "s|^\( *snapshot_url:\).*|\1 ${SNAPSHOT_URL}|" "$CFG"
    [[ -n "$MOONRAKER_URL" ]] && ui_run pct exec "$CTID" -- \
        sed -i "s|^\( *moonraker_url:\).*|\1 ${MOONRAKER_URL}|" "$CFG"
    if [[ -n "$BED_SIZE" ]]; then
        BX="${BED_SIZE%%,*}"; BY="${BED_SIZE##*,}"
        ui_run pct exec "$CTID" -- sed -i \
            "s|^\( *bed_size_mm:\).*|\1 [${BX// /}, ${BY// /}]|" "$CFG"
    fi
    ui_run pct exec "$CTID" -- systemctl restart "$APP"
    msg_ok "config.yaml written"
fi

# ------------------------------------------------------------------ result
sleep 2
CT_IP="$(pct exec "$CTID" -- hostname -I 2>/dev/null | awk '{print $1}')"
HEALTH="$(pct exec "$CTID" -- curl -fsS "http://127.0.0.1:${PORT}/api/health" 2>/dev/null || echo '')"

msg_step "Done"
echo "  Container   ${CTID} (${HOSTNAME_})"
echo "  Web UI      http://${CT_IP}:${PORT}"
echo "  Shell       pct enter ${CTID}"
echo "  Logs        pct exec ${CTID} -- journalctl -u ${APP} -f"
echo "  Config      ${INSTALL_DIR}/config/config.yaml"
echo "  Build log   ${UI_LOG}"
if [[ -n "$HEALTH" ]]; then
    msg_ok "Service is answering: ${HEALTH}"
else
    msg_warn "Service did not answer /api/health yet - check the logs."
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
