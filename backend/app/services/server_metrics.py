"""Live metrics for the machine that runs ClipForge: CPU, memory, swap, network, load and uptime.

Percentages come from psutil. Network rates are computed from counter deltas between
calls. Calls closer together than _MIN_WINDOW_SECONDS reuse the last rate, so a burst
of requests does not produce a near-zero measurement window.
"""

from __future__ import annotations

import os
import threading
import time
from typing import Any

import psutil

_MIN_WINDOW_SECONDS = 1.0

_lock = threading.Lock()
_last_net: tuple[float, int, int] | None = None  # (monotonic time, bytes_sent, bytes_recv)
_last_rate: tuple[float, float] = (0.0, 0.0)  # (sent per second, recv per second)

# psutil.cpu_percent(interval=None) measures since the previous call and returns 0.0 on
# the first call. Priming it here makes the first real reading meaningful.
psutil.cpu_percent(interval=None)


def _network_totals() -> tuple[int, int]:
    """Sum traffic over all non-loopback interfaces."""
    sent = recv = 0
    for name, counters in psutil.net_io_counters(pernic=True).items():
        if name.startswith("lo"):
            continue
        sent += counters.bytes_sent
        recv += counters.bytes_recv
    return sent, recv


def _network_rates(sent: int, recv: int) -> tuple[float, float]:
    global _last_net, _last_rate
    now = time.monotonic()
    with _lock:
        if _last_net is None:
            _last_net = (now, sent, recv)
            return _last_rate
        elapsed = now - _last_net[0]
        if elapsed < _MIN_WINDOW_SECONDS:
            return _last_rate
        # max(0, ...) guards against counter resets, for example after a reboot.
        rate = (
            max(0, sent - _last_net[1]) / elapsed,
            max(0, recv - _last_net[2]) / elapsed,
        )
        _last_net = (now, sent, recv)
        _last_rate = rate
        return rate


def _load_average() -> list[float] | None:
    try:
        return [round(v, 2) for v in os.getloadavg()]
    except (OSError, AttributeError):
        # Not available on every platform, Windows in particular.
        return None


def snapshot() -> dict[str, Any]:
    vm = psutil.virtual_memory()
    swap = psutil.swap_memory()
    sent, recv = _network_totals()
    sent_per_s, recv_per_s = _network_rates(sent, recv)
    return {
        "cpu": {
            "percent": round(psutil.cpu_percent(interval=None), 1),
            "cores": psutil.cpu_count(logical=True) or 0,
            "load_average": _load_average(),
        },
        "memory": {
            "percent": round(vm.percent, 1),
            # "available" is what Linux and macOS both report as reclaimable, so it avoids
            # counting disk cache as used memory.
            "used_bytes": vm.total - vm.available,
            "total_bytes": vm.total,
        },
        "swap": {
            "percent": round(swap.percent, 1),
            "used_bytes": swap.used,
            "total_bytes": swap.total,
        },
        "network": {
            "sent_bytes_per_s": round(sent_per_s),
            "recv_bytes_per_s": round(recv_per_s),
            "sent_total_bytes": sent,
            "recv_total_bytes": recv,
        },
        "uptime_seconds": int(time.time() - psutil.boot_time()),
    }
