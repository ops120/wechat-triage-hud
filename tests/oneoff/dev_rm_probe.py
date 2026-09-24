# -*- coding: utf-8 -*-
"""真机复核用的探针（只读，不改产品代码）。

用法：
  python tests/dev_rm_probe.py win            # 列出 HUD 窗口 + 微信窗口 rect/遮挡
  python tests/dev_rm_probe.py shot <out>     # 整屏截图（1920x1080）
  python tests/dev_rm_probe.py click X Y      # 真鼠标单击
  python tests/dev_rm_probe.py dbg            # 打印 hud_debug.log 末尾
"""
from __future__ import annotations

import ctypes
import os
import sys
import time

PROJECT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PROJECT_DIR)

from wechat_triage_hud.paths import DEBUG_PATH  # noqa: E402

import win32gui  # noqa: E402

from wechat_triage_hud import wechat_window as ww  # noqa: E402

u32 = ctypes.windll.user32
CLS = "Qt692QWindowToolSaveBits"


def hud_wins():
    out = []

    def cb(h, _):
        try:
            if win32gui.IsWindowVisible(h) and win32gui.GetClassName(h) == CLS:
                l, t, r, b = win32gui.GetWindowRect(h)
                out.append((h, l, t, r, b, r - l, b - t))
        except Exception:
            pass
        return True

    win32gui.EnumWindows(cb, None)
    return out


def win():
    w = ww.find_main()
    print("--- HUD 窗口（class=%s，仅可见）---" % CLS)
    hs = hud_wins()
    if not hs:
        print("  （无）")
    for h, l, t, r, b, wd, ht in hs:
        print(f"  hwnd={h} rect=({l},{t},{r},{b}) size={wd}x{ht}")
    print("--- 微信 ---")
    if not w:
        print("  未找到")
        return 1
    print(f"  hwnd={w.hwnd} title={w.title!r} rect={w.rect} "
          f"min={w.minimized} vis={w.visible}")
    print(f"  occlusion={ww.occlusion(w.hwnd)}")
    if hs:
        h, l, t, r, b, wd, ht = max(hs, key=lambda x: x[5] * x[6])
        print(f"  最大 HUD 窗口 vs 微信左上角: dx={l - w.rect[0]} "
              f"dy={t - w.rect[1]}  （面板形态时 dx 应≈0）")
    return 0


def shot(out):
    import mss
    with mss.MSS() as s:
        img = s.grab({"left": 0, "top": 0, "width": 1920, "height": 1080})
        s.shot(mon=0, output=out) if False else None
    import numpy as np
    from PIL import Image
    arr = np.array(img)[:, :, :3][:, :, ::-1]     # BGRA -> RGB
    Image.fromarray(arr).save(out)
    print(f"截图 → {out}  {arr.shape}")
    return 0


def crop(x, y, w, h, out, scale=2):
    import mss
    import numpy as np
    from PIL import Image
    with mss.MSS() as s:
        img = np.array(s.grab({"left": int(x), "top": int(y),
                               "width": int(w), "height": int(h)}))[:, :, :3][:, :, ::-1]
    im = Image.fromarray(img)
    if int(scale) != 1:
        im = im.resize((im.width * int(scale), im.height * int(scale)), Image.LANCZOS)
    im.save(out)
    print(f"裁剪 → {out}  {im.size}")
    return 0


def ocr(x, y, w, h, scale=2):
    """对屏幕上某区域做 OCR，打印「文本 → 屏幕坐标中心」，用于定位按钮。"""
    import mss
    import numpy as np
    from rapidocr_onnxruntime import RapidOCR
    with mss.MSS() as s:
        img = np.array(s.grab({"left": int(x), "top": int(y),
                               "width": int(w), "height": int(h)}))[:, :, :3][:, :, ::-1]
    if int(scale) != 1:
        import cv2
        img = cv2.resize(img, (img.shape[1] * int(scale), img.shape[0] * int(scale)),
                         interpolation=cv2.INTER_CUBIC)
    res, _ = RapidOCR()(img)
    for box, txt, score in (res or []):
        xs = [p[0] for p in box]
        ys = [p[1] for p in box]
        cx = int(x + (min(xs) + max(xs)) / 2 / int(scale))
        cy = int(y + (min(ys) + max(ys)) / 2 / int(scale))
        print(f"  ({cx},{cy})  conf={score:.2f}  {txt}")
    return 0


def logclick(x, y, out, gap=0.8):
    """两次真鼠标点击同一坐标（间隔 gap 秒），中间截一张图 —— 用于「日志」按钮双击清空。"""
    import time as _t
    click(x, y)
    _t.sleep(gap)
    crop(x - 460, y - 20, 900, 60, out, 2)
    click(x, y)
    print(f"logclick 完成（间隔 {gap}s），中途截图 {out}")
    return 0


def click(x, y):
    u32.SetCursorPos(int(x), int(y))
    time.sleep(0.25)
    u32.mouse_event(0x0002, 0, 0, 0, 0)
    time.sleep(0.08)
    u32.mouse_event(0x0004, 0, 0, 0, 0)
    print(f"click ({x},{y})")
    return 0


def click2(x, y):
    """双击（真事件，间隔极短）——用于「日志」按钮的两次点击。"""
    click(x, y)
    time.sleep(0.25)
    click(x, y)
    return 0


def dbg(n=15):
    if not os.path.exists(DEBUG_PATH):
        print("（无 debug 日志）")
        return 1
    lines = [x for x in open(DEBUG_PATH, encoding="utf-8").read().splitlines() if x.strip()]
    for x in lines[-int(n):]:
        print("  " + x)
    return 0


def main():
    if len(sys.argv) < 2:
        return win()
    c = sys.argv[1]
    if c == "win":
        return win()
    if c == "shot":
        return shot(sys.argv[2])
    if c == "click":
        return click(int(sys.argv[2]), int(sys.argv[3]))
    if c == "click2":
        return click2(int(sys.argv[2]), int(sys.argv[3]))
    if c == "logclick":
        return logclick(int(sys.argv[2]), int(sys.argv[3]), sys.argv[4],
                        float(sys.argv[5]) if len(sys.argv) > 5 else 0.8)
    if c == "crop":
        return crop(int(sys.argv[2]), int(sys.argv[3]), int(sys.argv[4]),
                    int(sys.argv[5]), sys.argv[6],
                    int(sys.argv[7]) if len(sys.argv) > 7 else 2)
    if c == "ocr":
        return ocr(int(sys.argv[2]), int(sys.argv[3]), int(sys.argv[4]),
                   int(sys.argv[5]),
                   int(sys.argv[6]) if len(sys.argv) > 6 else 2)
    if c == "dbg":
        return dbg(int(sys.argv[2]) if len(sys.argv) > 2 else 15)
    print("未知子命令")
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
