# -*- coding: utf-8 -*-
r"""真机复核 RM2 · 定位器（只读）：对屏幕某区域做 OCR，打印「文本 → 屏幕中心坐标」。

为什么不直接用 tests/dev_rm_probe.py ocr：它 scale!=1 时 `import cv2`，
本机没装 cv2（见 rm2_cv2_probe.log），于是抛 ImportError。这里改用 PIL 放大。

用法：
  python tests/dev_rm2_locate.py                      # 默认：面板整块
  python tests/dev_rm2_locate.py 1240 21 240 903      # 微信左侧会话列表
  python tests/dev_rm2_locate.py X Y W H [scale]
"""
from __future__ import annotations

import os
import sys

PROJECT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PROJECT_DIR)

import win32gui  # noqa: E402
from PIL import Image  # noqa: E402

CLS = "Qt692QWindowToolSaveBits"


def panel_rect():
    out = []

    def cb(h, _):
        try:
            if win32gui.GetClassName(h) == CLS and win32gui.IsWindowVisible(h):
                out.append(tuple(win32gui.GetWindowRect(h)))
        except Exception:
            pass
        return True

    win32gui.EnumWindows(cb, None)
    return max(out, key=lambda r: (r[2] - r[0]) * (r[3] - r[1])) if out else None


def grab(l, t, w, h):
    import mss
    import numpy as np
    with mss.MSS() as s:
        return np.array(s.grab({"left": int(l), "top": int(t),
                                "width": int(w), "height": int(h)}))[:, :, :3][:, :, ::-1]


def main():
    argv = sys.argv[1:]
    if len(argv) >= 4:
        l, t, w, h = (int(v) for v in argv[:4])
        scale = float(argv[4]) if len(argv) > 4 else 2.0
        tag = f"({l},{t})+{w}x{h}"
    else:
        pr = panel_rect()
        if not pr:
            print("找不到可见面板窗口（先启动 hud）")
            return 1
        l, t, r, b = pr
        l, t, w, h = l - 4, t - 4, r - l + 8, b - t + 8
        scale = 3.0
        tag = f"面板 {pr}"

    img = Image.fromarray(grab(l, t, w, h))
    if scale != 1.0:
        img = img.resize((int(img.width * scale), int(img.height * scale)), Image.LANCZOS)

    import numpy as np
    from rapidocr_onnxruntime import RapidOCR
    # RapidOCR 只吃 ndarray/str/bytes（实测传 PIL.Image 会 LoadImageError）
    res, _ = RapidOCR()(np.array(img.convert("RGB")))
    print(f"--- OCR {tag} scale={scale} 区域({l},{t})+{w}x{h} ---")
    out = []
    for box, txt, score in (res or []):
        xs = [p[0] for p in box]
        ys = [p[1] for p in box]
        cx = int(l + (min(xs) + max(xs)) / 2 / scale)
        cy = int(t + (min(ys) + max(ys)) / 2 / scale)
        out.append((cy, cx, txt, score))
    for cy, cx, txt, score in sorted(out):
        try:
            s = f"{float(score):.2f}"
        except Exception:
            s = str(score)
        print(f"  center=({cx},{cy})  conf={s}  {txt}")
    print(f"  共 {len(out)} 条")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
