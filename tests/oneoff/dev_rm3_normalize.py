# -*- coding: utf-8 -*-
"""RM3 环境归一（只动真实窗口位置/还原态，不碰产品代码）。

为什么需要：复核开始时实测到两个**环境**问题，会让步骤 3/4 假失败：
  1) 微信主窗口处于最小化（GetWindowRect = -32000）→ hud.follow() 按设计隐藏面板；
  2) 微信主窗口的中央/右列采样点被一个**不可见**的 Weixin 顶层窗（class
     Qt51514QWindowIcon, title 'Weixin'）盖住 → ww.occlusion() 返回 True
     → 扫描器跳过本轮，不产生审计记录。

本脚本只做：
  · 还原（SW_RESTORE）微信主窗口；
  · 把它摆回 rm2 基线的位置/尺寸 (1240,21,1897,924)，与 rm2 证据可比；
  · 若仍被那个不可见窗盖住，用 SetWindowPos 把**那个不可见窗**移开（不改尺寸、
     不激活、不动微信本体），再复核 occlusion()。
每一步都打印实测值。
"""
from __future__ import annotations

import ctypes
import ctypes.wintypes as wt
import os
import sys
import time

PROJ = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PROJ)

import win32con    # noqa: E402
import win32gui    # noqa: E402

from wechat_triage_hud import wechat_window as ww  # noqa: E402

u32 = ctypes.WinDLL("user32")      # 私有实例：绝不能给 ctypes.windll.user32 设 argtypes
# 为什么不用 ctypes.windll.user32：那个实例是**进程内共享**的（产品 wechat_window
# 也用同一个），一旦给它设 argtypes/restype，同一进程里产品的
# `_u32.WindowFromPoint(_POINT(x, y))` 就会抛
#   ctypes.ArgumentError: expected POINT instance instead of _POINT
# —— 实测踩到过（探针脚本把产品调用打崩，看着像产品缺陷）。私有实例各管各的签名。
# 显式签名解决另一件事：不声明 restype 时窗口句柄按 32 位截断。
u32.WindowFromPoint.argtypes = [wt.POINT]
u32.WindowFromPoint.restype = wt.HWND
u32.SetWindowPos.argtypes = [wt.HWND, wt.HWND, ctypes.c_int, ctypes.c_int,
                             ctypes.c_int, ctypes.c_int, ctypes.c_uint]
u32.SetWindowPos.restype = ctypes.c_int
u32.GetWindow.argtypes = [wt.HWND, ctypes.c_uint]
u32.GetWindow.restype = wt.HWND
u32.GetWindowThreadProcessId.argtypes = [wt.HWND, ctypes.POINTER(ctypes.c_ulong)]
u32.GetWindowThreadProcessId.restype = ctypes.c_ulong
# rm2 基线是**整窗** 657x903：微信 rect=(1240,21,1897,924)
BASELINE = (1240, 21, 657, 903)


def activate(hwnd):
    """把微信推到前台。

    为什么必需：实测复核开始时整个微信被一个**最大化的 Chrome 窗口**
    （class=Chrome_WidgetWin_1, rect=(-8,-8,1928,1048)）盖住 → occlusion 4/4
    → 扫描器按设计跳过，不会产生任何审计记录。这是真机复核的**前置条件**，
    不是产品缺陷（产品拒绝读被遮挡的像素是正确行为）。
    手法与 rm2 的 dev_rm2_flow_4to7.py 一致（AttachThreadInput + SetForegroundWindow），
    但句柄一律走 pywin32（64 位安全）。
    """
    win32gui.ShowWindow(hwnd, win32con.SW_RESTORE)
    ft = win32gui.GetForegroundWindow()
    t1 = ctypes.windll.user32.GetWindowThreadProcessId(ft, None)
    t2 = ctypes.windll.kernel32.GetCurrentThreadId()
    ctypes.windll.user32.AttachThreadInput(t1, t2, True)
    try:
        win32gui.BringWindowToTop(hwnd)
        win32gui.SetForegroundWindow(hwnd)
    except Exception as e:
        print(f"      SetForegroundWindow 异常 {type(e).__name__}: {e}")
    ctypes.windll.user32.AttachThreadInput(t1, t2, False)
    time.sleep(0.6)
    return win32gui.GetForegroundWindow() == hwnd


JSON_PATH = os.path.join(PROJ, "out", "verify_20260923", "rm3_env_normalize.json")


def restore_saved():
    """把被本脚本移走的隐藏窗放回原位（收尾用）。"""
    import json
    if not os.path.exists(JSON_PATH):
        print("没有需要还原的记录")
        return 0
    d = json.load(open(JSON_PATH, encoding="utf-8"))
    for hwnd_s, rect in (d.get("moved_windows") or {}).items():
        hwnd = int(hwnd_s)
        if not win32gui.IsWindow(hwnd):
            print(f"hwnd={hwnd} 已不存在，跳过")
            continue
        l, t, r, b = rect
        u32.SetWindowPos(hwnd, 0, l, t, r - l, b - t, 0x0004 | 0x0010)
        time.sleep(0.4)
        print(f"hwnd={hwnd} 还原 rect={win32gui.GetWindowRect(hwnd)}（期望 {rect}）")
    return 0


