"""What this computer can give the engine: processors, memory, storage, graphics.

``detect()`` reports the machine; ``recommend_workers()`` turns that into worker counts for each pipeline stage.
Nothing here needs a third-party package: memory comes from /proc (Linux), sysctl/vm_stat (macOS) or the Win32 API.

Syncora's analysis runs on the processor. A graphics processor is reported when one can be identified, but the
engine does not use it, and the recommendations never depend on it.
"""

from __future__ import annotations

import ctypes
import os
import shutil
import subprocess
import sys
import tempfile
import time
from dataclasses import asdict, dataclass
from functools import lru_cache
from pathlib import Path

GIB = 2**30


@dataclass(frozen=True)
class Resources:
    cpu_logical: int
    cpu_usable: int  # cores this process may run on (affinity, containers)
    ram_total_bytes: int | None
    ram_available_bytes: int | None
    gpu: str | None
    gpu_memory_bytes: int | None
    platform: str

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass(frozen=True)
class WorkerPlan:
    """Workers per stage. ``probe`` reads metadata (disk-bound), ``analyze`` decodes audio (one FFmpeg process
    each, plus signal processing), ``match`` compares recordings (processor-bound, one process each)."""

    probe: int
    analyze: int
    match: int
    reason: str

    def to_dict(self) -> dict:
        return asdict(self)


def cpu_usable() -> int:
    if hasattr(os, "sched_getaffinity"):
        try:
            return max(1, len(os.sched_getaffinity(0)))
        except OSError:
            pass
    return max(1, os.cpu_count() or 1)


def memory() -> tuple[int | None, int | None]:
    """Total and available physical memory in bytes (None when the system will not say)."""
    try:
        if sys.platform.startswith("linux"):
            fields = {}
            with open("/proc/meminfo") as f:
                for line in f:
                    key, _, rest = line.partition(":")
                    fields[key] = int(rest.split()[0]) * 1024
            return fields.get("MemTotal"), fields.get("MemAvailable", fields.get("MemFree"))
        if sys.platform == "darwin":
            total = int(subprocess.run(["sysctl", "-n", "hw.memsize"], capture_output=True, text=True,
                                       timeout=5).stdout.strip())  # fmt: skip
            vm = subprocess.run(["vm_stat"], capture_output=True, text=True, timeout=5).stdout
            page = 4096
            counts: dict[str, int] = {}
            for line in vm.splitlines():
                if "page size of" in line:
                    page = int(line.split("page size of")[1].split()[0])
                key, _, value = line.partition(":")
                value = value.strip().rstrip(".")
                if value.isdigit():
                    counts[key.strip()] = int(value)
            free = sum(
                counts.get(k, 0) for k in ("Pages free", "Pages inactive", "Pages speculative", "Pages purgeable")
            )
            return total, free * page
        if sys.platform == "win32":

            class MemoryStatus(ctypes.Structure):
                _fields_ = [
                    ("dwLength", ctypes.c_ulong), ("dwMemoryLoad", ctypes.c_ulong),
                    ("ullTotalPhys", ctypes.c_ulonglong), ("ullAvailPhys", ctypes.c_ulonglong),
                    ("ullTotalPageFile", ctypes.c_ulonglong), ("ullAvailPageFile", ctypes.c_ulonglong),
                    ("ullTotalVirtual", ctypes.c_ulonglong), ("ullAvailVirtual", ctypes.c_ulonglong),
                    ("ullAvailExtendedVirtual", ctypes.c_ulonglong),
                ]  # fmt: skip

            status = MemoryStatus()
            status.dwLength = ctypes.sizeof(MemoryStatus)
            if ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(status)):  # type: ignore[attr-defined]
                return int(status.ullTotalPhys), int(status.ullAvailPhys)
    except (OSError, ValueError, subprocess.SubprocessError):
        pass
    return None, None


@lru_cache(maxsize=1)
def gpu() -> tuple[str | None, int | None]:
    """Name and memory of the main graphics processor, when cheaply known (for information only)."""
    try:
        if shutil.which("nvidia-smi"):
            out = subprocess.run(
                ["nvidia-smi", "--query-gpu=name,memory.total", "--format=csv,noheader,nounits"],
                capture_output=True, text=True, timeout=5,
            ).stdout.strip().splitlines()  # fmt: skip
            if out:
                name, mem = (part.strip() for part in out[0].split(","))
                return name, int(float(mem) * 2**20)
        if sys.platform == "darwin":
            brand = subprocess.run(["sysctl", "-n", "machdep.cpu.brand_string"], capture_output=True, text=True,
                                   timeout=5).stdout.strip()  # fmt: skip
            if brand.startswith("Apple"):
                return f"{brand} (integrated, shares system memory)", None
    except (OSError, ValueError, subprocess.SubprocessError):
        pass
    return None, None


def detect() -> Resources:
    total, available = memory()
    gpu_name, gpu_mem = gpu()
    return Resources(
        cpu_logical=os.cpu_count() or 1,
        cpu_usable=cpu_usable(),
        ram_total_bytes=total,
        ram_available_bytes=available,
        gpu=gpu_name,
        gpu_memory_bytes=gpu_mem,
        platform=sys.platform,
    )


