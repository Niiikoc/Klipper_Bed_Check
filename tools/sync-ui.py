#!/usr/bin/env python3
"""Copy lib/ui.sh into the marked block of proxmox/create-lxc.sh.

create-lxc.sh is executed straight from `curl`, so it cannot source lib/ui.sh
the way install.sh does - it has to carry its own copy. Run this after editing
lib/ui.sh; tests/test_ui_sync.py fails if you forget.
"""
import io
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
LIB = ROOT / "lib" / "ui.sh"
TARGET = ROOT / "proxmox" / "create-lxc.sh"

BEGIN = "# >>> lib/ui.sh >>>"
END = "# <<< lib/ui.sh <<<"


def splice(target_text: str, lib_text: str) -> str:
    lines = target_text.splitlines()
    try:
        i = lines.index(BEGIN)
        j = lines.index(END)
    except ValueError:
        raise SystemExit(f"{TARGET}: missing {BEGIN} / {END} markers")
    if j < i:
        raise SystemExit(f"{TARGET}: {END} comes before {BEGIN}")
    body = lib_text.splitlines()
    return "\n".join(lines[: i + 1] + body + lines[j:]) + "\n"


def main() -> int:
    lib_text = io.open(LIB, encoding="utf-8").read()
    target_text = io.open(TARGET, encoding="utf-8").read()
    merged = splice(target_text, lib_text)
    if merged == target_text:
        print("already in sync")
        return 0
    io.open(TARGET, "w", encoding="utf-8", newline="\n").write(merged)
    print(f"updated {TARGET.relative_to(ROOT)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
