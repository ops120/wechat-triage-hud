# -*- coding: utf-8 -*-
r"""RM3：真点「日志」两次（最小复现步骤的第一步）。

用整面板 OCR 定位「日志 N KB」按钮中心 → 真鼠标点两次（间隔 <4s）→
打印 out/jev_audit.jsonl 的 size 变化。不动产品代码。

用法：python .tests\dev_rm3_click_log.py
"""
from __future__ import annotations

import ctypes
import os
import sys
import time

PROJ = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
EVID = os.path.join(PROJ, "out", "verify_20260923")
sys.path.insert(0, PROJ)

import mss          # noqa: E402
import numpy as np  # noqa: E402
import win32gui     # noqa: E402
from PIL import Image                       # noqa: E402
from rapidocr_onnxruntime import RapidOCR   # noqa: E402

from wechat_triage_hud.paths import AUDIT_PATH   # noqa: E402

u32 = ctypes.windll.user32
CLS = "Qt692QWindowToolSaveBits"
_OCR = RapidOCR()


def panel_bbox():
    res = []

    def cb(h, _):
        try:
            if win32gui.GetClassName(h) == CLS and win32gui.IsWindowVisible(h):
                res.append((h,) + tuple(win32gui.GetWindowRect(h)))
        except Exception:
            pass
        return True

    win32gui.EnumWindows(cb, None)
    if not res:
        return None
    _, l, t, r, b = max(res, key=lambda x: (x[3] - x[1]) * (x[4] - x[2]))
    return l, t, r, b


def size():
    return os.path.getsize(AUDIT_PATH) if os.path.exists(AUDIT_PATH) else -1


def click(x, y):
    u32.SetCursorPos(int(x), int(y))
    time.sleep(0.12)
    u32.mouse_event(0x0002, 0, 0, 0, 0)
    time.sleep(0.08)
    u32.mouse_event(0x0004, 0, 0, 0, 0)


def main() -> int:
    l, t, r, b = panel_bbox()
    print(f"面板 rect=({l},{t},{r},{b})")
    with mss.MSS() as s:
        img = Image.fromarray(np.array(s.grab(
            {"left": l - 4, "top": t - 4, "width": r - l + 8, "height": b - t + 8}))[:, :, :3])
    img2 = img.resize((img.width * 3, img.height * 3), Image.LANCZOS)
    res, _ = _OCR(np.array(img2.convert("RGB")))
    hit = None
    for box, txt, score in (res or []):
        if "日志" in txt:
            cx = int((min(p[0] for p in box) + max(p[0] for p in box)) / 2 / 3) + l - 4
            cy = int((min(p[1] for p in box) + max(p[1] for p in box)) / 2 / 3) + t - 4
            if cy < t + 60:
                hit = (cx, cy, txt)
    print(f"定位到「日志」按钮：{hit}")
    if not hit:
        print("FAIL 没读到「日志」按钮")
        return 1
    lx, ly, _ = hit
    before = size()
    click(lx, ly)
    t1 = time.time()
    time.sleep(1.8)
    click(lx, ly)
    gap = time.time() - t1
    time.sleep(1.2)
    after = size()
    print(f"真点两次间隔 {gap:.2f}s：size {before} -> {after}")
    print("RESULT " + ("PASS 审计日志已归零" if after == 0 else f"FAIL 未归零（{after}）"))
    return 0 if after == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
