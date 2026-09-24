# -*- coding: utf-8 -*-
"""悬停定位：把鼠标移到 (x,y)，比较移动前后同一块区域，找出"哪个控件被高亮"。

用途：真机点击前先确认目标控件的确切矩形（OCR 只给文字中心，按钮 padding 未知）。
输出：像素差均值 + 变化区域 bbox（屏幕坐标）+ 前后两张放大截图。
"""
from __future__ import annotations

import ctypes
import os
import sys
import time

import mss
import numpy as np

u32 = ctypes.windll.user32
OUT = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "out")


def grab(l, t, w, h):
    with mss.MSS() as s:
        return np.array(s.grab({"left": l, "top": t, "width": w, "height": h}))[:, :, :3].astype(int)


def park():
    u32.SetCursorPos(5, 5)
    time.sleep(0.6)


def main():
    x, y = int(sys.argv[1]), int(sys.argv[2])
    l, t, w, h = (int(v) for v in sys.argv[3:7])
    tag = sys.argv[7] if len(sys.argv) > 7 else "hover"
    park()
    a = grab(l, t, w, h)
    u32.SetCursorPos(x, y)
    time.sleep(0.9)
    b = grab(l, t, w, h)
    park()
    d = np.abs(a - b).mean(axis=2)
    ys, xs = np.where(d > 20)
    print(f"区域 ({l},{t})+{w}x{h}  鼠标移到 ({x},{y})  像素差均值 {d.mean():.2f}")
    if len(xs):
        print(f"  高亮 bbox（屏幕坐标）: x {l + xs.min()}..{l + xs.max()} "
              f"y {t + ys.min()}..{t + ys.max()}  （{len(xs)} 像素变化）")
    else:
        print("  无任何变化 —— 该坐标不在可交互控件上（或鼠标事件没送达）")
    from PIL import Image
    Image.fromarray(a[:, :, ::-1].astype("uint8")).resize((w * 3, h * 3)).save(
        os.path.join(OUT, f"rm_{tag}_before.png"))
    Image.fromarray(b[:, :, ::-1].astype("uint8")).resize((w * 3, h * 3)).save(
        os.path.join(OUT, f"rm_{tag}_hover.png"))
    print(f"  截图 → out/rm_{tag}_before.png / out/rm_{tag}_hover.png")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
