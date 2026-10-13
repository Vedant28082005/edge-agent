"""Checks the build-time clock floor in esp32-core/scripts/fw_build_ts.py (no PlatformIO needed)."""
import runpy
import sys
import time
from pathlib import Path

script = Path(__file__).resolve().parents[1] / "esp32-core" / "scripts" / "fw_build_ts.py"
mod = runpy.run_path(str(script), init_globals={"Import": lambda *_: (_ for _ in ()).throw(NameError("no scons"))})
build_timestamp = mod["build_timestamp"]
assert mod["FLOOR_TS"] == 1790812800

real = time.time
failures = 0
try:
    for fake, should_pass in [(1790812799, False), (0, False), (1790812800, True), (real(), True)]:
        time.time = lambda f=fake: f
        try:
            ts = build_timestamp()
            ok = should_pass and ts == int(fake)
        except SystemExit as e:
            ok = (not should_pass) and "2026-10-01" in str(e)
        print(("ok   " if ok else "FAIL ") + f"clock={int(fake)} expected {'build' if should_pass else 'abort'}")
        failures += 0 if ok else 1
finally:
    time.time = real
sys.exit(1 if failures else 0)