#: Memory a worker needs at its peak: decoding holds 1 MiB chunks; matching holds two envelopes, FFT buffers and
#: the fine-stage windows (a three-hour recorder against a clip: about 150 MB).
ANALYZE_WORKER_BYTES = 200 * 2**20
MATCH_WORKER_BYTES = 400 * 2**20


def recommend_workers(res: Resources | None = None) -> WorkerPlan:
    res = res or detect()
    cores = res.cpu_usable
    # One core stays free for the app and the engine's own threads.
    compute = max(1, cores - 1)
    reason = [f"{cores} usable cores"]
    if res.ram_available_bytes is not None:
        budget = max(res.ram_available_bytes - 1 * GIB, 512 * 2**20)  # leave the system 1 GB
        by_ram_match = max(1, budget // MATCH_WORKER_BYTES)
        by_ram_analyze = max(1, budget // ANALYZE_WORKER_BYTES)
        reason.append(f"{res.ram_available_bytes / GIB:.1f} GB memory available")
    else:
        by_ram_match = by_ram_analyze = compute
    match = int(min(compute, by_ram_match))
    # Decoding runs in FFmpeg processes that also read the disk: more than 8 at once only adds seeking.
    analyze = int(min(compute, by_ram_analyze, 8))
    probe = int(min(8, max(2, cores)))
    return WorkerPlan(probe=probe, analyze=max(1, analyze), match=max(1, match), reason=", ".join(reason))


def disk_space(path: str | Path) -> dict:
    """Free and total bytes of the drive holding ``path`` (its nearest existing parent)."""
    p = Path(path)
    while not p.exists() and p != p.parent:
        p = p.parent
    usage = shutil.disk_usage(p)
    return {"path": str(path), "free_bytes": usage.free, "total_bytes": usage.total}


def measure_write_speed(directory: str | Path, size: int = 64 * 2**20) -> float:
    """Sequential write speed of a drive in bytes per second (writes, syncs and deletes one temporary file)."""
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    block = os.urandom(2**20)
    fd, name = tempfile.mkstemp(prefix=".syncora-speed-", dir=directory)
    try:
        t0 = time.perf_counter()
        with os.fdopen(fd, "wb") as f:
            for _ in range(size // len(block)):
                f.write(block)
            f.flush()
            os.fsync(f.fileno())
        return size / max(time.perf_counter() - t0, 1e-6)
    finally:
        os.unlink(name)


def raise_open_file_limit() -> int | None:
    """Allow the process as many open files as the system permits (memory-mapped analysis files each hold one).

    macOS starts applications with a limit of 256. Returns the new limit (None where there is no such limit).
    """
    try:
        import resource
    except ImportError:  # Windows: no per-process limit of this kind for our use
        return None
    soft, hard = resource.getrlimit(resource.RLIMIT_NOFILE)
    target = hard if hard != resource.RLIM_INFINITY else 65536
    if sys.platform == "darwin":
        target = min(target, 10240)  # OPEN_MAX: macOS refuses more for setrlimit
    if target > soft:
        try:
            resource.setrlimit(resource.RLIMIT_NOFILE, (target, hard))
            return target
        except (ValueError, OSError):
            return soft
    return soft


def peak_rss_bytes() -> int | None:
    """Largest resident memory this process has used so far."""
    try:
        import resource

        peak = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
        return int(peak if sys.platform == "darwin" else peak * 1024)
    except ImportError:
        pass
    if sys.platform == "win32":

        class Counters(ctypes.Structure):
            _fields_ = [
                ("cb", ctypes.c_ulong), ("PageFaultCount", ctypes.c_ulong),
                ("PeakWorkingSetSize", ctypes.c_size_t), ("WorkingSetSize", ctypes.c_size_t),
                ("QuotaPeakPagedPoolUsage", ctypes.c_size_t), ("QuotaPagedPoolUsage", ctypes.c_size_t),
                ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t), ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
                ("PagefileUsage", ctypes.c_size_t), ("PeakPagefileUsage", ctypes.c_size_t),
            ]  # fmt: skip

        counters = Counters()
        counters.cb = ctypes.sizeof(Counters)
        handle = ctypes.windll.kernel32.GetCurrentProcess()  # type: ignore[attr-defined]
        if ctypes.windll.psapi.GetProcessMemoryInfo(handle, ctypes.byref(counters), counters.cb):  # type: ignore[attr-defined]
            return int(counters.PeakWorkingSetSize)
    return None


def current_rss_bytes() -> int | None:
    """Resident memory of this process now (Linux and macOS read it from the system; else None)."""
    try:
        if sys.platform.startswith("linux"):
            with open("/proc/self/statm") as f:
                return int(f.read().split()[1]) * os.sysconf("SC_PAGE_SIZE")
        if sys.platform == "darwin":
            out = subprocess.run(["ps", "-o", "rss=", "-p", str(os.getpid())], capture_output=True, text=True,
                                 timeout=5).stdout  # fmt: skip
            return int(out.strip()) * 1024
    except (OSError, ValueError, subprocess.SubprocessError):
        pass
    return None
