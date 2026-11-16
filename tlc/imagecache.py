"""Keeping recently decoded images in memory, per worker process.

Detect Lines, Detect Spots and Calculate RF each load the same stored image;
decoding a 50 MP phone photo takes ~0.8 s. The last few decoded images are
kept, up to ``IMAGE_CACHE_MB``, least recently used dropped first.

Cached arrays are read-only, so no request can draw on another's image; a
caller that draws makes its own copy. Stored images are written once under a
random name and never changed; (path, inode, size, modification time)
identifies one even if a deleted file's name and inode were reused. Using a
file sets only its access time (:func:`tlc.media.touch`).
"""

from __future__ import annotations

import os
import threading
from collections import OrderedDict
from collections.abc import Callable

import numpy as np
from django.conf import settings

_lock = threading.Lock()
_entries: OrderedDict[tuple[str, int, int, int], np.ndarray] = OrderedDict()


def _key(path: str) -> tuple[str, int, int, int]:
    stat = os.stat(path)
    return (path, stat.st_ino, stat.st_size, stat.st_mtime_ns)


def contains(path: str) -> bool:
    """Whether the image at ``path`` is cached (so a job need not decode it)."""
    try:
        key = _key(path)
    except OSError:
        return False
    with _lock:
        return key in _entries


def release(keep: str | None = None) -> None:
    """Drop cached images to make room for a job, except the one at ``keep``."""
    with _lock:
        for key in [k for k in _entries if k[0] != keep]:
            del _entries[key]


def load(path: str, decode: Callable[[bytes], np.ndarray | None]) -> np.ndarray | None:
    """The decoded image at ``path``, from the cache or via ``decode``."""
    key = _key(path)
    with _lock:
        image = _entries.get(key)
        if image is not None:
            _entries.move_to_end(key)
            return image
    with open(path, "rb") as fh:
        image = decode(fh.read())
    if image is None:
        return None
    image.flags.writeable = False
    _store(key, image)
    return image


def _store(key: tuple[str, int, int, int], image: np.ndarray) -> None:
    budget = settings.IMAGE_CACHE_MB * 1024 * 1024
    if image.nbytes > budget:
        return
    with _lock:
        _entries[key] = image
        _entries.move_to_end(key)
        while sum(entry.nbytes for entry in _entries.values()) > budget:
            _entries.popitem(last=False)


def clear() -> None:
    """Drop every cached image (tests)."""
    with _lock:
        _entries.clear()
