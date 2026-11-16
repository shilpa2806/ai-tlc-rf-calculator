"""Machine-learning-assisted candidate annotation (SI section 6).

Two YOLOv8s detectors propose editable candidates; nothing here produces a
final result. The logic is version 1's, from its ``auto_detect_spots`` and
``auto_detect_lines`` views:

* **Spots (TLC-SpotDet).** Inference runs only on the horizontal strip between
  the baseline (Min) and the solvent front (Max), widened by
  ``TLC_BAND_MARGIN`` px; candidates whose centre falls outside the band
  itself are dropped. The spot centre is the centre of the predicted box. If a
  Plate Area is set, the strip is also limited to its x-range.
* **Lines (TLC-LineDet).** The most confident box of each class is taken, and
  the two are assigned **by position** -- the lower one is the baseline, the
  upper one the solvent front -- because image geometry, not the class label,
  defines the TLC semantics. With only one line found, it is assigned by which
  half of the image it lies in and the other line is estimated at 10 % / 90 %
  of the image height. If a Plate Area is set, the model sees only that box
  (an addition to version 1: on uncropped photos with a busy background
  the model otherwise tends to put a line on the background).

All coordinates in and out are original-image pixels (after any rotation).
"""

from __future__ import annotations

from typing import Any

import numpy as np
from django.conf import settings

from .inference import line_model, spot_model


class DetectionError(ValueError):
    """A detection request that cannot be served; the message is user-facing."""


def detect_spots(
    image: np.ndarray,
    *,
    min_y: float,
    max_y: float,
    region: tuple[int, int, int, int] | None = None,
) -> dict[str, Any]:
    """Propose spot centres inside the Min/Max band.

    Args:
        image: BGR image in original-image orientation.
        min_y: Baseline y (original-image px).
        max_y: Solvent-front y (original-image px).
        region: Optional Plate Area ``(x, y, width, height)``; limits the x-range.

    Returns:
        ``{"spots": [{"x", "y", "conf", "w", "h"}], "roi": [x1, y1, x2, y2],
        "model": {...}}``, where ``w`` and ``h`` are the box size.

    Raises:
        DetectionError: If the band or the region leaves nothing to search.
    """
    height, width = image.shape[:2]
    margin = int(settings.TLC_BAND_MARGIN)

    band_top = min(min_y, max_y)
    band_bottom = max(min_y, max_y)
    y1 = max(0, int(band_top) - margin)
    y2 = min(height, int(band_bottom) + margin)
    if y2 <= y1 + 2:
        raise DetectionError("The Min and Max lines are too close together to search between.")

    x1, x2 = 0, width
    if region is not None:
        rx, _, rw, _ = region
        x1, x2 = max(0, rx), min(width, rx + rw)
        if x2 <= x1 + 2:
            raise DetectionError("The plate area is too narrow to search.")

    model, info = spot_model()
    detections = model.predict(image[y1:y2, x1:x2], conf=float(settings.SPOT_CONF_DEFAULT))

    spots = []
    for det in detections:
        cx = (float(det["x1"]) + float(det["x2"])) / 2.0 + x1
        cy = (float(det["y1"]) + float(det["y2"])) / 2.0 + y1
        # The margin only gives the model context; results must lie in the band.
        if cy < band_top or cy > band_bottom:
            continue
        # The box size lets the browser tell a second box on the same spot
        # from a neighbouring spot, whatever the photo's resolution.
        w = float(det["x2"]) - float(det["x1"])
        h = float(det["y2"]) - float(det["y1"])
        spots.append({"x": cx, "y": cy, "conf": float(det["conf"]), "w": w, "h": h})

    return {"spots": spots, "roi": [x1, y1, x2, y2], "model": info.as_dict()}


def _center_y(det: dict[str, Any]) -> float:
    return (float(det["y1"]) + float(det["y2"])) / 2.0


def assign_lines(
    detections: list[dict[str, Any]], height: int, min_cls: int | None, max_cls: int | None
) -> dict[str, Any] | None:
    """Turn raw line boxes into baseline/front positions (version 1's rules).

    Kept separate from inference so the assignment rules can be tested
    without the model.

    Returns:
        ``{"minY", "maxY", "estimated"}``, where ``estimated`` names the line
        that was guessed (``"min"``/``"max"``) or is ``None`` when both were
        detected; or ``None`` if there are no detections of either class.
    """
    best: dict[int, dict[str, Any]] = {}
    for det in detections:
        cls = det["cls"]
        if cls not in (min_cls, max_cls):
            continue
        if cls not in best or det["conf"] > best[cls]["conf"]:
            best[cls] = det

    ys = [_center_y(det) for det in best.values()]
    if not ys:
        return None
    if len(ys) >= 2:
        return {"minY": max(ys), "maxY": min(ys), "estimated": None}

    y = ys[0]
    if y < height / 2:  # upper half: the solvent front; estimate the baseline
        return {"minY": float(height * 0.90), "maxY": y, "estimated": "min"}
    return {"minY": y, "maxY": float(height * 0.10), "estimated": "max"}


def _crop(
    image: np.ndarray, region: tuple[int, int, int, int] | None
) -> tuple[np.ndarray, int, int]:
    """Crop ``image`` to ``region`` (clipped to the image); return it and its offset."""
    if region is None:
        return image, 0, 0
    height, width = image.shape[:2]
    rx, ry, rw, rh = region
    x1, y1 = max(0, rx), max(0, ry)
    x2, y2 = min(width, rx + rw), min(height, ry + rh)
    if x2 <= x1 + 2 or y2 <= y1 + 2:
        raise DetectionError("The plate area is too small to search.")
    return image[y1:y2, x1:x2], x1, y1


def detect_lines(
    image: np.ndarray, region: tuple[int, int, int, int] | None = None
) -> dict[str, Any]:
    """Propose baseline (Min) and solvent-front (Max) positions.

    Args:
        image: BGR image in original-image orientation.
        region: Optional Plate Area ``(x, y, width, height)``. Detection then
            runs on that box only, and the 10 %/90 % estimate refers to it.

    Returns:
        ``{"minY", "maxY", "estimated", "model"}`` in original-image px.

    Raises:
        DetectionError: If the model finds neither line.
    """
    view, _, y0 = _crop(image, region)
    model, info = line_model()
    detections = model.predict(view, conf=float(settings.LINE_CONF_DEFAULT))
    lines = assign_lines(
        detections,
        view.shape[0],
        min_cls=model.class_id("min_line"),
        max_cls=model.class_id("max_line"),
    )
    if lines is None:
        raise DetectionError("No reference lines detected. Place Min and Max manually.")
    lines["minY"] += y0
    lines["maxY"] += y0
    return {**lines, "model": info.as_dict()}
