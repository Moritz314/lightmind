"""A memory limit for what a tool or shell command starts.  One runaway tool must never freeze the machine
(2026-10-21: a model tool grew to 8 GB in 30 s on a 13.7 GB laptop).

* Windows: a Job Object (JOB_OBJECT_LIMIT_JOB_MEMORY for the whole tree, KILL_ON_JOB_CLOSE) made with ctypes.
  Children inherit the job; no breakaway is allowed.  The process is put in the job right after it starts, so a
  child it starts in those first microseconds is the only one that could escape.
* POSIX: RLIMIT_AS in the child before exec (per process, which is enough here).

Past the limit an allocation fails, the program usually ends with an error, and `exceeded` recognises that."""
from __future__ import annotations

import os
import re
from typing import Any, Callable

RAM_SHARE = 0.4            # default limit: this share of the physical RAM ...
RAM_FLOOR_MB = 2048        # ... but never below this,
RAM_CEILING = 0.8          # and no limit, not even a tool's own, above this share
UNKNOWN_RAM_MB = 4096      # default when the RAM cannot be read
_OOM = re.compile(r"MemoryError|Cannot allocate memory|bad_alloc|out of memory|failed to allocate|"
                  r"not enough memory|ENOMEM|insufficient memory", re.I)

_MB = 1 << 20
_JOB_MEMORY, _KILL_ON_CLOSE, _EXTENDED_INFO = 0x200, 0x2000, 9
_SET_QUOTA, _TERMINATE = 0x0100, 0x0001


