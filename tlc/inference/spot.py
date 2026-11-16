"""ONNX Runtime wrapper for TLC-SpotDet (``spot_best.onnx``).

Ported from version 1's ``circle_detector/spot_onnx.py``. The
preprocessing (letterbox to a square, RGB, /255) and both output decoders are
kept as they were, so candidates match version 1 for the same input; only type
hints were added and the input size is now read from the model.
"""

from __future__ import annotations

from typing import Any

import cv2
import numpy as np
import onnxruntime as ort


def letterbox(
    im: np.ndarray,
    new_shape: int | tuple[int, int] = 1280,
    color: tuple[int, int, int] = (114,) * 3,
) -> tuple[np.ndarray, float, tuple[int, int]]:
    """Resize and pad to a square, keeping the aspect ratio.

    Returns:
        ``(padded_image, scale, (pad_left, pad_top))``.
    """
    h, w = im.shape[:2]
    if isinstance(new_shape, int):
        new_shape = (new_shape, new_shape)

    r = min(new_shape[0] / h, new_shape[1] / w)
    new_unpad = (int(round(w * r)), int(round(h * r)))
    dw = (new_shape[1] - new_unpad[0]) / 2
    dh = (new_shape[0] - new_unpad[1]) / 2

    im_resized = cv2.resize(im, new_unpad, interpolation=cv2.INTER_LINEAR)
    top, bottom = int(round(dh - 0.1)), int(round(dh + 0.1))
    left, right = int(round(dw - 0.1)), int(round(dw + 0.1))
    im_padded = cv2.copyMakeBorder(
        im_resized, top, bottom, left, right, cv2.BORDER_CONSTANT, value=color
    )
    return im_padded, r, (left, top)


def iou_xyxy(box: np.ndarray, boxes: np.ndarray) -> np.ndarray:
    """IoU between one box ``(4,)`` and many boxes ``(N, 4)``."""
    x1 = np.maximum(box[0], boxes[:, 0])
    y1 = np.maximum(box[1], boxes[:, 1])
    x2 = np.minimum(box[2], boxes[:, 2])
    y2 = np.minimum(box[3], boxes[:, 3])

    inter = np.maximum(0, x2 - x1) * np.maximum(0, y2 - y1)
    a1 = (box[2] - box[0]) * (box[3] - box[1])
    a2 = (boxes[:, 2] - boxes[:, 0]) * (boxes[:, 3] - boxes[:, 1])
    result: np.ndarray = inter / (a1 + a2 - inter + 1e-9)
    return result


def nms_xyxy(boxes: np.ndarray, scores: np.ndarray, iou_thres: float = 0.45) -> list[int]:
    """Class-agnostic non-maximum suppression; returns kept indices."""
    idxs = scores.argsort()[::-1]
    keep: list[int] = []
    while idxs.size > 0:
        i = int(idxs[0])
        keep.append(i)
        if idxs.size == 1:
            break
        ious = iou_xyxy(boxes[i], boxes[idxs[1:]])
        idxs = idxs[1:][ious < iou_thres]
    return keep


class SpotONNX:
    """TLC-SpotDet inference session."""

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
        # The exported graph has a fixed square input (1x3xSxS); use it unless
        # the caller overrides, so the letterbox size always matches the model.
        self.imgsz = int(imgsz or model_input.shape[-1])

    def predict(
        self, bgr: np.ndarray, conf: float = 0.25, iou: float = 0.45
    ) -> list[dict[str, Any]]:
        """Return candidate boxes in ``bgr`` pixel coordinates.

        Each detection is ``{"x1", "y1", "x2", "y2", "conf", "cls"}``.
        """
        conf = float(conf)

        img, r, (padw, padh) = letterbox(bgr, self.imgsz)
        rgb = cv2.cvtColor(img, cv2.COLOR_BGR2RGB)
        x = rgb.astype(np.float32) / 255.0
        x = np.transpose(x, (2, 0, 1))[None, ...]  # 1x3xHxW

        outputs = self.sess.run(None, {self.input_name: x})
        det = np.squeeze(np.array(outputs[0]))

        if det.size == 0:
            return []

        # Case A: NMS fused into the graph -> Nx6 (x1, y1, x2, y2, score, cls).
        # The deployed spot_best.onnx has this layout (1x300x6).
        if det.ndim == 2 and det.shape[1] == 6:
            dets = []
            for row in det:
                x1, y1, x2, y2, score, cls = row[:6]
                score = float(score) if np.size(score) == 1 else float(np.max(score))
                if score < conf:
                    continue
                dets.append(
                    {
                        "x1": (float(x1) - padw) / r,
                        "y1": (float(y1) - padh) / r,
                        "x2": (float(x2) - padw) / r,
                        "y2": (float(y2) - padh) / r,
                        "conf": float(score),
                        "cls": int(float(cls)),
                    }
                )
            return dets

        # Case B: raw YOLO output without NMS, (C, N) or (N, C) with xywh first.
        if det.ndim == 2 and det.shape[0] < det.shape[1]:
            det = det.T
        if det.ndim != 2 or det.shape[1] < 6:
            return []  # unknown layout

        xywh = det[:, 0:4]
        cls_scores = det[:, 4:]
        cls_ids = np.argmax(cls_scores, axis=1)
        scores = np.max(cls_scores, axis=1)

        keep = scores >= conf
        xywh, scores, cls_ids = xywh[keep], scores[keep], cls_ids[keep]
        if xywh.shape[0] == 0:
            return []

        boxes = np.stack(
            [
                xywh[:, 0] - xywh[:, 2] / 2,
                xywh[:, 1] - xywh[:, 3] / 2,
                xywh[:, 0] + xywh[:, 2] / 2,
                xywh[:, 1] + xywh[:, 3] / 2,
            ],
            axis=1,
        )
        keep_idx = nms_xyxy(boxes, scores, iou_thres=iou)

        return [
            {
                "x1": (float(boxes[i, 0]) - padw) / r,
                "y1": (float(boxes[i, 1]) - padh) / r,
                "x2": (float(boxes[i, 2]) - padw) / r,
                "y2": (float(boxes[i, 3]) - padh) / r,
                "conf": float(scores[i]),
                "cls": int(cls_ids[i]),
            }
            for i in keep_idx
        ]
