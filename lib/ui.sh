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