def physical_ram_mb() -> int:
    """Physical RAM in MB; 0 when it cannot be read."""
    try:
        if os.name == "nt":
            import ctypes
            from ctypes import wintypes

            class Status(ctypes.Structure):
                _fields_ = [("length", wintypes.DWORD), ("load", wintypes.DWORD), ("total", ctypes.c_ulonglong),
                            ("avail", ctypes.c_ulonglong), ("total_page", ctypes.c_ulonglong),
                            ("avail_page", ctypes.c_ulonglong), ("total_virt", ctypes.c_ulonglong),
                            ("avail_virt", ctypes.c_ulonglong), ("avail_ext", ctypes.c_ulonglong)]
            s = Status()
            s.length = ctypes.sizeof(Status)
            if not ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(s)):
                return 0
            return int(s.total // _MB)
        return int(os.sysconf("SC_PAGE_SIZE") * os.sysconf("SC_PHYS_PAGES") // _MB)
    except (AttributeError, OSError, ValueError):
        return 0


def available_ram_mb() -> int:
    """RAM in MB that can be had now without swapping (Windows: available physical memory, Linux: MemAvailable);
    0 when it cannot be read (macOS, a locked /proc), so a caller must treat 0 as unknown, not as full."""
    try:
        if os.name == "nt":
            import ctypes
            from ctypes import wintypes

            class Status(ctypes.Structure):
                _fields_ = [("length", wintypes.DWORD), ("load", wintypes.DWORD), ("total", ctypes.c_ulonglong),
                            ("avail", ctypes.c_ulonglong), ("total_page", ctypes.c_ulonglong),
                            ("avail_page", ctypes.c_ulonglong), ("total_virt", ctypes.c_ulonglong),
                            ("avail_virt", ctypes.c_ulonglong), ("avail_ext", ctypes.c_ulonglong)]
            s = Status()
            s.length = ctypes.sizeof(Status)
            if not ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(s)):
                return 0
            return int(s.avail // _MB)
        with open("/proc/meminfo", encoding="ascii") as f:
            for line in f:
                if line.startswith("MemAvailable:"):
                    return int(line.split()[1]) // 1024
    except (AttributeError, OSError, ValueError):
        return 0
    return 0


def default_limit_mb(ram: int) -> int:
    return max(RAM_FLOOR_MB, round(ram * RAM_SHARE)) if ram > 0 else UNKNOWN_RAM_MB


def limit_mb(configured: Any, tool_mb: Any = None, ram: int | None = None) -> int:
    """The limit in MB for one process tree; 0 = none.  `configured` is `tools.memory_mb` (empty: the default,
    0: off), `tool_mb` the `memory_mb:` of a tool's frontmatter (it replaces the configured value, but a global 0
    still means off).  Whatever the source, never above 80 % of the RAM."""
    ram = physical_ram_mb() if ram is None else ram
    try:
        value = default_limit_mb(ram) if configured in (None, "") or isinstance(configured, bool) else int(configured)
    except (TypeError, ValueError):
        value = default_limit_mb(ram)
    if value <= 0:
        return 0
    try:
        own = int(tool_mb) if tool_mb not in (None, "") and not isinstance(tool_mb, bool) else 0
    except (TypeError, ValueError):
        own = 0
    if own > 0:
        value = own
    return min(value, int(ram * RAM_CEILING)) if ram > 0 else value


def posix_preexec(mb: int) -> Callable[[], None]:
    """For `preexec_fn`: cap the address space of the new process (and so of what it starts)."""
    def apply() -> None:
        import resource
        cap = mb * _MB
        _, hard = resource.getrlimit(resource.RLIMIT_AS)
        if hard != resource.RLIM_INFINITY:
            cap = min(cap, hard)
        resource.setrlimit(resource.RLIMIT_AS, (cap, hard))
    return apply


class Job:
    """A Windows Job Object holding one process tree.  `Job.start(mb, pid)` is None when there is nothing to
    limit (not Windows, or the job could not be made: then the tool runs unlimited rather than not at all)."""

    def __init__(self, handle: Any, mb: int) -> None:
        self.handle, self.mb = handle, mb

    @staticmethod
    def _api():
        import ctypes
        from ctypes import wintypes

        class Basic(ctypes.Structure):
            _fields_ = [("per_process_time", ctypes.c_longlong), ("per_job_time", ctypes.c_longlong),
                        ("flags", wintypes.DWORD), ("min_ws", ctypes.c_size_t), ("max_ws", ctypes.c_size_t),
                        ("active_limit", wintypes.DWORD), ("affinity", ctypes.c_size_t),
                        ("priority", wintypes.DWORD), ("scheduling", wintypes.DWORD)]

        class IO(ctypes.Structure):
            _fields_ = [(n, ctypes.c_ulonglong) for n in
                        ("rd_ops", "wr_ops", "other_ops", "rd_bytes", "wr_bytes", "other_bytes")]

        class Extended(ctypes.Structure):
            _fields_ = [("basic", Basic), ("io", IO), ("process_limit", ctypes.c_size_t),
                        ("job_limit", ctypes.c_size_t), ("peak_process", ctypes.c_size_t),
                        ("peak_job", ctypes.c_size_t)]

        k = ctypes.WinDLL("kernel32", use_last_error=True)
        k.CreateJobObjectW.restype = wintypes.HANDLE
        k.CreateJobObjectW.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR]
        k.SetInformationJobObject.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD]
        k.QueryInformationJobObject.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD,
                                                ctypes.c_void_p]
        k.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
        k.OpenProcess.restype = wintypes.HANDLE
        k.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        k.CloseHandle.argtypes = [wintypes.HANDLE]
        return ctypes, k, Extended

    @classmethod
    def start(cls, mb: int, pid: int) -> "Job | None":
        if os.name != "nt" or mb <= 0:
            return None
        try:
            ctypes, k, Extended = cls._api()
            job = k.CreateJobObjectW(None, None)
            if not job:
                return None
            info = Extended()
            info.basic.flags = _JOB_MEMORY | _KILL_ON_CLOSE      # no BREAKAWAY flag: children stay inside
            info.job_limit = mb * _MB
            proc = k.OpenProcess(_SET_QUOTA | _TERMINATE, False, pid)
            if not (k.SetInformationJobObject(job, _EXTENDED_INFO, ctypes.byref(info), ctypes.sizeof(info)) and proc
                    and k.AssignProcessToJobObject(job, proc)):
                k.CloseHandle(job)
                if proc:
                    k.CloseHandle(proc)
                return None
            k.CloseHandle(proc)
            return cls(job, mb)
        except (OSError, AttributeError):
            return None

    def peak_mb(self) -> float:
        """The most committed memory the tree has used so far."""
        try:
            ctypes, k, Extended = self._api()
            info = Extended()
            if k.QueryInformationJobObject(self.handle, _EXTENDED_INFO, ctypes.byref(info), ctypes.sizeof(info), None):
                return info.peak_job / _MB
        except (OSError, AttributeError):
            pass
        return 0.0

    def close(self) -> None:
        """Closing the handle ends every process still in the job (KILL_ON_JOB_CLOSE)."""
        h, self.handle = self.handle, None
        if h:
            try:
                self._api()[1].CloseHandle(h)
            except (OSError, AttributeError):
                pass


def exceeded(rc: int, text: str, mb: int, job: Job | None = None) -> bool:
    """Did a process that ended with `rc` and printed `text` die at its memory limit?  A failed allocation is
    reported by the program itself (MemoryError, bad_alloc ...); on Windows the job's peak confirms it."""
    if rc == 0 or mb <= 0:
        return False
    if job is not None and job.handle:
        peak = job.peak_mb()
        if peak >= 0.9 * mb:
            return True
    return bool(_OOM.search(text))
