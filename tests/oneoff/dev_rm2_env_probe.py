# -*- coding: utf-8 -*-
r"""真机复核 RM2 · 步骤 1：环境清理确认 + 面板窗口枚举 + 吸附关系。

输出（全部来自真实 win32 调用）：
  * 当前运行的 wechat_triage_hud.hud 进程 PID
  * 类名 Qt692QWindowToolSaveBits 的**可见**窗口：hwnd / rect / 尺寸 / 标题
  * 微信主窗口：hwnd / class / title / rect
  * 吸附关系：dx = 面板左 - 微信左，dy = 面板顶 - 微信顶，宽度差

用法：python .tests\dev_rm2_env_probe.py
"""
import ctypes
import os
import sys

PROJECT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PROJECT_DIR)

import psutil      # noqa: E402
import win32gui    # noqa: E402

from wechat_triage_hud import wechat_window as ww  # noqa: E402

CLS = "Qt692QWindowToolSaveBits"
fails = []


def check(name, ok, detail=""):
    print(f"  {'PASS' if ok else 'FAIL'} {name}" + (f"  [{detail}]" if detail else ""))
    if not ok:
        fails.append(name)
    return ok


def hud_windows():
    out = []

    def cb(h, _):
        try:
            if win32gui.GetClassName(h) == CLS:
                r = win32gui.GetWindowRect(h)
                out.append({
                    "hwnd": h,
                    "rect": tuple(r),
                    "size": (r[2] - r[0], r[3] - r[1]),
                    "visible": bool(win32gui.IsWindowVisible(h)),
                    "title": win32gui.GetWindowText(h),
                    "pid": win32gui.GetWindowThreadProcessId(h)[1],
                })
        except Exception:
            pass
        return True

    win32gui.EnumWindows(cb, None)
    return out


def hud_pids():
    me = os.getpid()
    out = []
    for p in psutil.process_iter(["pid", "cmdline"]):
        try:
            if p.info["pid"] == me:
                continue
            cl = " ".join(p.info["cmdline"] or [])
            if "wechat_triage_hud.hud" in cl:
                out.append(p.info["pid"])
        except Exception:
            pass
    return sorted(out)


def main():
    print("=" * 66)
    print("RM2 步骤 1：环境与吸附关系（真实 win32 枚举）")
    print("=" * 66)

    pids = hud_pids()
    print(f"\n[1] hud 进程：{pids if pids else '0 个'}")
    check("恰好 1 个面板进程（无僵尸实例）", len(pids) == 1, f"{len(pids)} 个：{pids}")

    wins = hud_windows()
    print(f"\n[2] 类名 {CLS} 的顶层窗口（含隐藏）：{len(wins)} 个")
    for w in wins:
        print(f"    hwnd={w['hwnd']} pid={w['pid']} visible={w['visible']} "
              f"size={w['size'][0]}x{w['size'][1]} rect={w['rect']} title={w['title']!r}")
    vis = [w for w in wins if w["visible"]]
    check(f"存在可见的 {CLS} 窗口", bool(vis), f"可见 {len(vis)} / 总计 {len(wins)}")
    if not vis:
        return 1
    hw = max(vis, key=lambda w: w["size"][0] * w["size"][1])
    hl, ht, hr, hb = hw["rect"]
    print(f"    → 主面板 hwnd={hw['hwnd']} rect=({hl},{ht},{hr},{hb}) "
          f"size={hw['size'][0]}x{hw['size'][1]}")
    if len(vis) > 1:
        print("    ⚠ 有多个可见面板窗口（单实例保护应阻止）")

    print("\n[3] 微信主窗口")
    w = ww.find_main()
    if not w:
        check("找到微信主窗口", False, "未找到（未登录/未运行）")
        return 1
    print(f"    hwnd={w.hwnd}\n    class={w.cls!r}\n    title={w.title!r}\n"
          f"    rect(帧)={w.rect}\n    rect(还原)={w.normal_rect}\n"
          f"    minimized={w.minimized} visible={w.visible}")
    wl, wt_, wr, wb = w.rect
    check("微信窗口标题非空（会话名/微信）", bool(w.title.strip()), w.title)
    check("微信窗口可见且未最小化", w.visible and not w.minimized,
          f"visible={w.visible} minimized={w.minimized}")

    print("\n[4] 吸附关系（面板应贴微信左侧，dx≈0）")
    dx, dy = hl - wl, ht - wt_
    dw = (hr - hl) - (wr - wl)
    print(f"    面板左 {hl} - 微信左 {wl} = dx {dx}")
    print(f"    面板顶 {ht} - 微信顶 {wt_} = dy {dy}")
    print(f"    面板宽 {hr-hl} - 微信宽 {wr-wl} = dW {dw}")
    check("dx ≈ 0（面板贴在微信左边缘）", abs(dx) <= 3, f"dx={dx}")
    check("宽度 = 微信宽（或按内容/屏幕夹取）", dw <= 0 or abs(dw) <= 3, f"dW={dw}")

    # 面板是否压住微信消息区（自遮挡检查的输入条件）
    overlap_w = max(0, min(hr, wr) - max(hl, wl))
    print(f"    面板与微信矩形水平重叠 {overlap_w}px")
    check("面板未压住微信矩形", overlap_w == 0, f"重叠 {overlap_w}px")

    print("\n" + "=" * 66)
    print("失败项：" + (", ".join(fails) if fails else "无"))
    return 1 if fails else 0


if __name__ == "__main__":
    raise SystemExit(main())
