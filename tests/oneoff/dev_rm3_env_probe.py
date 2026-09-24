# -*- coding: utf-8 -*-
"""RM3 环境探针（只读）：把「遮挡」判定拆到每个采样点，找出到底是谁盖住了微信。

用途：rm2 之后微信的 75%/中央采样点被一个**隐藏**的 Weixin 子窗口盖住，
occlusion() 报 True → 扫描器会跳过、不产生审计记录，步骤 3/4 会假失败。
本探针用 WindowFromPoint 逐点打印最顶层窗口（hwnd/class/title/rect/visible/pid），
并调用产品自己的 ww.occlusion() 做交叉核对。不改任何产品代码/产物。
"""
from __future__ import annotations

import ctypes
import ctypes.wintypes as wt
import os
import sys
import time

PROJ = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PROJ)

import psutil     # noqa: E402
import win32gui   # noqa: E402

from wechat_triage_hud import wechat_window as ww  # noqa: E402

u32 = ctypes.WinDLL("user32")       # 私有实例：绝不给共享的 ctypes.windll.user32 设 argtypes
# 说明：ctypes.windll.user32 是进程内共享实例，产品 wechat_window 也用同一个；
# 给它设 argtypes 会让产品的 `_u32.WindowFromPoint(_POINT(x, y))` 直接抛
# ArgumentError（实测踩到，看着像产品缺陷，其实是我探针的副作用）。
# 私有实例 + 显式 restype(HWND) 既隔离又能避免句柄被截成 32 位。
u32.WindowFromPoint.argtypes = [wt.POINT]
u32.WindowFromPoint.restype = wt.HWND
u32.GetWindow.argtypes = [wt.HWND, ctypes.c_uint]
u32.GetWindow.restype = wt.HWND
u32.GetWindowThreadProcessId.argtypes = [wt.HWND, ctypes.POINTER(ctypes.c_ulong)]
u32.GetWindowThreadProcessId.restype = ctypes.c_ulong
CLS_PANEL = "Qt692QWindowToolSaveBits"


def pname(pid):
    try:
        return psutil.Process(pid).name()
    except Exception:
        return "?"


def sample_points(rect):
    l, t, r, b = rect
    w, h = r - l, b - t
    return [("标题栏", l + w // 2, t + 12),
            ("消息区中心", l + w // 2, t + h // 2),
            ("左列", l + int(w * 0.15), t + h // 2),
            ("右列上方", l + int(w * 0.75), t + int(h * 0.35))]


def panels():
    res = []

    def cb(h, _):
        try:
            if win32gui.IsWindowVisible(h) and win32gui.GetClassName(h) == CLS_PANEL:
                res.append((h,) + tuple(win32gui.GetWindowRect(h)))
        except Exception:
            pass
        return True

    win32gui.EnumWindows(cb, None)
    return sorted(res, key=lambda x: -((x[3] - x[1]) * (x[4] - x[2])))


def main():
    t = time.strftime("%Y-%m-%d %H:%M:%S")
    print(f"=== 环境探针 {t} ===")
    r = wt.RECT()
    u32.SystemParametersInfoW(0x0030, 0, ctypes.byref(r), 0)
    print(f"workarea=({r.left},{r.top},{r.right},{r.bottom})  "
          f"screen={u32.GetSystemMetrics(0)}x{u32.GetSystemMetrics(1)}")

    ps = panels()
    print(f"可见面板 {len(ps)} 个: " + (str([x[1:] for x in ps]) if ps else "无"))

    w = ww.find_main()
    if not w:
        print("找不到微信主窗口 → 后续无法验证")
        return 1
    print(f"微信: hwnd={w.hwnd} title={w.title!r} rect={w.rect} "
          f"minimized={w.minimized} visible={w.visible}")

    occ, why = ww.occlusion(w.hwnd)
    print(f"产品 occlusion() -> {occ}  {why}")

    print("\n逐采样点（产品用的是 w.rect，与下面同一套坐标）：")
    bad = 0
    for name, x, y in sample_points(w.rect):
        h = u32.WindowFromPoint(wt.POINT(x, y))
        root = ww._root_of(h) if h else 0
        if not root:
            print(f"  {name:6s} ({x},{y}) -> 无窗口")
            continue
        cls = win32gui.GetClassName(root)
        title = win32gui.GetWindowText(root)
        rect = win32gui.GetWindowRect(root)
        pid = ctypes.windll.user32.GetWindowThreadProcessId(root, None)
        vis = win32gui.IsWindowVisible(root)
        mark = "OK " if root == w.hwnd else "!! "
        if root != w.hwnd:
            bad += 1
        print(f"  {mark}{name:6s} ({x},{y}) -> hwnd={root} class={cls!r} "
              f"title={title[:20]!r} rect={rect} visible={vis} pid={pid} {pname(pid)}")

    # 该隐藏窗口的样式位：确认它是不是 WS_VISIBLE=0 的隐藏顶层窗
    for name, x, y in sample_points(w.rect):
        h = u32.WindowFromPoint(wt.POINT(x, y))
        root = ww._root_of(h) if h else 0
        if root and root != w.hwnd:
            style = u32.GetWindowLongW(root, -16)      # GWL_STYLE
            ex = u32.GetWindowLongW(root, -20)         # GWL_EXSTYLE
            own = u32.GetWindow(root, 4)               # GW_OWNER
            print(f"\n覆盖者 hwnd={root} style=0x{style & 0xFFFFFFFF:08X} "
                  f"WS_VISIBLE={'是' if style & 0x10000000 else '否'} "
                  f"exstyle=0x{ex & 0xFFFFFFFF:08X} owner={own} "
                  f"IsWindowVisible={win32gui.IsWindowVisible(root)}")
            break
    print(f"\n结论：{bad}/4 个采样点被判为被别的窗口覆盖（产品据此跳过扫描）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
