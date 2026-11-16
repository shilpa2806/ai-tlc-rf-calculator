"""Admitting image jobs by the memory they need, not by a fixed count.

Running out of memory is what breaks the server (the kernel kills a worker
mid-request); CPU overload only makes requests slower. So before an image
job -- decoding a photo, detection, drawing the result -- starts, it states
the memory it will need (:func:`job_bytes`, from the image's pixel count:
measured ~760 MB for a 50 MP phone photo decoded afresh, ~320 MB when it is
already cached; re-estimated at every check, as a queued job's image may
meanwhile have been cached). It runs when that fits in the memory that is free
now, minus a reserve and minus what running jobs in *all* workers have
claimed. If it does not, the worker first drops its cached images (a
convenience, not a need); then the job waits, re-checking as jobs finish, and
after ``IMAGE_JOB_WAIT_SECONDS`` gives up with :class:`Busy` (the views answer
503 "try again"). After every job, freed memory is handed back to the system
(``malloc_trim``), or the next reading would still count it as used.

Free memory is read from the container's cgroup when there is one (so the
limit is the container's, not the host's), else from ``/proc/meminfo``.
Claims are shared between worker processes in a small file under an
exclusive lock (``IMAGE_JOB_CLAIMS_FILE``): without it, two workers checking
at the same instant -- a burst of requests -- both see the memory as free and
both start (measured: 1474 MB of a 1500 MB container). Claims of a process
that died are dropped. The count is conservative -- a running job is counted
both in measured usage and in its claim -- which errs towards waiting, never
towards an out-of-memory kill. Where no reading is possible (not Linux),
every job is admitted.
"""

from __future__ import annotations

import ctypes
import json
import os
import threading
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from pathlib import Path

from django.conf import settings

# Measured peaks per pixel (ONNX Runtime's memory arena off), on a 50 MP
# photo, in and out of the container: decoding dominates any job; with the
# image already decoded, each job's own work (Calculate RF copies the image
# and encodes a PNG); and the extra full-size copy a fine rotation makes.
BYTES_PER_PIXEL_DECODE = 16
BYTES_PER_PIXEL_CACHED = {"lines": 2, "spots": 5, "render": 7}
BYTES_PER_PIXEL_FINE_ROTATION = 3
# Both ONNX sessions, loaded by a worker's first detection (arena off).
MODEL_LOAD_BYTES = 150 * 1024 * 1024

_UNLIMITED = 1 << 60  # cgroup v1 reports "no limit" as a huge number

try:
    import fcntl
except ImportError:  # not Unix: claims stay within this process
    fcntl = None  # type: ignore[assignment]

_cond = threading.Condition()
_local_claims: dict[int, int] = {}  # used only without fcntl


class Busy(Exception):
    """Not enough free memory for this job within the waiting time."""


def job_bytes(
    width: int,
    height: int,
    *,
    kind: str = "render",
    cached: bool = False,
    fine_rotation: bool = False,
    load_models: bool = False,
    extra: int = 0,
) -> int:
    """Memory an image job needs: its peak above the worker's resting use."""
    per_pixel = BYTES_PER_PIXEL_CACHED[kind] if cached else BYTES_PER_PIXEL_DECODE
    if fine_rotation:
        per_pixel += BYTES_PER_PIXEL_FINE_ROTATION
    return width * height * per_pixel + (MODEL_LOAD_BYTES if load_models else 0) + extra


def _trim() -> None:
    """Return freed heap memory to the system (glibc only; else nothing)."""
    try:
        ctypes.CDLL("libc.so.6").malloc_trim(0)
    except OSError, AttributeError:
        pass


def _read_int(path: Path) -> int | None:
    try:
        text = path.read_text().strip()
    except OSError:
        return None
    return int(text) if text.isdigit() else None


def _stat_field(path: Path, field: str) -> int:
    try:
        for line in path.read_text().splitlines():
            name, _, value = line.partition(" ")
            if name == field:
                return int(value)
    except OSError, ValueError:
        pass
    return 0


def _cgroup_free(root: Path = Path("/sys/fs/cgroup")) -> int | None:
    """Free memory under the cgroup's limit, or ``None`` if it has none.

    Reclaimable page cache (``inactive_file``) counts as free, as the kernel
    drops it before killing anything.
    """
    limit = _read_int(root / "memory.max")  # cgroup v2; "max" -> None
    if limit is not None:
        used = _read_int(root / "memory.current") or 0
        cache = _stat_field(root / "memory.stat", "inactive_file")
        return limit - (used - cache)
    v1 = root / "memory"
    limit = _read_int(v1 / "memory.limit_in_bytes")
    if limit is not None and limit < _UNLIMITED:
        used = _read_int(v1 / "memory.usage_in_bytes") or 0
        cache = _stat_field(v1 / "memory.stat", "total_inactive_file")
        return limit - (used - cache)
    return None


