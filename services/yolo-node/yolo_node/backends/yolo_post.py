"""YOLOv8 pre/post-processing shared by the ONNX and TensorRT backends (numpy + PIL only).

Output tensor layout of a YOLOv8 detect model: (1, 4 + nc, N) with rows cx, cy, w, h, class scores (no objectness)."""
from __future__ import annotations

from typing import List, Tuple

import numpy as np
from PIL import Image

PAD_VALUE = 114
IOU_THRESHOLD = 0.45
MAX_CANDIDATES = 300


def letterbox(rgb: np.ndarray, size: int) -> Tuple[np.ndarray, float, int, int]:
    """Resize keeping aspect ratio into a size x size canvas. Returns (float32 1x3xSxS in [0,1], ratio, padx, pady)."""
    h, w = rgb.shape[:2]
    r = min(size / float(h), size / float(w))
    nw, nh = max(1, int(round(w * r))), max(1, int(round(h * r)))
    img = Image.fromarray(rgb).resize((nw, nh), Image.BILINEAR)
    canvas = np.full((size, size, 3), PAD_VALUE, dtype=np.uint8)
    px, py = (size - nw) // 2, (size - nh) // 2
    canvas[py:py + nh, px:px + nw] = np.asarray(img)
    x = canvas.transpose(2, 0, 1)[None].astype(np.float32) / 255.0
    return np.ascontiguousarray(x), r, px, py


def _nms(boxes: np.ndarray, scores: np.ndarray, iou_thr: float) -> List[int]:
    x1, y1, x2, y2 = boxes[:, 0], boxes[:, 1], boxes[:, 2], boxes[:, 3]
    areas = (x2 - x1) * (y2 - y1)
    order = scores.argsort()[::-1]
    keep: List[int] = []
    while order.size:
        i = int(order[0])
        keep.append(i)
        if order.size == 1:
            break
        rest = order[1:]
        xx1 = np.maximum(x1[i], x1[rest])
        yy1 = np.maximum(y1[i], y1[rest])
        xx2 = np.minimum(x2[i], x2[rest])
        yy2 = np.minimum(y2[i], y2[rest])
        inter = np.maximum(0.0, xx2 - xx1) * np.maximum(0.0, yy2 - yy1)
        iou = inter / (areas[i] + areas[rest] - inter + 1e-9)
        order = rest[iou <= iou_thr]
    return keep


def postprocess(out: np.ndarray, names: List[str], ratio: float, padx: int, pady: int,
                orig_w: int, orig_h: int, min_conf: float) -> List[Tuple[str, float, List[float]]]:
    """Decode a (1, 4+nc, N) (or (1, N, 4+nc)) output into [(label, conf, [x, y, w, h])] in original-image pixels,
    sorted by conf descending. Class ids outside `names` are dropped."""
    a = np.asarray(out, dtype=np.float32)
    a = a.reshape(a.shape[-2], a.shape[-1])
    nc = len(names)
    if a.shape[0] != 4 + nc and a.shape[1] == 4 + nc:
        a = a.T
    if a.shape[0] != 4 + nc:
        raise ValueError("unexpected output shape")
    preds = a.T                                   # (N, 4 + nc)
    scores = preds[:, 4:]
    cls = scores.argmax(axis=1)
    conf = scores[np.arange(scores.shape[0]), cls]
    m = conf >= min_conf
    if not m.any():
        return []
    preds, cls, conf = preds[m], cls[m], conf[m]
    if conf.size > MAX_CANDIDATES:
        top = conf.argsort()[::-1][:MAX_CANDIDATES]
        preds, cls, conf = preds[top], cls[top], conf[top]
    cx, cy, w, h = preds[:, 0], preds[:, 1], preds[:, 2], preds[:, 3]
    boxes = np.stack([cx - w / 2, cy - h / 2, cx + w / 2, cy + h / 2], axis=1)
    boxes[:, [0, 2]] = (boxes[:, [0, 2]] - padx) / ratio
    boxes[:, [1, 3]] = (boxes[:, [1, 3]] - pady) / ratio
    boxes[:, [0, 2]] = boxes[:, [0, 2]].clip(0, orig_w)
    boxes[:, [1, 3]] = boxes[:, [1, 3]].clip(0, orig_h)
    offset = cls.astype(np.float32)[:, None] * 8192.0          # class-aware NMS
    keep = _nms(boxes + offset, conf, IOU_THRESHOLD)
    res = []
    for i in keep:
        if not (0 <= int(cls[i]) < nc):
            continue
        x1, y1, x2, y2 = (float(v) for v in boxes[i])
        if x2 <= x1 or y2 <= y1:
            continue
        res.append((names[int(cls[i])], float(conf[i]), [x1, y1, x2 - x1, y2 - y1]))
    res.sort(key=lambda d: -d[1])
    return res
