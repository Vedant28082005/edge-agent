# PlatformIO pre-script: writes the firmware build timestamp (Unix seconds, UTC) into a generated header.
# The firmware rejects NTP time earlier than this (contracts/mqtt.md, "Firmware time requirement").
# Only src/edge/fw_build.cpp includes the header, so a rebuild recompiles just that file.
import os
import time

Import("env")  # noqa: F821  (provided by PlatformIO/SCons)

gen = os.path.join(env.subst("$BUILD_DIR"), "fwgen")  # noqa: F821
os.makedirs(gen, exist_ok=True)
path = os.path.join(gen, "fw_build_ts.h")
with open(path, "w") as f:
    f.write("#pragma once\n#define FW_BUILD_TS %dLL\n" % int(time.time()))
env.Append(CPPPATH=[gen])  # noqa: F821
