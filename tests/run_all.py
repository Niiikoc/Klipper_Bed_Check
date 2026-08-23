"""Run every bed-check test module and report a single verdict."""
import os
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
MODULES = ["test_pipeline.py", "test_service.py", "test_arbiter.py"]

failed = []
for mod in MODULES:
    print(f"\n=== {mod} " + "=" * (60 - len(mod)))
    rc = subprocess.call([sys.executable, os.path.join(HERE, mod)])
    if rc:
        failed.append(mod)

print("\n" + "=" * 66)
print("FAILED MODULES:", ", ".join(failed) if failed else "none")
sys.exit(1 if failed else 0)