def report(tag):
    w = ww.find_main()
    if not w:
        print(f"[{tag}] 找不到微信主窗口")
        return None
    occ, why = ww.occlusion(w.hwnd)
    print(f"[{tag}] hwnd={w.hwnd} title={w.title!r} rect={w.rect} "
          f"minimized={w.minimized} visible={w.visible}")
    print(f"[{tag}] occlusion={occ}  {why or '未被遮挡'}")
    return w


def coverers(rect):
    l, t, r, b = rect
    w, h = r - l, b - t
    pts = [("标题栏", l + w // 2, t + 12), ("消息区中心", l + w // 2, t + h // 2),
           ("左列", l + int(w * 0.15), t + h // 2),
           ("右列上方", l + int(w * 0.75), t + int(h * 0.35))]
    out = {}
    for name, x, y in pts:
        hh = u32.WindowFromPoint(wt.POINT(x, y))
        root = ww._root_of(hh) if hh else 0
        out[name] = (root, (x, y))
    return out


def main():
    print("=== RM3 环境归一 " + time.strftime("%Y-%m-%d %H:%M:%S") + " ===")
    if len(sys.argv) > 1 and sys.argv[1] == "restore":
        return restore_saved()
    saved = {}
    w = report("初始")
    if not w:
        return 1

    if w.minimized:
        print("→ 微信最小化，SW_RESTORE")
        win32gui.ShowWindow(w.hwnd, win32con.SW_RESTORE)
        time.sleep(1.5)

    cur = ww.find_main()
    l, t, r, b = cur.rect
    if (l, t, r - l, b - t) != BASELINE:
        print(f"→ 摆回 rm2 基线 {BASELINE}（当前 {l},{t},{r-l},{b-t}）")
        win32gui.MoveWindow(cur.hwnd, BASELINE[0], BASELINE[1],
                            BASELINE[2], BASELINE[3], True)
        time.sleep(1.5)
    w = report("归一后")

    # 把微信推到前台，先解决"被别的窗口整块盖住"（实测是一个最大化 Chrome）。
    print("→ 尝试把微信推到前台（前置条件：扫描器要求微信不被遮挡）")
    ok = activate(w.hwnd)
    print(f"      GetForegroundWindow == 微信 hwnd ? {ok}")
    w = report("推到前台后")

    cover = coverers(w.rect)
    interesting = {n: v for n, v in cover.items() if v[0] != w.hwnd}
    for name, (root, pt) in interesting.items():
        try:
            cls = win32gui.GetClassName(root)
            title = win32gui.GetWindowText(root)
            rect = win32gui.GetWindowRect(root)
            cloaked = ctypes.c_int(0)
            ctypes.windll.dwmapi.DwmGetWindowAttribute(
                root, 14, ctypes.byref(cloaked), ctypes.sizeof(cloaked))
        except Exception as e:
            print(f"  ??  {name} {pt} -> root={root} 查询失败 {type(e).__name__}: {e}")
            continue
        print(f"  !!  {name} {pt} -> 覆盖者 hwnd={root} class={cls!r} "
              f"title={title[:16]!r} rect={rect} "
              f"IsWindowVisible={win32gui.IsWindowVisible(root)} cloaked={cloaked.value} "
              f"IsIconic={win32gui.IsIconic(root)} owner={u32.GetWindow(root, 4)}")
        if cloaked.value or not win32gui.IsWindowVisible(root):
            # 只处理"用户根本看不见、却被 WindowFromPoint 算成遮挡"的窗：
            # 把它移出屏幕并记下原位置，收尾用 restore 还原。
            orig = tuple(rect)
            print(f"      → 不可见/cloaked 却被算作遮挡；移出屏幕，原 rect={orig} 已记录")
            saved.setdefault("moved_windows", {})[str(root)] = list(orig)
            u32.SetWindowPos(root, None, -30000, -30000, 0, 0,
                             0x0001 | 0x0004 | 0x0010)   # NOSIZE|NOZORDER|NOACTIVATE
            time.sleep(0.8)
        else:
            print(f"      → 是**用户可见**的窗口，不动它（这是真机前置条件，非产品问题）")

    w = report("处理覆盖者后")
    import json
    os.makedirs(os.path.dirname(JSON_PATH), exist_ok=True)
    saved["wechat_rect"] = list(w.rect)
    saved["foreground_ok"] = bool(ok)
    saved["occlusion"] = list(ww.occlusion(w.hwnd))
    saved["at"] = time.strftime("%Y-%m-%d %H:%M:%S")
    with open(JSON_PATH, "w", encoding="utf-8") as f:
        json.dump(saved, f, ensure_ascii=False, indent=2)
    print(f"记录 → {JSON_PATH}")
    return 0 if not ww.occlusion(w.hwnd)[0] else 1


if __name__ == "__main__":
    raise SystemExit(main())
