"""MemAvailable and SoC temperature. Real: /proc and /sys. Mock: fixed, settable by tests."""
from __future__ import annotations

from pathlib import Path


class ProcSysInfo:
    def mem_available_mb(self) -> float:
        try:
            for line in Path("/proc/meminfo").read_text().splitlines():
                if line.startswith("MemAvailable:"):
                    return round(int(line.split()[1]) / 1024, 1)
        except (OSError, ValueError, IndexError):
            pass
        return 0.0       # unknown counts as "not enough": fail closed

    def temp_c(self) -> float | None:
        try:
            return round(int(Path("/sys/class/thermal/thermal_zone0/temp").read_text()) / 1000, 1)
        except (OSError, ValueError):
            return None


class MockSysInfo:
    def __init__(self, mem_mb: float = 812.0, temp: float | None = None):
        self.mem = mem_mb
        self.temp = temp

    def mem_available_mb(self) -> float:
        return self.mem

    def temp_c(self) -> float | None:
        return self.temp
