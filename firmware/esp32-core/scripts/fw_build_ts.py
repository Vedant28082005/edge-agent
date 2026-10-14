# PlatformIO pre-script: writes the firmware build timestamp (Unix seconds, UTC) into a generated header.
# The firmware rejects NTP time earlier than this (contracts/mqtt.md, "Firmware time requirement").
# Only src/edge/fw_build.cpp includes the header, so a rebuild recompiles just that file.
#
# The build ABORTS if the build host's clock is earlier than FLOOR_TS (2026-10-01T00:00:00Z): a build machine
# with a wrong clock would ship firmware that accepts time from before the certificates were issued.
# Raise the constant when the contract is next revised.
import os
import sys
import time

FLOOR_TS = 1790812800  # 2026-10-01T00:00:00Z (contracts/mqtt.md, "Build-time sanity check")


def build_timestamp():
    now = int(time.time())
    if now < FLOOR_TS:
        raise SystemExit(
            "fw_build_ts: build host clock (%d) is earlier than 2026-10-01T00:00:00Z (%d). "
            "Fix the PC clock; refusing to build firmware with a bogus FW_BUILD_TS." % (now, FLOOR_TS)
        )
    return now


try:
    Import("env")  # noqa: F821  (provided by PlatformIO/SCons)
except NameError:  # imported from tools/test_build_floor.py
    env = None

if env is not None:
    gen = os.path.join(env.subst("$BUILD_DIR"), "fwgen")
    os.makedirs(gen, exist_ok=True)
    with open(os.path.join(gen, "fw_build_ts.h"), "w") as f:
        f.write("#pragma once\n#define FW_BUILD_TS %dLL\n" % build_timestamp())
    env.Append(CPPPATH=[gen])
