# -*- coding: utf-8 -*-
"""一次性：对屏幕区域做 OCR，打印 文本→屏幕坐标（定位按钮用）。"""
import os
import sys
import traceback

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

x, y, w, h = (int(v) for v in sys.argv[1:5])
scale = int(sys.argv[5]) if len(sys.argv) > 5 else 2
out = []

try:
    import mss
    import numpy as np
    import cv2
    from rapidocr_onnxruntime import RapidOCR

    with mss.MSS() as s:
        img = np.array(s.grab({"left": x, "top": y, "width": w, "height": h}))[:, :, :3][:, :, ::-1]
    if scale != 1:
        img = cv2.resize(img, (img.shape[1] * scale, img.shape[0] * scale),
                         interpolation=cv2.INTER_CUBIC)
    res, _ = RapidOCR()(img)
    out.append(f"检测到 {0 if not res else len(res)} 个文本块")
    for item in (res or []):
        box, txt = item[0], item[1]
        try:
            score = float(item[2])
        except (IndexError, TypeError, ValueError):
            score = -1.0
        xs = [p[0] for p in box]
        ys = [p[1] for p in box]
        cx = int(x + (min(xs) + max(xs)) / 2 / scale)
        cy = int(y + (min(ys) + max(ys)) / 2 / scale)
        out.append(f"  ({cx},{cy})  conf={score:.2f}  {txt}")
except Exception:
    out.append("EXC:\n" + traceback.format_exc())

with open(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                       "out", "_ocr_out.txt"), "w", encoding="utf-8") as f:
    f.write("\n".join(out))
print("\n".join(out))
