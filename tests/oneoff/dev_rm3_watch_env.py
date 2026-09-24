# -*- coding: utf-8 -*-
"""RM3 环境观察器：为什么面板 hwnd / 微信位置会自己变？

真机复核时实测到：我启动的面板 PID 在一分钟后消失，同时出现 3 个新的
`wechat_triage_hud.hud` 进程，微信窗口也从 (1240,21) 挪到 (691,21)。
在把这类现象写进报告之前，必须先确认真机本身是不是**有别的进程在动它**，
否则会把"环境噪声"误判成产品缺陷。

本脚本只读：每 3 秒采样一次（面板 hwnd/rect、HUD 进程 PID、微信 rect、
前台窗口标题），写 out/verify_20260923/rm3_env_watch.log。不改产品代码。
"""
from __future__ import annotations

import os
import sys
import time

PROJ = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
EVID = os.path.join(PROJ, "out", "verify_20260923")
sys.path.insert(0, PROJ)
os.makedirs(EVID, exist_ok=True)

import psutil       # noqa: E402
import win32gui     # noqa: E402

from wechat_triage_hud import wechat_window as ww  # noqa: E402

CLS = "Qt692QWindowToolSaveBits"
DURATION_S = float(os.environ.get("RM3_WATCH_S", "42"))
PERIOD_S = 3.0
me = os.getpid()


def panels():
    res = []

    def cb(h, _):
        try:
            if win32gui.IsWindowVisible(h) and win32gui.GetClassName(h) == CLS:
                res.append((h,) + tuple(win32gui.GetWindowRect(h)))
        except Exception:
            pass
        return True

    win32gui.EnumWindows(cb, None)
    return sorted(res, key=lambda x: -((x[3] - x[1]) * (x[4] - x[2])))


def hud_procs():
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


def main() -> int:
    log = open(os.path.join(EVID, "rm3_env_watch.log"), "w", encoding="utf-8")

    def say(s):
        print(s, flush=True)
        log.write(s + "\n")
        log.flush()

    say("=== RM3 环境观察 " + time.strftime("%Y-%m-%d %H:%M:%S") + f" 约 {DURATION_S:.0f}s ===")
    t0 = time.time()
    while time.time() - t0 < DURATION_S:
        w = ww.find_main()
        fg = win32gui.GetForegroundWindow()
        try:
            fg_txt = f"{win32gui.GetClassName(fg)}/{win32gui.GetWindowText(fg)[:24]}"
        except Exception:
            fg_txt = "?"
        say(f"+{time.time()-t0:5.1f}s  面板={[x[1:] for x in panels()]}  "
            f"HUD进程={hud_procs()}  微信={w.rect if w else None}  "
            f"微信最小化={w.minimized if w else '?'}  前台={fg_txt}")
        time.sleep(PERIOD_S)
    log.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
