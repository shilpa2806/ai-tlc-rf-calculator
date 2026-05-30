# circle_detector/line_onnx.py
from __future__ import annotations

from dataclasses import dataclass
from typing import List, Dict, Any, Optional

import cv2
import numpy as np

try:
    import onnxruntime as ort
except Exception as e:
    raise RuntimeError(
        "onnxruntime is required for ONNX inference. Install with: pip install onnxruntime"
    ) from e


@dataclass
class Det:
    x1: float
    y1: float
    x2: float
    y2: float
    conf: float
    cls: int = 0


class LineONNX:
    """
    Minimal ONNX wrapper similar to SpotONNX.
    Assumes YOLO-style ONNX output. If your exported model output shape differs,
    we will adjust decode logic accordingly.
    """

    def __init__(self, onnx_path: str, imgsz: int = 1024, providers: Optional[list] = None):
        self.onnx_path = onnx_path
        self.imgsz = int(imgsz)

        if providers is None:
            providers = ["CPUExecutionProvider"]

        self.sess = ort.InferenceSession(onnx_path, providers=providers)
        self.input_name = self.sess.get_inputs()[0].name
        self.output_name = self.sess.get_outputs()[0].name

    def _letterbox(self, im: np.ndarray, new_size: int):
        h, w = im.shape[:2]
        scale = min(new_size / h, new_size / w)
        nh, nw = int(round(h * scale)), int(round(w * scale))

        resized = cv2.resize(im, (nw, nh), interpolation=cv2.INTER_LINEAR)
        canvas = np.full((new_size, new_size, 3), 114, dtype=np.uint8)

        top = (new_size - nh) // 2
        left = (new_size - nw) // 2
        canvas[top:top + nh, left:left + nw] = resized
        return canvas, scale, left, top

    def _preprocess(self, bgr: np.ndarray):
        img, scale, pad_x, pad_y = self._letterbox(bgr, self.imgsz)
        rgb = cv2.cvtColor(img, cv2.COLOR_BGR2RGB)
        x = rgb.astype(np.float32) / 255.0
        x = np.transpose(x, (2, 0, 1))[None, ...]  # (1,3,H,W)
        return x, scale, pad_x, pad_y

    def _nms(self, boxes, scores, iou_th=0.45):
        x1 = boxes[:, 0]; y1 = boxes[:, 1]; x2 = boxes[:, 2]; y2 = boxes[:, 3]
        areas = (x2 - x1 + 1) * (y2 - y1 + 1)
        order = scores.argsort()[::-1]
        keep = []
        while order.size > 0:
            i = order[0]
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

    def predict(self, bgr: np.ndarray, conf: float = 0.25, iou: float = 0.45) -> List[Dict[str, Any]]:
        x, scale, pad_x, pad_y = self._preprocess(bgr)
        out = self.sess.run([self.output_name], {self.input_name: x})[0]

        # Common YOLOv8 ONNX: (1,84,8400) or (1,8400,84)
        out = np.squeeze(out)

        if out.ndim != 2:
            raise RuntimeError(f"Unexpected ONNX output shape: {out.shape}")

        if out.shape[0] < out.shape[1]:
            out = out.T  # make it (N, C)

        # out: (N, 4 + num_classes)
        boxes = out[:, :4]
        scores_all = out[:, 4:]
        cls = np.argmax(scores_all, axis=1)
        scores = scores_all[np.arange(scores_all.shape[0]), cls]

        # Filter by confidence
        m = scores >= float(conf)
        boxes = boxes[m]
        scores = scores[m]
        cls = cls[m]

        if boxes.shape[0] == 0:
            return []

        # xywh -> xyxy
        xywh = boxes.copy()
        xyxy = np.zeros_like(xywh)
        xyxy[:, 0] = xywh[:, 0] - xywh[:, 2] / 2  # x1
        xyxy[:, 1] = xywh[:, 1] - xywh[:, 3] / 2  # y1
        xyxy[:, 2] = xywh[:, 0] + xywh[:, 2] / 2  # x2
        xyxy[:, 3] = xywh[:, 1] + xywh[:, 3] / 2  # y2

        # NMS
        keep = self._nms(xyxy, scores, iou_th=float(iou))
        xyxy = xyxy[keep]
        scores = scores[keep]
        cls = cls[keep]

        # map back from letterbox to original image
        # undo padding, undo scaling
        xyxy[:, [0, 2]] -= pad_x
        xyxy[:, [1, 3]] -= pad_y
        xyxy /= scale

        H, W = bgr.shape[:2]
        xyxy[:, 0] = np.clip(xyxy[:, 0], 0, W - 1)
        xyxy[:, 1] = np.clip(xyxy[:, 1], 0, H - 1)
        xyxy[:, 2] = np.clip(xyxy[:, 2], 0, W - 1)
        xyxy[:, 3] = np.clip(xyxy[:, 3], 0, H - 1)

        dets = []
        for (x1, y1, x2, y2), s, c in zip(xyxy, scores, cls):
            dets.append({"x1": float(x1), "y1": float(y1), "x2": float(x2), "y2": float(y2), "conf": float(s), "cls": int(c)})
        return dets