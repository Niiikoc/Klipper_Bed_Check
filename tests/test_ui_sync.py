"""Guard the two copies of the shell UI helpers.

install.sh sources lib/ui.sh. proxmox/create-lxc.sh cannot - it is executed
straight from `curl` - so it carries an inlined copy between markers. If the
two drift, the Proxmox installer silently stops matching the one people see
inside the container. tools/sync-ui.py repairs it.
"""
import io
import os
import shutil
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(ROOT, "tools"))

LIB = os.path.join(ROOT, "lib", "ui.sh")
LXC = os.path.join(ROOT, "proxmox", "create-lxc.sh")
INSTALL = os.path.join(ROOT, "install.sh")

BEGIN = "# >>> lib/ui.sh >>>"
END = "# <<< lib/ui.sh <<<"


def read(path):
    return io.open(path, encoding="utf-8").read()


def inlined_block(text):
    lines = text.splitlines()
    return lines[lines.index(BEGIN) + 1:lines.index(END)]


def check(cond, label):
    print(f"  [{'ok ' if cond else 'FAIL'}] {label}")
    return 0 if cond else 1


def main():
    failures = 0

    lxc = read(LXC)
    failures += check(BEGIN in lxc and END in lxc,
                      "create-lxc.sh carries the ui.sh markers")
    if failures:
        return 1

    failures += check(inlined_block(lxc) == read(LIB).splitlines(),
                      "inlined block matches lib/ui.sh")

    failures += check("source \"${REPO_DIR}/lib/ui.sh\"" in read(INSTALL),
                      "install.sh sources lib/ui.sh")

    # The helpers are useless if they cannot survive the flags every script
    # sets, so run them for real rather than only diffing text.
    bash = shutil.which("bash")
    if not bash:
        print("  [skip] bash not on PATH, not exercising the helpers")
        print("\nFAILURES:", failures)
        return 1 if failures else 0

    for path in (LIB, LXC, INSTALL):
        rc = subprocess.call([bash, "-n", path])
        failures += check(rc == 0, f"{os.path.relpath(path, ROOT)} parses")

    script = f"""
set -euo pipefail
UI_LOG="$(mktemp)"
source {LIB!r}
msg_step "step"
msg_info "working"
sleep 0.4
msg_ok "done"
msg_warn "careful"
ui_run true
echo "survived:$(ui_dur 125)"
"""
    # The helpers emit UTF-8 spinner frames; on Windows the default console
    # codec is cp125x and would blow up decoding them.
    out = subprocess.run([bash, "-c", script], capture_output=True, text=True,
                         encoding="utf-8", errors="replace")
    failures += check(out.returncode == 0,
                      f"helpers run clean under set -euo pipefail (rc={out.returncode})")
    failures += check("survived:2m05s" in out.stdout,
                      "ui_dur formats 125s as 2m05s")
    if out.returncode:
        print(out.stderr.strip())

    print("\nFAILURES:", failures)
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
