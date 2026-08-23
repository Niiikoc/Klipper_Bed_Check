"""Static checks on the single-page web UI.

A syntax error in the inline <script> is invisible from the server: FastAPI
still serves the page with 200, the browser downloads it, and then silently
runs nothing at all. The symptom is a blank UI with no printer and no API
calls in the log, which looks exactly like a backend fault. Catch it here.
"""
import io
import os
import re
import shutil
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
INDEX = os.path.join(ROOT, "app", "static", "index.html")


def check(cond, label, detail=""):
    print(f"  [{'ok ' if cond else 'FAIL'}] {label}" + (f"  {detail}" if detail else ""))
    return 0 if cond else 1


def script_of(html):
    m = re.search(r"<script>(.*?)</script>", html, re.S)
    if not m:
        raise SystemExit("no inline <script> block in index.html")
    return m.group(1)


def main():
    failures = 0
    html = io.open(INDEX, encoding="utf-8").read()
    js = script_of(html)

    node = shutil.which("node")
    if node:
        tmp = os.path.join(tempfile.mkdtemp(prefix="bedcheck-ui-"), "ui.js")
        io.open(tmp, "w", encoding="utf-8", newline="\n").write(js)
        out = subprocess.run([node, "--check", tmp], capture_output=True,
                             text=True, encoding="utf-8", errors="replace")
        failures += check(out.returncode == 0, "inline script parses (node --check)",
                          (out.stderr or "").strip().splitlines()[0] if out.returncode else "")
    else:
        # No JS engine here, so fall back to the failure that actually bit:
        # a string literal broken across a newline by a mangled escape.
        broken = [i for i, line in enumerate(js.splitlines(), 1)
                  if line.count('"') % 2 and not line.lstrip().startswith("//")]
        failures += check(not broken, "no unterminated double-quoted strings",
                          f"lines {broken}" if broken else "node not installed, "
                          "used the fallback check")

    # Every element the script reaches for must exist in the markup, or the
    # first call throws on null and the rest of init() never runs.
    declared = set(re.findall(r'\bid="([^"]+)"', html))
    used = set(re.findall(r'\$\("([^"]+)"\)', js))
    missing = sorted(used - declared)
    failures += check(not missing, f"all {len(used)} $(id) lookups resolve",
                      f"missing: {missing}" if missing else "")

    # The controls added for camera orientation have to be wired both ways.
    for element in ("rotate", "flip", "btnSaveOrient"):
        failures += check(element in declared and element in used,
                          f"orientation control '{element}' is present and used")

    failures += check("cfg.capture" in js,
                      "loadConfig reads capture settings from the API")

    print("\nFAILURES:", failures)
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
