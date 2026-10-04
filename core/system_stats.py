"""Host / process resource snapshot for dashboard (no psutil dependency)."""
from __future__ import annotations

import os
import time
from typing import Any, Dict, Optional

# Process start (approx) — set on first import
_BOOT_MONO = time.monotonic()
_BOOT_WALL = time.time()


def _read_text(path: str) -> str:
    try:
        with open(path, "r", encoding="utf-8", errors="ignore") as f:
            return f.read()
    except Exception:
        return ""


def _parse_kb_line(line: str) -> Optional[int]:
    # e.g. "VmRSS:\t  421336 kB"
    parts = line.replace(":", " ").split()
    for i, p in enumerate(parts):
        if p.isdigit():
            unit = parts[i + 1].lower() if i + 1 < len(parts) else "kb"
            n = int(p)
            if unit.startswith("kb"):
                return n * 1024
            if unit.startswith("mb"):
                return n * 1024 * 1024
            if unit.startswith("gb"):
                return n * 1024 * 1024 * 1024
            return n
    return None


def process_rss_bytes() -> Optional[int]:
    status = _read_text("/proc/self/status")
    for line in status.splitlines():
        if line.startswith("VmRSS:"):
            return _parse_kb_line(line)
    return None


def host_mem() -> Dict[str, Optional[int]]:
    """Container/host memory from /proc/meminfo (bytes)."""
    info = _read_text("/proc/meminfo")
    total = avail = free = None
    for line in info.splitlines():
        if line.startswith("MemTotal:"):
            total = _parse_kb_line(line)
        elif line.startswith("MemAvailable:"):
            avail = _parse_kb_line(line)
        elif line.startswith("MemFree:"):
            free = _parse_kb_line(line)
    used = None
    if total is not None and avail is not None:
        used = max(0, total - avail)
    elif total is not None and free is not None:
        used = max(0, total - free)
    return {"total": total, "used": used, "available": avail if avail is not None else free}


def disk_usage(path: str = ".") -> Dict[str, Optional[int]]:
    try:
        st = os.statvfs(path)
        total = st.f_frsize * st.f_blocks
        free = st.f_frsize * st.f_bavail
        used = total - free
        return {"total": total, "used": used, "free": free}
    except Exception:
        return {"total": None, "used": None, "free": None}


def fmt_bytes(n: Optional[int]) -> str:
    if n is None:
        return "—"
    n = float(n)
    for unit, div in (("GB", 1024**3), ("MB", 1024**2), ("KB", 1024)):
        if n >= div:
            return f"{n / div:.1f} {unit}"
    return f"{int(n)} B"


def bar(used: Optional[int], total: Optional[int], width: int = 12) -> str:
    if not total or used is None:
        return "░" * width
    pct = max(0.0, min(1.0, used / total))
    filled = int(round(pct * width))
    return "█" * filled + "░" * (width - filled)


def pct(used: Optional[int], total: Optional[int]) -> str:
    if not total or used is None:
        return "—"
    return f"{min(100.0, 100.0 * used / total):.0f}%"


def uptime_str() -> str:
    sec = int(time.monotonic() - _BOOT_MONO)
    if sec < 60:
        return f"{sec}s"
    if sec < 3600:
        return f"{sec // 60}m {sec % 60}s"
    h = sec // 3600
    m = (sec % 3600) // 60
    if h < 48:
        return f"{h}h {m}m"
    return f"{h // 24}d {h % 24}h"


def collect_runtime_counts() -> Dict[str, int]:
    jobs_running = 0
    jobs_queued = 0
    clients = 0
    try:
        from core.job_worker import RUNNING_JOB_TASKS, CLIENTS, MAX_CONCURRENT_JOBS
        jobs_running = sum(1 for t in RUNNING_JOB_TASKS.values() if t and not t.done())
        clients = len(CLIENTS)
        # approximate queued = cannot know DB here cheaply without async
        _ = MAX_CONCURRENT_JOBS
    except Exception:
        pass
    return {
        "jobs_running": jobs_running,
        "clients": clients,
        "max_concurrent_jobs": int(
            os.environ.get("MAX_CONCURRENT_JOBS", "2") or 2
        ),
    }


async def build_system_usage_text() -> str:
    """Markdown text for System Usage screen."""
    rss = process_rss_bytes()
    mem = host_mem()
    disk = disk_usage(".")
    counts = collect_runtime_counts()

    lines = [
        "**📊 System Usage**",
        "",
        "**🧠 Memory**",
        f"`{bar(mem.get('used'), mem.get('total'))}` "
        f"**{fmt_bytes(mem.get('used'))}** / {fmt_bytes(mem.get('total'))} "
        f"({pct(mem.get('used'), mem.get('total'))})",
        f"Bot process (RSS): **{fmt_bytes(rss)}**",
        f"Available: **{fmt_bytes(mem.get('available'))}**",
        "",
        "**💾 Disk** (app volume)",
        f"`{bar(disk.get('used'), disk.get('total'))}` "
        f"**{fmt_bytes(disk.get('used'))}** / {fmt_bytes(disk.get('total'))} "
        f"({pct(disk.get('used'), disk.get('total'))})",
        f"Free: **{fmt_bytes(disk.get('free'))}**",
        "",
        "**⚙️ Runtime**",
        f"Uptime: **{uptime_str()}**",
        f"Live job tasks: **{counts['jobs_running']}** "
        f"(max concurrent `{counts['max_concurrent_jobs']}`)",
        f"Forward clients in memory: **{counts['clients']}**",
    ]

    # Optional env hints
    low = (os.environ.get("LOW_MEMORY", "") or "").strip().lower() in ("1", "true", "yes")
    if low:
        lines.append("Mode: `LOW_MEMORY=1`")

    # Mongo ping latency (lightweight)
    try:
        from database import db
        t0 = time.monotonic()
        if db.client is not None:
            await db.client.admin.command("ping")
            ms = (time.monotonic() - t0) * 1000
            lines.extend(["", f"**🗄️ MongoDB** ping: **{ms:.0f} ms**"])
        else:
            lines.extend(["", "**🗄️ MongoDB:** not connected"])
    except Exception as e:
        lines.extend(["", f"**🗄️ MongoDB:** error `{type(e).__name__}`"])

    lines.extend([
        "",
        "_Host/container limits as seen by this process. "
        "Koyeb free ≈ 512 MB RAM._",
    ])
    return "\n".join(lines)
