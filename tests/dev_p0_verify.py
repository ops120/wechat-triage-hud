"""P0 交互验证（需屏幕解锁，现在可用）。

逐项真实验证，不看代码里有没有那行：
  A. 点击是否真的送达 HUD（先看 hud_debug.log 有没有收到事件）
  B. 📌 固定：固定后微信移动 HUD 不动；取消固定后又跟随
  C. 滚动：滚轮滚动右侧判断面板，画面是否真的变化
  D. 右键菜单「退出」：窗口消失且进程归零（✕ 已于 2026-09-23 移除）
"""
import ctypes
import ctypes.wintypes as wt
import os
import sys
import time

PROJECT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PROJECT_DIR)

from wechat_triage_hud.paths import DEBUG_PATH  # noqa: E402

import mss          # noqa: E402
import numpy as np  # noqa: E402
import psutil       # noqa: E402
import win32gui     # noqa: E402

from wechat_triage_hud import wechat_window as ww  # noqa: E402

u32 = ctypes.windll.user32
CLS = "Qt692QWindowToolSaveBits"
GAP = 6                # 与 hud.MIN_W, GAP = 640, 6 保持一致（面板与微信的缝）
DBG = DEBUG_PATH
fails = []


def check(name, ok, detail=""):
    print(f"  {'OK  ' if ok else 'FAIL'} {name}" + (f"  [{detail}]" if detail else ""))
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
    for p in psutil.process_iter(["pid", "name", "cmdline"]):
        try:
            if p.info["pid"] == me:
                continue
            if any(a.endswith("hud.py") for a in (p.info["cmdline"] or [])):
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


# HUD 相对微信有三种摆放模式（hud.py 的 follow()）：
#   ① 微信下方：x = 微信左,        y = 微信底 + GAP
#   ② 微信右侧：x = 微信右 + GAP,  y = 微信顶
#   ③ 微信左侧：x = 微信左 − GAP − HUD 宽, y = 微信顶
# 之前的期望公式只写了 ①，于是在真机（面板在微信**左侧**时）报出假失败：
# HUD 明明跟对了（527 → 587，正是 −6−707 的结果），却被判 FAIL。
# 2026-09-23 真机复核（out/verify_20260923/rm2_final_summary.log）据此修正。
def expect_hud_xy(wx_rect, hud_w, hud_h, scr):
    # scr 是 screen_rect() 的 **tuple**（l, t, r, b），不是 QRect ——
    # 早先这里写成 scr.bottom()/right()/left()，2026-09-23 真机跑就 AttributeError 崩了。
    l, t, r, b = wx_rect
    sl, st, sr, sb = scr
    if b + GAP + hud_h <= sb:
        return l, b + GAP
    if r + GAP + hud_w <= sr:
        return r + GAP, t
    if l - GAP - hud_w >= sl:
        return l - GAP - hud_w, t
    # 三种都放不下：夹进屏幕（此时位置与微信无关，不再断言跟随方向）
    return None, None


def screen_rect():
    from ctypes import wintypes
    r = wintypes.RECT()
    u32.SystemParametersInfoW(0x0030, 0, ctypes.byref(r), 0)   # SPI_GETWORKAREA
    return (r.left, r.top, r.right - 1, r.bottom - 1)


def wheel(x, y, notches=5):
    u32.SetCursorPos(int(x), int(y))
    time.sleep(0.25)
    for _ in range(notches):
        u32.mouse_event(0x0800, 0, 0, -120, 0)
        time.sleep(0.12)


def grab(rect):
    l, t, r, b = rect
    with mss.MSS() as s:
        return np.array(s.grab({"left": l, "top": t,
                                "width": r - l, "height": b - t}))


def dbg_lines():
    if not os.path.exists(DBG):
        return []
    return [x for x in open(DBG, encoding="utf-8").read().splitlines() if x.strip()]


