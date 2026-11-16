"""Lazily loaded ONNX models: TLC-SpotDet and TLC-LineDet.

Each session is created on first use, not at import, so management commands,
system checks and the unit tests that do not detect anything never pay for the
~430 MB of ONNX Runtime sessions. Creation is guarded by a lock: under
Gunicorn's threaded workers two concurrent first requests would otherwise both
build the same session. ``InferenceSession.run`` itself is thread-safe and the
wrappers hold no per-request state, so a loaded session is shared freely.

Every model also carries a :class:`ModelInfo` -- file name, SHA-256 and export
metadata -- which is returned with detection results so that candidates stay
traceable to the exact artefact that produced them (SI section 10.3).
"""

from __future__ import annotations

import hashlib
import threading
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

import onnxruntime as ort
from django.conf import settings

from .line import LineONNX
from .spot import SpotONNX


@dataclass(frozen=True)
class ModelInfo:
    """Identity of a deployed model artefact."""

    name: str  # "TLC-SpotDet" / "TLC-LineDet"
    file: str
    sha256: str
    exported: str  # export timestamp recorded by Ultralytics, if any
    ultralytics: str  # Ultralytics version that exported it, if recorded
    imgsz: int

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


_lock = threading.Lock()
_sessions: dict[str, tuple[Any, ModelInfo]] = {}


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _load(key: str, name: str, path: Path, factory: type) -> tuple[Any, ModelInfo]:
    cached = _sessions.get(key)
    if cached is not None:
        return cached
    with _lock:
        cached = _sessions.get(key)
        if cached is None:
            options = ort.SessionOptions()
            # ONNX Runtime ignores OMP_NUM_THREADS and otherwise starts one
            # thread per core per session; with several request threads per
            # worker that oversubscribes the CPU.
            options.intra_op_num_threads = int(settings.ONNX_INTRA_OP_THREADS)
            options.inter_op_num_threads = 1
            options.enable_cpu_mem_arena = bool(settings.ONNX_CPU_MEM_ARENA)
            model = factory(str(path), sess_options=options)
            meta = model.sess.get_modelmeta().custom_metadata_map
            info = ModelInfo(
                name=name,
                file=path.name,
                sha256=_sha256(path),
                exported=meta.get("date", ""),
                ultralytics=meta.get("version", ""),
                imgsz=model.imgsz,
            )
            cached = _sessions[key] = (model, info)
    return cached


def models_loaded() -> bool:
    """Whether both sessions exist in this process (a first job loads them)."""
    return "spot" in _sessions and "line" in _sessions


_identity_cache: dict[tuple[str, int, int], str] = {}


def model_identities() -> list[dict[str, str]]:
    """Name, file and SHA-256 of both models, without loading them.

    For records written when no detection ran in this process; hashes are
    cached by file path, size and modification time.
    """
    out = []
    for name, setting in (("TLC-SpotDet", "SPOT_MODEL_PATH"), ("TLC-LineDet", "LINE_MODEL_PATH")):
        path = Path(getattr(settings, setting))
        stat = path.stat()
        key = (str(path), stat.st_size, stat.st_mtime_ns)
        if key not in _identity_cache:
            _identity_cache[key] = _sha256(path)
        out.append({"name": name, "file": path.name, "sha256": _identity_cache[key]})
    return out


def spot_model() -> tuple[SpotONNX, ModelInfo]:
    """Return the shared TLC-SpotDet session and its identity."""
    model, info = _load("spot", "TLC-SpotDet", Path(settings.SPOT_MODEL_PATH), SpotONNX)
    return model, info


def line_model() -> tuple[LineONNX, ModelInfo]:
    """Return the shared TLC-LineDet session and its identity."""
    model, info = _load("line", "TLC-LineDet", Path(settings.LINE_MODEL_PATH), LineONNX)
    return model, info
