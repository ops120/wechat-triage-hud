# -*- coding: utf-8 -*-
r"""RM3 补充：把 dev_p0_verify.py 的 A/B/C/D 在真机上**独立**再跑一遍。

为什么需要单独一个脚本（不是想替换 dev_p0_verify.py）：
  `dev_p0_verify.py` 现在会在 [B] 段抛
      AttributeError: 'tuple' object has no attribute 'bottom'
  ——它把 `screen_rect()` 返回的 **tuple** 传给了按 `wintypes.RECT` 写的
  `expect_hud_xy(..., scr)`。脚本就此中断，[C]/[D] 一行都没跑。
  这不是"结果失败"，是**脚本自身的类型错误**，因此不能把 p0 的 FAIL 记成产品缺陷，
  也不能拿"p0 绿了"当通过。这里用**同一套三种摆放模式公式 + 正确的 RECT 类型**
  在真机上重算期望位置，同时把 A/C/D 用真鼠标重跑，作为独立证据。

真机动作全部为：真鼠标点击 / 真滚轮 / 真右键菜单 / 真进程枚举。不改产品代码。
用法：python .tests\dev_rm3_p0_acd.py
"""
from __future__ import annotations

import ctypes
import ctypes.wintypes as wt
import os
import sys
import time

PROJECT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PROJECT_DIR)

import mss          # noqa: E402
import numpy as np  # noqa: E402
import psutil       # noqa: E402
import win32gui     # noqa: E402

from wechat_triage_hud.paths import DEBUG_PATH  # noqa: E402
from wechat_triage_hud import wechat_window as ww   # noqa: E402

u32 = ctypes.windll.user32
CLS = "Qt692QWindowToolSaveBits"
GAP = 6
DBG = DEBUG_PATH
fails = []


def check(name, ok, detail=""):
    print(f"  {'OK  ' if ok else 'FAIL'} {name}" + (f"  [{detail}]" if detail else ""), flush=True)
    if not ok:
        fails.append(name)
    return ok


def hud_rect():
    out = []

    def cb(h, _):
        try:
            if win32gui.IsWindowVisible(h) and win32gui.GetClassName(h) == CLS:
                out.append((h,) + tuple(win32gui.GetWindowRect(h)))
        except Exception:
            pass
        return True

    win32gui.EnumWindows(cb, None)
    return max(out, key=lambda x: (x[3] - x[1]) * (x[4] - x[2])) if out else None


def real_procs():
    me = os.getpid()
    n = []
    for p in psutil.process_iter(["pid", "cmdline"]):
        try:
            if p.info["pid"] == me:
                continue
            if "wechat_triage_hud.hud" in " ".join(p.info["cmdline"] or []):
                n.append(p.info["pid"])
        except Exception:
            pass
    return n


def click(x, y):
    u32.SetCursorPos(int(x), int(y))
    time.sleep(0.3)
    u32.mouse_event(0x0002, 0, 0, 0, 0)
    time.sleep(0.06)
    u32.mouse_event(0x0004, 0, 0, 0, 0)


def right_click(x, y):
    u32.SetCursorPos(int(x), int(y))
    time.sleep(0.3)
    u32.mouse_event(0x0008, 0, 0, 0, 0)      # RIGHTDOWN
    time.sleep(0.06)
    u32.mouse_event(0x0010, 0, 0, 0, 0)      # RIGHTUP



def popup_rect():
    """找可见的弹出菜单窗口（QMenu 是独立顶层窗口）。"""
    out = []

    def cb(h, _):
        try:
            if win32gui.IsWindowVisible(h):
                c = win32gui.GetClassName(h)
                if "Popup" in c or "Menu" in c:
                    out.append((h,) + tuple(win32gui.GetWindowRect(h)))
        except Exception:
            pass
        return True

    win32gui.EnumWindows(cb, None)
    return max(out, key=lambda x: (x[3] - x[1]) * (x[4] - x[2])) if out else None


class Scr:      # noqa: N801  —— 与 wintypes.RECT 同名接口：left/top/right/bottom 方法
    def __init__(self, l, t, r, b):
        self._l, self._t, self._r, self._b = l, t, r, b

    def left(self):
        return self._l

    def top(self):
        return self._t

    def right(self):
        return self._r

    def bottom(self):
        return self._b

    def __repr__(self):
        return f"({self._l},{self._t},{self._r},{self._b})"


def screen_rect():
    """工作区。**返回带方法的对象**（p0 脚本返回的是 tuple，expect_hud_xy 却按
    RECT 调 .bottom() —— 这正是它崩在 [B] 的原因）。"""
    r = wt.RECT()
    u32.SystemParametersInfoW(0x0030, 0, ctypes.byref(r), 0)     # SPI_GETWORKAREA
    return Scr(r.left, r.top, r.right - 1, r.bottom - 1)


def expect_hud_xy(wx_rect, hud_w, hud_h, scr):
    """hud.py follow() 的三种摆放模式（下方 / 右侧 / 左侧）+ 屏幕夹取。
    与 dev_p0_verify.py 里的公式逐字一致，只换掉 scr 的类型。"""
    l, t, r, b = wx_rect
    if b + GAP + hud_h <= scr.bottom():
        return l, b + GAP, "微信下方"
    if r + GAP + hud_w <= scr.right():
        return r + GAP, t, "微信右侧"
    if l - GAP - hud_w >= scr.left():
        return l - GAP - hud_w, t, "微信左侧"
    return None, None, "三种都放不下 → 屏幕夹取"


