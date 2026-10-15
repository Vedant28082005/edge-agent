"""Health figures: MemAvailable and SoC temperature (best effort; works on Linux, falls back elsewhere)."""
from __future__ import annotations

import sys
from pathlib import Path
from typing import Optional


def mem_available_mb() -> float:
    try:
        for line in Path("/proc/meminfo").read_text().splitlines():
            if line.startswith("MemAvailable:"):
                return round(int(line.split()[1]) / 1024.0, 1)
    except (OSError, ValueError, IndexError):
        pass
    if sys.platform == "win32":
        try:
            import ctypes

            class MS(ctypes.Structure):
                _fields_ = [("l", ctypes.c_ulong), ("load", ctypes.c_ulong), ("tp", ctypes.c_ulonglong),
                            ("ap", ctypes.c_ulonglong), ("tpf", ctypes.c_ulonglong), ("apf", ctypes.c_ulonglong),
                            ("tv", ctypes.c_ulonglong), ("av", ctypes.c_ulonglong), ("ae", ctypes.c_ulonglong)]
            ms = MS()
            ms.l = ctypes.sizeof(MS)
            ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(ms))   # type: ignore[attr-defined]
            return round(ms.ap / 1048576.0, 1)
        except Exception:  # noqa: BLE001
            pass
    return 0.0


def temp_c() -> Optional[float]:
    for p in ("/sys/class/thermal/thermal_zone0/temp",):
        try:
            return round(int(Path(p).read_text().strip()) / 1000.0, 1)
        except (OSError, ValueError):
            continue
    return None
