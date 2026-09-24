# -*- coding: utf-8 -*-
r"""真机复核探针：切换微信会话（先激活微信窗口，再单击会话行），并回报真实结果。

为什么需要它：直接 mouse_event 点会话行时，若前台窗口不是微信（例如跑脚本的控制台），
点击会被前台窗口吃掉 —— 实测第一轮 flow 里点 (1379,775) 后会话**没变**。
这里先用 AttachThreadInput 抢前台，再点。

用法：
  python tests/dev_rm2_switch.py                     # 只快照（微信标题 + 面板标题/左列）
  python tests/dev_rm2_switch.py click X Y           # 激活微信 → 单击 → 快照
  python tests/dev_rm2_switch.py clickrow <片段>      # OCR 找行 → 激活 → 单击 → 快照
"""
from __future__ import annotations

import ctypes
import os
import sys
import time

PROJECT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PROJECT_DIR)

import mss          # noqa: E402
import numpy as np  # noqa: E402
import win32gui     # noqa: E402
from PIL import Image                       # noqa: E402
from rapidocr_onnxruntime import RapidOCR   # noqa: E402

from wechat_triage_hud import wechat_window as ww  # noqa: E402

u32 = ctypes.windll.user32
CLS = "Qt692QWindowToolSaveBits"
WX = (1240, 21, 240, 903)
_OCR = RapidOCR()


def grab(l, t, w, h):
    with mss.MSS() as s:
        return np.array(s.grab({"left": int(l), "top": int(t),
                                "width": int(w), "height": int(h)}))[:, :, :3][:, :, ::-1]


def ocr(l, t, w, h, scale=3.0):
    img = Image.fromarray(grab(l, t, w, h))
    if scale != 1.0:
        img = img.resize((int(img.width * scale), int(img.height * scale)), Image.LANCZOS)
    res, _ = _OCR(np.array(img.convert("RGB")))
    return [(txt, int(l + (min(p[0] for p in box) + max(p[0] for p in box)) / 2 / scale),
             int(t + (min(p[1] for p in box) + max(p[1] for p in box)) / 2 / scale))
            for box, txt, _ in (res or [])]


def panel():
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


def activate(hwnd):
    u32.ShowWindow(hwnd, 9)                      # SW_RESTORE
    ft = u32.GetForegroundWindow()
    t1 = u32.GetWindowThreadProcessId(ft, None)
    t2 = ctypes.windll.kernel32.GetCurrentThreadId()
    u32.AttachThreadInput(t1, t2, True)
    u32.BringWindowToTop(hwnd)
    ok = u32.SetForegroundWindow(hwnd)
    u32.AttachThreadInput(t1, t2, False)
    time.sleep(0.5)
    fg = u32.GetForegroundWindow()
    return ok, fg, win32gui.GetWindowText(fg)


def click(x, y):
    u32.SetCursorPos(int(x), int(y))
    time.sleep(0.15)
    u32.mouse_event(0x0002, 0, 0, 0, 0)
    time.sleep(0.07)
    u32.mouse_event(0x0004, 0, 0, 0, 0)


def selected_row(rows, x=1255):
    """微信列表里被选中的会话行背景是绿色 → 用真实像素判断「当前打开的是哪一行」。"""
    hits = []
    for t_, cx, cy in rows:
        px = grab(x, cy, 1, 1)[0][0]
        r, g, b = int(px[0]), int(px[1]), int(px[2])
        if g > r + 25 and g > b + 25:
            hits.append((t_, cx, cy, (r, g, b)))
    return hits


def snapshot(label=""):
    w = ww.find_main()
    pr = panel()
    print(f"--- 快照 {label} ---")
    rows = ocr(*WX, 2.0)
    sel = selected_row(rows)
    print(f"  微信列表 OCR {len(rows)} 条；绿色高亮行：{sel}")
    print(f"  微信 hwnd={w.hwnd} title={w.title!r} rect={w.rect}")
    ttl = ocr(1560, 85, 310, 40, 3.0)
    print(f"  微信消息区标题：{[a for a, x, y in ttl]}")
    if not pr:
        print("  面板：无可见窗口")
        return
    l, t, r, b = pr
    head = ocr(l - 4, t - 4, r - l + 8, 34, 3.0)
    left = ocr(l - 4, t + 90, 260, 150, 3.0)
    print(f"  面板 rect={pr}")
    print(f"  面板标题栏：{[f'{a}({x},{y})' for a, x, y in head]}")
    print(f"  面板左列：{[f'{a}({x},{y})' for a, x, y in left]}")


def main():
    a = sys.argv[1:]
    w = ww.find_main()
    if not w:
        print("找不到微信窗口")
        return 1
    if not a:
        snapshot("现状")
        return 0
    if a[0] == "click":
        x, y = int(a[1]), int(a[2])
        print(f"激活微信：{activate(w.hwnd)}")
        click(x, y)
        time.sleep(2.5)
        snapshot(f"点击 ({x},{y}) 后")
        return 0
    if a[0] == "clickrow":
        frag = a[1]
        rows = ocr(*WX, 2.0)
        cand = [(t_, x, y) for t_, x, y in rows if frag in t_]
        print(f"微信列表 OCR {len(rows)} 条；匹配 {frag!r}：{cand}")
        if not cand:
            return 1
        t_, x, y = cand[0]
        print(f"激活微信：{activate(w.hwnd)}")
        time.sleep(0.4)
        click(x, y)
        time.sleep(2.5)
        snapshot(f"点击行 {t_!r} ({x},{y}) 后")
        return 0
    print("未知子命令")
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
