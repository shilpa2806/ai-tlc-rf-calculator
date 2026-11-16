"""ONNX Runtime wrapper for TLC-LineDet (``line_best.onnx``).

Ported from version 1's ``circle_detector/line_onnx.py``. Letterbox,
decoding and NMS are kept as they were. Two things are now read from the model
instead of being assumed: the input size, and the class names. The exported
model declares ``{0: 'max_line', 1: 'min_line'}`` (SI Table S2); the original
view hard-coded the opposite, which was harmless only because the final
baseline/front assignment is made by vertical position.
"""

from __future__ import annotations

import ast
from typing import Any

import cv2
import numpy as np
import onnxruntime as ort

# Used only if the model carries no ``names`` metadata (SI Table S2).
DEFAULT_NAMES: dict[int, str] = {0: "max_line", 1: "min_line"}


def _class_names(sess: ort.InferenceSession) -> dict[int, str]:
    raw = sess.get_modelmeta().custom_metadata_map.get("names")
    if not raw:
        return dict(DEFAULT_NAMES)
    try:
        parsed = ast.literal_eval(raw)  # Ultralytics stores a Python dict literal
    except ValueError, SyntaxError:
        return dict(DEFAULT_NAMES)
    if not isinstance(parsed, dict):
        return dict(DEFAULT_NAMES)
    return {int(k): str(v) for k, v in parsed.items()}


class LineONNX:
    """TLC-LineDet inference session (raw YOLOv8 output, NMS done here)."""

    def __init__(
        self,
        onnx_path: str,
        imgsz: int | None = None,
        sess_options: ort.SessionOptions | None = None,
    ) -> None:
        self.onnx_path = onnx_path
        self.sess = ort.InferenceSession(
            onnx_path, sess_options=sess_options, providers=["CPUExecutionProvider"]
        )
        model_input = self.sess.get_inputs()[0]
        self.input_name = model_input.name
        self.output_name = self.sess.get_outputs()[0].name
        self.imgsz = int(imgsz or model_input.shape[-1])
        self.names = _class_names(self.sess)

    def class_id(self, name: str) -> int | None:
        """Return the class id for ``name`` (e.g. ``"min_line"``), if present."""
        return next((cid for cid, cname in self.names.items() if cname == name), None)

    def _letterbox(self, im: np.ndarray, new_size: int) -> tuple[np.ndarray, float, int, int]:
        h, w = im.shape[:2]
        scale = min(new_size / h, new_size / w)
        nh, nw = int(round(h * scale)), int(round(w * scale))

        resized = cv2.resize(im, (nw, nh), interpolation=cv2.INTER_LINEAR)
        canvas = np.full((new_size, new_size, 3), 114, dtype=np.uint8)

        top = (new_size - nh) // 2
        left = (new_size - nw) // 2
        canvas[top : top + nh, left : left + nw] = resized
        return canvas, scale, left, top

    def _preprocess(self, bgr: np.ndarray) -> tuple[np.ndarray, float, int, int]:
        img, scale, pad_x, pad_y = self._letterbox(bgr, self.imgsz)
        rgb = cv2.cvtColor(img, cv2.COLOR_BGR2RGB)
        x = rgb.astype(np.float32) / 255.0
        x = np.transpose(x, (2, 0, 1))[None, ...]  # (1,3,H,W)
        return x, scale, pad_x, pad_y

    @staticmethod
    def _nms(boxes: np.ndarray, scores: np.ndarray, iou_th: float = 0.45) -> list[int]:
        x1, y1, x2, y2 = boxes[:, 0], boxes[:, 1], boxes[:, 2], boxes[:, 3]
        areas = (x2 - x1 + 1) * (y2 - y1 + 1)
        order = scores.argsort()[::-1]
        keep: list[int] = []
        while order.size > 0:
            i = int(order[0])
            keep.append(i)
            xx1 = np.maximum(x1[i], x1[order[1:]])
            yy1 = np.maximum(y1[i], y1[order[1:]])
            xx2 = np.minimum(x2[i], x2[order[1:]])
            yy2 = np.minimum(y2[i], y2[order[1:]])
            w = np.maximum(0.0, xx2 - xx1 + 1)
            h = np.maximum(0.0, yy2 - yy1 + 1)
            inter = w * h
            iou = inter / (areas[i] + areas[order[1:]] - inter)
            inds = np.where(iou <= iou_th)[0]
            order = order[inds + 1]
        return keep

    def predict(
        self, bgr: np.ndarray, conf: float = 0.25, iou: float = 0.45
    ) -> list[dict[str, Any]]:
        """Return line-region boxes in ``bgr`` pixel coordinates.

        Each detection is ``{"x1", "y1", "x2", "y2", "conf", "cls"}``.
        """
        x, scale, pad_x, pad_y = self._preprocess(bgr)
        out = self.sess.run([self.output_name], {self.input_name: x})[0]

        # YOLOv8 raw output: (1, 4+C, N) or (1, N, 4+C).
        out = np.squeeze(out)
        if out.ndim != 2:
            raise RuntimeError(f"Unexpected ONNX output shape: {out.shape}")
        if out.shape[0] < out.shape[1]:
            out = out.T  # -> (N, 4+C)

        boxes = out[:, :4]
        scores_all = out[:, 4:]
        cls = np.argmax(scores_all, axis=1)
        scores = scores_all[np.arange(scores_all.shape[0]), cls]

        m = scores >= float(conf)
        boxes, scores, cls = boxes[m], scores[m], cls[m]
        if boxes.shape[0] == 0:
            return []

        xyxy = np.zeros_like(boxes)
        xyxy[:, 0] = boxes[:, 0] - boxes[:, 2] / 2
        xyxy[:, 1] = boxes[:, 1] - boxes[:, 3] / 2
        xyxy[:, 2] = boxes[:, 0] + boxes[:, 2] / 2
        xyxy[:, 3] = boxes[:, 1] + boxes[:, 3] / 2

        keep = self._nms(xyxy, scores, iou_th=float(iou))
        xyxy, scores, cls = xyxy[keep], scores[keep], cls[keep]

        # Undo the letterbox: remove padding, then scaling.
        xyxy[:, [0, 2]] -= pad_x
        xyxy[:, [1, 3]] -= pad_y
        xyxy /= scale

        h, w = bgr.shape[:2]
        xyxy[:, [0, 2]] = np.clip(xyxy[:, [0, 2]], 0, w - 1)
        xyxy[:, [1, 3]] = np.clip(xyxy[:, [1, 3]], 0, h - 1)

        return [
            {
                "x1": float(x1),
                "y1": float(y1),
                "x2": float(x2),
                "y2": float(y2),
                "conf": float(s),
                "cls": int(c),
            }
            for (x1, y1, x2, y2), s, c in zip(xyxy, scores, cls, strict=True)
        ]
