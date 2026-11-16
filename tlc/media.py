"""The media directory: where every file the app writes goes, and how it is pruned.

Uploaded images, images fetched from the ELN, annotated images and CSVs are all
written to ``MEDIA_ROOT`` under random names and are needed only while someone
is working on a plate. They are deleted once unused for
``MEDIA_MAX_AGE_HOURS``:

* automatically -- after a write, at most once every ``MEDIA_SWEEP_MINUTES``
  per worker process, so a container needs no cron job;
* on demand -- ``python manage.py cleanup_media``.

"Unused" counts from the last time a request read the file (:func:`touch`,
which sets its access time), not from when it was written, so a plate left
open in a tab keeps its image as long as it is being worked on. The
modification time is left alone: it identifies the file's content
(:mod:`tlc.imagecache`).
"""

from __future__ import annotations

import logging
import os
import threading
import time
from collections.abc import Iterator

from django.conf import settings

logger = logging.getLogger(__name__)

# Never deleted, whatever their age.
_PROTECTED = {".gitkeep"}

_sweep_lock = threading.Lock()
_last_sweep = 0.0


def write(name: str, content: bytes | str) -> None:
    """Write ``content`` to ``MEDIA_ROOT/name``, then prune if a sweep is due."""
    os.makedirs(settings.MEDIA_ROOT, exist_ok=True)
    path = os.path.join(settings.MEDIA_ROOT, name)
    if isinstance(content, str):
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(content)
    else:
        with open(path, "wb") as fh:
            fh.write(content)
    prune_if_due()


def touch(path: str) -> None:
    """Mark a media file as just used, so a sweep keeps it.

    Sets the access time explicitly (``noatime`` mounts do not stop that).
    """
    try:
        os.utime(path, ns=(time.time_ns(), os.stat(path).st_mtime_ns))
    except OSError:
        pass


def _last_used(stat: os.stat_result) -> float:
    return max(stat.st_atime, stat.st_mtime)


def stale_files(max_age_hours: float) -> Iterator[os.DirEntry[str]]:
    """Media files not used for at least ``max_age_hours``."""
    if not os.path.isdir(settings.MEDIA_ROOT):
        return
    cutoff = time.time() - max_age_hours * 3600
    for entry in os.scandir(settings.MEDIA_ROOT):
        try:
            if (
                entry.is_file(follow_symlinks=False)
                and entry.name not in _PROTECTED
                and _last_used(entry.stat(follow_symlinks=False)) < cutoff
            ):
                yield entry
        except FileNotFoundError:  # removed meanwhile by another worker
            continue


def prune(max_age_hours: float) -> int:
    """Delete media files unused for ``max_age_hours``; return how many."""
    removed = 0
    for entry in stale_files(max_age_hours):
        try:
            os.remove(entry.path)
            removed += 1
        except FileNotFoundError:
            continue
    return removed


def prune_if_due() -> None:
    """Prune, unless disabled or this process swept within the interval.

    Never raises: a failed sweep must not fail the request that triggered it.
    """
    global _last_sweep
    max_age = settings.MEDIA_MAX_AGE_HOURS
    if max_age <= 0:
        return
    now = time.monotonic()
    if not _sweep_lock.acquire(blocking=False):
        return  # another thread is sweeping
    try:
        if _last_sweep and now - _last_sweep < settings.MEDIA_SWEEP_MINUTES * 60:
            return
        _last_sweep = now
        removed = prune(max_age)
        if removed:
            logger.info("Removed %d media file(s) unused for %s h", removed, max_age)
    except OSError:
        logger.exception("Media sweep failed")
    finally:
        _sweep_lock.release()