def _cgroup_limit(root: Path = Path("/sys/fs/cgroup")) -> int | None:
    limit = _read_int(root / "memory.max")
    if limit is None:
        limit = _read_int(root / "memory" / "memory.limit_in_bytes")
    return limit if limit is not None and limit < _UNLIMITED else None


def _meminfo_field(field: str, path: Path = Path("/proc/meminfo")) -> int | None:
    try:
        for line in path.read_text().splitlines():
            if line.startswith(field + ":"):
                return int(line.split()[1]) * 1024
    except OSError, ValueError, IndexError:
        pass
    return None


def reserve_bytes() -> int:
    """Memory kept spare: ``IMAGE_JOB_RESERVE_MB``, but at most a tenth of the
    total, so a small container is not mostly reserve."""
    reserve = settings.IMAGE_JOB_RESERVE_MB * 1024 * 1024
    totals = [t for t in (_cgroup_limit(), _meminfo_field("MemTotal")) if t is not None]
    return min(reserve, min(totals) // 10) if totals else reserve


def _meminfo_free(path: Path = Path("/proc/meminfo")) -> int | None:
    try:
        for line in path.read_text().splitlines():
            if line.startswith("MemAvailable:"):
                return int(line.split()[1]) * 1024
    except OSError, ValueError, IndexError:
        pass
    return None


def free_bytes() -> int | None:
    """Memory free for new work now, or ``None`` if it cannot be read."""
    readings = [r for r in (_cgroup_free(), _meminfo_free()) if r is not None]
    return min(readings) if readings else None


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


@contextmanager
def _claims() -> Iterator[dict[int, int]]:
    """All workers' claims ``{pid: bytes}``, locked; changes are saved."""
    if fcntl is None:
        yield _local_claims
        return
    fd = os.open(settings.IMAGE_JOB_CLAIMS_FILE, os.O_RDWR | os.O_CREAT, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        raw = os.read(fd, 1 << 16)
        try:
            stored = json.loads(raw) if raw else {}
            claims = {int(pid): int(n) for pid, n in stored.items()}
        except ValueError, AttributeError, TypeError:
            claims = {}
        claims = {pid: n for pid, n in claims.items() if n > 0 and _alive(pid)}
        yield claims
        os.ftruncate(fd, 0)
        os.pwrite(fd, json.dumps(claims).encode(), 0)
    finally:
        os.close(fd)  # also releases the lock


def _try_claim(nbytes: int, reserve: int) -> bool:
    """Claim ``nbytes`` if they fit beside every worker's claims."""
    with _claims() as claims:
        free = free_bytes()
        if free is not None and nbytes > free - reserve - sum(claims.values()):
            return False
        pid = os.getpid()
        claims[pid] = claims.get(pid, 0) + nbytes
        return True


def _unclaim(nbytes: int) -> None:
    with _claims() as claims:
        pid = os.getpid()
        left = claims.get(pid, 0) - nbytes
        if left > 0:
            claims[pid] = left
        else:
            claims.pop(pid, None)


@contextmanager
def admit(
    needed: int | Callable[[], int], release: Callable[[], None] | None = None
) -> Iterator[None]:
    """Run the enclosed job once the memory it needs is free.

    ``needed`` is the job's bytes, or a function re-estimating them at each
    check. ``release`` frees memory this process can do without (its image
    cache); it is called once, the first time the job does not fit.

    Raises:
        Busy: if it does not fit within ``IMAGE_JOB_WAIT_SECONDS``.
    """
    estimate = needed if callable(needed) else (lambda: needed)
    reserve = reserve_bytes()
    deadline = time.monotonic() + settings.IMAGE_JOB_WAIT_SECONDS
    with _cond:
        while True:
            nbytes = estimate()
            if _try_claim(nbytes, reserve):
                break
            if release is not None:
                release()
                release = None
                _trim()
                continue
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise Busy
            # Woken when a job here finishes; the timeout also catches memory
            # freed by other workers.
            _cond.wait(min(0.5, remaining))
    try:
        yield
    finally:
        _trim()
        with _cond:
            _unclaim(nbytes)
            _cond.notify_all()
