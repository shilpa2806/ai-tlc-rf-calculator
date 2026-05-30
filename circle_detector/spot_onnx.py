import numpy as np
import cv2
import onnxruntime as ort


def letterbox(im, new_shape=1280, color=(114, 114, 114)):
    """Resize + pad to square while keeping aspect ratio. Returns (img, r, (padw,padh))."""
    h, w = im.shape[:2]
    if isinstance(new_shape, int):
        new_shape = (new_shape, new_shape)

    r = min(new_shape[0] / h, new_shape[1] / w)
    new_unpad = (int(round(w * r)), int(round(h * r)))
    dw = new_shape[1] - new_unpad[0]
    dh = new_shape[0] - new_unpad[1]
    dw /= 2
    dh /= 2

    im_resized = cv2.resize(im, new_unpad, interpolation=cv2.INTER_LINEAR)
    top, bottom = int(round(dh - 0.1)), int(round(dh + 0.1))
    left, right = int(round(dw - 0.1)), int(round(dw + 0.1))
    im_padded = cv2.copyMakeBorder(im_resized, top, bottom, left, right, cv2.BORDER_CONSTANT, value=color)
    return im_padded, r, (left, top)


def iou_xyxy(box, boxes):
    """IoU between one box and many boxes. box: (4,), boxes: (N,4)"""
    x1 = np.maximum(box[0], boxes[:, 0])
    y1 = np.maximum(box[1], boxes[:, 1])
    x2 = np.minimum(box[2], boxes[:, 2])
    y2 = np.minimum(box[3], boxes[:, 3])

    inter = np.maximum(0, x2 - x1) * np.maximum(0, y2 - y1)
    a1 = (box[2] - box[0]) * (box[3] - box[1])
    a2 = (boxes[:, 2] - boxes[:, 0]) * (boxes[:, 3] - boxes[:, 1])
    return inter / (a1 + a2 - inter + 1e-9)


def nms_xyxy(boxes, scores, iou_thres=0.45):
    """Class-agnostic NMS."""
    idxs = scores.argsort()[::-1]
    keep = []
    while idxs.size > 0:
        i = idxs[0]
        keep.append(i)
        if idxs.size == 1:
            break
        ious = iou_xyxy(boxes[i], boxes[idxs[1:]])
        idxs = idxs[1:][ious < iou_thres]
    return keep


class SpotONNX:
    def __init__(self, onnx_path: str, imgsz: int = 1280):
        self.onnx_path = onnx_path
        self.imgsz = imgsz
        self.sess = ort.InferenceSession(onnx_path, providers=["CPUExecutionProvider"])
        self.input_name = self.sess.get_inputs()[0].name

    def predict(self, bgr: np.ndarray, conf: float = 0.15, iou: float = 0.45):
        conf = float(conf)

        # preprocess
        img, r, (padw, padh) = letterbox(bgr, self.imgsz)
        rgb = cv2.cvtColor(img, cv2.COLOR_BGR2RGB)
        x = rgb.astype(np.float32) / 255.0
        x = np.transpose(x, (2, 0, 1))[None, ...]  # 1x3xHxW

        outputs = self.sess.run(None, {self.input_name: x})
        det = outputs[0]

        # squeeze batch
        det = np.array(det)
        det = np.squeeze(det)

        if det is None or det.size == 0:
            return []

        # CASE A: Already NMS fused output -> Nx6 (x1,y1,x2,y2,score,cls)
        # Some exports return shape (1,N,6) or (N,6)
        if det.ndim == 2 and det.shape[1] == 6:
            rows = det
            dets = []
            for row in rows:
                x1, y1, x2, y2, score, cls = row[:6]
                score = float(score) if np.size(score) == 1 else float(np.max(score))
                if score < conf:
                    continue

                # undo letterbox
                x1 = (float(x1) - padw) / r
                y1 = (float(y1) - padh) / r
                x2 = (float(x2) - padw) / r
                y2 = (float(y2) - padh) / r

                dets.append({"x1": x1, "y1": y1, "x2": x2, "y2": y2, "conf": float(score), "cls": int(float(cls))})
            return dets

        # CASE B: Raw YOLO output (no NMS)
        # Typical shapes:
        # (8400, 84) or (84, 8400) or similar where first 4 = xywh, rest = class scores
        if det.ndim == 2 and det.shape[0] < det.shape[1]:
            # (C, N) -> (N, C)
            det = det.T

        if det.ndim != 2 or det.shape[1] < 6:
            # Unknown output layout
            return []

        xywh = det[:, 0:4]
        cls_scores = det[:, 4:]  # class logits/scores

        # best class per candidate
        cls_ids = np.argmax(cls_scores, axis=1)
        scores = np.max(cls_scores, axis=1)

        # filter by conf
        keep = scores >= conf
        xywh = xywh[keep]
        scores = scores[keep]
        cls_ids = cls_ids[keep]

        if xywh.shape[0] == 0:
            return []

        # xywh -> xyxy (in letterboxed image coords)
        x_c = xywh[:, 0]
        y_c = xywh[:, 1]
        w = xywh[:, 2]
        h = xywh[:, 3]
        x1 = x_c - w / 2
        y1 = y_c - h / 2
        x2 = x_c + w / 2
        y2 = y_c + h / 2
        boxes = np.stack([x1, y1, x2, y2], axis=1)

        # NMS
        keep_idx = nms_xyxy(boxes, scores, iou_thres=iou)
        boxes = boxes[keep_idx]
        scores = scores[keep_idx]
        cls_ids = cls_ids[keep_idx]

        # undo letterbox
        dets = []
        for (x1, y1, x2, y2), score, cls in zip(boxes, scores, cls_ids):
            x1 = (float(x1) - padw) / r
            y1 = (float(y1) - padh) / r
            x2 = (float(x2) - padw) / r
            y2 = (float(y2) - padh) / r
            dets.append({"x1": x1, "y1": y1, "x2": x2, "y2": y2, "conf": float(score), "cls": int(cls)})

        return dets