def wheel(x, y, notches=5):
    u32.SetCursorPos(int(x), int(y))
    time.sleep(0.25)
    for _ in range(notches):
        u32.mouse_event(0x0800, 0, 0, -120, 0)
        time.sleep(0.12)


def grab(rect):
    l, t, r, b = rect
    with mss.MSS() as s:
        return np.array(s.grab({"left": l, "top": t, "width": r - l, "height": b - t}))


def dbg_lines():
    if not os.path.exists(DBG):
        return []
    return [x for x in open(DBG, encoding="utf-8").read().splitlines() if x.strip()]


def main():
    h = hud_rect()
    if not h:
        print("未找到可见的面板窗口（先启动面板：$env:JEV_HUD_DEBUG=1; "
              "python -m wechat_triage_hud.hud）")
        return 1
    hid, l, t, r, b = h
    w = ww.find_main()
    print(f"HUD hwnd={hid} rect=({l},{t},{r},{b})")
    print(f"微信 rect={w.rect}\n")
    n0 = len(dbg_lines())

    # ---------- A. 点击是否送达 ----------
    print("[A] 点击是否真的送达 HUD（看调试日志）")
    click(l + 70, t + 62)
    time.sleep(2.5)
    lines = dbg_lines()[n0:]
    got = [x for x in lines if "on_row_click" in x or "mousePress" in x]
    check("HUD 收到了点击事件", bool(got), got[0][:60] if got else "无任何事件记录")

    # ---------- B. 固定 / 取消固定（含三种摆放模式） ----------
    print("\n[B] 📌 固定按钮：固定后微信移动 HUD 应不动；取消固定后应回到吸附位")
    pin_x, pin_y = r - 79, t + 16
    click(pin_x, pin_y)
    time.sleep(1.2)
    logs = dbg_lines()[n0:]
    check("收到 set_pinned 调用", any("set_pinned" in x for x in logs),
          next((x[11:] for x in logs if "set_pinned" in x), "无"))

    wl, wt_, wr, wb = w.rect
    win32gui.MoveWindow(w.hwnd, wl + 60, wt_, wr - wl, wb - wt_, True)
    time.sleep(2.5)
    h2 = hud_rect()
    check("固定后 HUD 未跟随（位置不变）", bool(h2) and abs(h2[1] - l) <= 3,
          f"HUD x {l} -> {h2[1] if h2 else '?'}")

    click(pin_x, pin_y)               # 取消固定
    time.sleep(2.5)
    h3 = hud_rect()
    w3 = ww.find_main()
    scr = screen_rect()
    width = (h3[3] - h3[1]) if h3 else 0
    height = (h3[4] - h3[2]) if h3 else 0
    ex, ey, mode = expect_hud_xy(w3.rect, width, height, scr)
    if ex is None:
        print("  SKIP 三种摆放模式都放不下 → 本项不判定")
    else:
        ok = bool(h3) and abs(h3[1] - ex) <= 3 and abs(h3[2] - ey) <= 3
        check("取消固定后又跟随微信（三种摆放模式 + 屏幕夹取）", ok,
              f"HUD ({h3[1]},{h3[2]}) vs 期望 ({ex},{ey}) 模式={mode} "
              f"微信={w3.rect} 面板={width}x{height} 屏={scr}")
    win32gui.MoveWindow(w.hwnd, wl, wt_, wr - wl, wb - wt_, True)
    time.sleep(2.0)

    # ---------- C. 滚动（先点左列第一行让右栏有内容） ----------
    print("\n[C] 滚轮滚动右侧判断面板（先真点左列第一行）")
    h4 = hud_rect()
    _, l4, t4, r4, b4 = h4
    click(l4 + 80, t4 + 62)
    time.sleep(2.8)
    before = grab((l4, t4, r4, b4))
    wheel(r4 - 200, t4 + (b4 - t4) // 2, notches=6)
    time.sleep(0.8)
    after = grab((l4, t4, r4, b4))
    diff = float(np.abs(before.astype(int) - after.astype(int)).mean())
    check("滚动后画面确实变化", diff > 1.0, f"像素差 {diff:.2f}")

    # ---------- D. 右键菜单「退出」 ----------
    print("\n[D] 右键菜单「退出」：窗口消失 + 进程归零")
    h5 = hud_rect()
    _, l5, t5, r5, b5 = h5
    right_click(l5 + 80, t5 + 60)
    time.sleep(1.2)
    pr = popup_rect()
    check("右键菜单弹出", bool(pr), f"菜单 rect={pr[1:] if pr else None}")
    if pr:
        click(pr[1] + (pr[3] - pr[1]) // 2, pr[4] - 16)     # 末项「退出」
    time.sleep(3.5)
    check("HUD 窗口已消失（右键菜单退出）", hud_rect() is None)
    left = real_procs()
    check("无残留 hud.py 进程", not left, f"残留 {left}")

    print("\n" + "=" * 60)
    if fails:
        print("失败项：")
        for f in fails:
            print("  -", f)
    else:
        print("A/B/C/D 全部通过（本脚本口径）")
    return 1 if fails else 0


if __name__ == "__main__":
    raise SystemExit(main())