def main():
    h = hud_rect()
    if not h:
        # 面板在"微信被遮挡/最小化"时会按设计隐藏自己（follow() → setVisible(False)）。
        # 此时裸报"HUD 未运行"会让人以为坏了 —— 先把真实原因查出来。
        print("未找到可见的面板窗口。排查：")
        try:
            w = ww.find_main()
            if not w:
                print("  · 找不到微信主窗口（未登录 / 未运行）")
            else:
                print(f"  · 微信 rect={w.rect} minimized={w.minimized} visible={w.visible}")
                occ, why = ww.occlusion(w.hwnd)
                print(f"  · 遮挡={occ}  {why[:80]}")
                if occ or w.minimized or not w.visible:
                    print("  → 面板按设计隐藏了自己（微信不可用时不显示）。")
                    print("    请让微信窗口可见且不被遮挡后重跑；这不算失败。")
                    return 0
        except Exception as e:
            print(f"  · 诊断出错: {type(e).__name__}: {e}")
        print("  → 前置条件正常但仍无面板，可能是启动失败：")
        print("    看 out/hud_debug.log（需以 JEV_HUD_DEBUG=1 启动）")
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

    # ---------- B. 固定 / 取消固定 ----------
    print("\n[B] 📌 固定按钮：固定后微信移动，HUD 应不动")
    # 按钮布局：内容右边距 13，按钮宽 20、间距 8
    #   现在标题栏右侧只有 📌 与 ▾：▾ 中心 r-23、📌 中心 r-51
    #   （✕ 2026-09-23 移除、◍ 2026-09-24 移除 —— 每删一个按钮，这里都要右移 28px，
    #     否则会点到隔壁按钮上，报一堆看不懂的失败）
    pin_x, pin_y = r - 51, t + 16
    click(pin_x, pin_y)
    time.sleep(1.2)
    logs = dbg_lines()[n0:]
    check("收到 set_pinned 调用", any("set_pinned" in x for x in logs),
          next((x[11:] for x in logs if "set_pinned" in x), "无"))

    wl, wt_, wr, wb = w.rect
    win32gui.MoveWindow(w.hwnd, wl + 60, wt_, wr - wl, wb - wt_, True)
    time.sleep(2.5)
    h2 = hud_rect()
    pinned_ok = h2 and abs(h2[1] - l) <= 3
    check("固定后 HUD 未跟随（位置不变）", bool(pinned_ok),
          f"HUD x {l} -> {h2[1] if h2 else '?'}")

    # 取消固定
    click(pin_x, pin_y)
    time.sleep(2.5)
    h3 = hud_rect()
    w3 = ww.find_main()
    # 期望位置按 follow() 的**三种摆放模式**算（下方 / 右侧 / 左侧），
    # 并把屏幕夹取算进去。真机上 HUD 常在微信左侧（微信贴着屏幕右缘时），
    # 只按"下方模式"算会报假失败（2026-09-23 真机复核实测）。
    scr = screen_rect()
    width = (h3[3] - h3[1]) if h3 else 0
    height = (h3[4] - h3[2]) if h3 else 0
    ex, ey = expect_hud_xy(w3.rect, width, height, scr)
    if ex is None:
        print("  SKIP 三种摆放模式都放不下，位置由屏幕夹取决定 → 本项不判定")
    else:
        follow_ok = h3 and abs(h3[1] - ex) <= 3 and abs(h3[2] - ey) <= 3
        mode = ("下方" if (ey == w3.rect[3] + GAP) else
                "右侧" if (ex == w3.rect[2] + GAP) else "左侧")
        check("取消固定后又跟随微信（含屏幕夹取）", bool(follow_ok),
              (f"HUD ({h3[1]},{h3[2]}) vs 期望 ({ex},{ey}) 模式={mode} "
               f"(微信 {w3.rect}, 屏 {scr})") if h3 else "HUD 不见了")

    # 还原微信位置
    win32gui.MoveWindow(w.hwnd, wl, wt_, wr - wl, wb - wt_, True)
    time.sleep(2.0)

    # ---------- C. 滚动 ----------
    print("\n[C] 滚轮滚动右侧判断面板")
    # 右栏**有内容才能滚**。首轮分诊完成前右栏是空的（只有提示文字），
    # 此时滚轮当然没变化 —— 真机上曾因此报出假失败（像素差 0.30，
    # 而同一次运行里、有内容时的同类断言是 8.23）。先点左列一个人拿到详情。
    h4 = hud_rect()
    _, l4, t4, r4, b4 = h4
    click(l4 + 80, t4 + 62)          # 点左列第一行 → 右栏出详情
    time.sleep(2.5)
    before = grab((l4, t4, r4, b4))
    wheel(r4 - 200, t4 + (b4 - t4) // 2, notches=6)
    time.sleep(0.8)
    after = grab((l4, t4, r4, b4))
    diff = float(np.abs(before.astype(int) - after.astype(int)).mean())
    # 面板高度现在是跟着内容走的（PRD F-13：装得下就一屏看全，超出屏幕上限才滚动）。
    # 所以"滚了没变化"未必是 bug —— 先看面板自己记的滚动状态：
    note = [x for x in dbg_lines() if "滚动状态：" in x]
    note = note[-1] if note else ""
    if diff > 1.0:
        check("滚动后画面确实变化", True, f"像素差 {diff:.2f}")
    elif "一屏放下" in note:
        check("滚动后画面确实变化", True,
              f"像素差 0 属预期：{note.split('滚动状态：')[-1]}（内容已一屏放下，没有滚动条）")
    else:
        check("滚动后画面确实变化", False,
              f"像素差 {diff:.2f}；滚动状态记录={note or '(无)'}")

    # ---------- D. 右键菜单「退出」 ----------
    # 2026-09-23：✕ 已按用户要求移除，退出唯一入口是右键菜单 →「退出」
    # （Esc 通路仍在，但面板不接受键盘焦点，真机按键落不到）
    print("\n[D] 右键菜单「退出」：窗口消失 + 进程归零")
    h5 = hud_rect()
    _, l5, t5, r5, b5 = h5
    right_click(l5 + 80, t5 + 60)
    time.sleep(1.2)
    pr = popup_rect()
    check("右键菜单弹出", bool(pr),
          f"菜单 rect={pr[1:] if pr else None}（历史记录：6 项、末项「退出」）")
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
        print("P0 全部通过")
    return 1 if fails else 0


if __name__ == "__main__":
    raise SystemExit(main())
