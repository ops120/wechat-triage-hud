# -*- coding: utf-8 -*-
"""RM3 步骤1：后台启动面板 → 采样可见性 → 15s 定格枚举 + 截图。

只读产品代码，只写 out\\verify_20260923\\ 下的证据。
启动前请先跑 dev_rm3_normalize.py（还原微信、去遮挡）——本脚本不碰窗口位置，
只负责「起进程 / 采样 / 枚举 / 截图」，避免把环境归一和启动证据混在一起。
"""
from __future__ import annotations

import os
import subprocess
import sys
import time

PROJ = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
EVID = os.path.join(PROJ, "out", "verify_20260923")
sys.path.insert(0, PROJ)
os.makedirs(EVID, exist_ok=True)

import win32gui     # noqa: E402
import mss          # noqa: E402
import mss.tools    # noqa: E402  （子模块必须显式 import，否则 mss.tools 属性不存在）
from mss import MSS as _MSS  # noqa: E402

sys.path.insert(0, PROJ)
from wechat_triage_hud import wechat_window as ww  # noqa: E402

CLS = "Qt692QWindowToolSaveBits"


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


def main():
    # 本脚本自己写日志文件：不依赖 PowerShell 管道 —— 面板子进程会继承管道句柄，
    # `python x.py | Out-File` 会一直等到面板退出（实测卡死 30s 超时）。
    class _Tee:
        def __init__(self, *fs):
            self.fs = fs

        def write(self, s):
            for f in self.fs:
                try:
                    f.write(s)
                    f.flush()
                except Exception:
                    pass

        def flush(self):
            for f in self.fs:
                try:
                    f.flush()
                except Exception:
                    pass

    _logf = open(os.path.join(EVID, "rm3_step2_enum.log"), "w", encoding="utf-8")
    sys.stdout = _Tee(sys.__stdout__, _logf)

    t0 = time.time()
    stamp = time.strftime("%Y-%m-%d %H:%M:%S")
    print(f"=== RM3 启动 {stamp} ===")
    env = dict(os.environ)
    env["JEV_HUD_DEBUG"] = "1"
    env["PYTHONIOENCODING"] = "utf-8"
    so = open(os.path.join(EVID, "rm3_hud_stdout.log"), "w", encoding="utf-8")
    se = open(os.path.join(EVID, "rm3_hud_stderr.log"), "w", encoding="utf-8")
    # DETACHED_PROCESS：面板不再挂在启动脚本的控制台上，避免句柄被父进程借用
    flags = 0x00000008 | 0x00000200          # DETACHED_PROCESS | NEW_PROCESS_GROUP
    p = subprocess.Popen([sys.executable, "-m", "wechat_triage_hud.hud"],
                         cwd=PROJ, env=env, stdin=subprocess.DEVNULL,
                         stdout=so, stderr=se, creationflags=flags)
    print(f"启动 PID={p.pid}  JEV_HUD_DEBUG=1")

    first = None
    vis_at = {}
    last = None
    while True:
        el = time.time() - t0
        if el > 22:
            break
        ps = panels()
        now = round(el, 2)
        if ps and first is None:
            first = now
            print(f"  首次可见面板：+{now}s rect={ps[0][1:]}")
        last = ps
        for m in (15, 16, 18, 20):
            if now >= m and m not in vis_at:
                vis_at[m] = len(ps)
        if el >= 15.0 and "15" in vis_at and now >= 15.5:
            break
        time.sleep(0.5)

    el = time.time() - t0
    print(f"  +{el:.2f}s 枚举 {CLS} 可见窗口：{len(last)} 个")
    for h, l, t, r, b in last:
        print(f"    hwnd={h} rect=({l},{t},{r},{b}) size={r-l}x{b-t}")
    print(f"  首次可见：{first}s   各时刻可见数 {vis_at}")

    # ---- 面板在微信的哪一侧（按 hud.follow() 的三种摆放模式判定）----
    wx = ww.find_main()
    side, detail = "?", ""
    if not last:
        side = "无面板"
    elif not wx:
        side = "找不到微信"
    else:
        hl, ht, hr, hb = last[0][1:]
        wl, wt, wr, wb = wx.rect
        GAP, BALL = 6, 52
        W, H = hr - hl, hb - ht
        if (W, H) == (BALL, BALL):
            side = "小球（非面板形态）"
        elif abs(hl - wl) <= 3 and abs(ht - (wb + GAP)) <= 3:
            side = "微信下方"
            detail = f"x 与微信左对齐({wl})，y = 微信底 {wb} + GAP {GAP} = {wb + GAP}"
        elif abs(hl - (wr + GAP)) <= 3 and abs(ht - wt) <= 3:
            side = "微信右侧"
            detail = f"x = 微信右 {wr} + GAP {GAP} = {wr + GAP}"
        elif abs(hl - (wl - GAP - W)) <= 3 and abs(ht - wt) <= 3:
            side = "微信左侧"
            detail = (f"x = 微信左 {wl} − GAP {GAP} − 面板宽 {W} = "
                      f"{wl - GAP - W}（实测 {hl}）")
        else:
            side = "其它（屏幕夹取/用户拖过/固定）"
            detail = f"实测 ({hl},{ht})  微信 {wx.rect}"
    print(f"  → 面板位置判定：{side}   {detail}")

    # 截图留证（整屏 + 面板裁剪）
    with _MSS() as s:
        full = s.grab(s.monitors[0])
        mss.tools.to_png(full.rgb, full.size,
                         output=os.path.join(EVID, "rm3_step2_screen.png"))
        if last:
            h, l, t, r, b = last[0]
            pad = 4
            crop = s.grab({"left": l - pad, "top": t - pad,
                           "width": r - l + 2 * pad, "height": b - t + 2 * pad})
            mss.tools.to_png(crop.rgb, crop.size,
                             output=os.path.join(EVID, "rm3_step2_panel.png"))
    print(f"  截图 → rm3_step2_screen.png / rm3_step2_panel.png")
    with open(os.path.join(EVID, "rm3_step1_t0.txt"), "w", encoding="utf-8") as f:
        f.write(f"t0={stamp}\npid={p.pid}\nfirst_visible={first}\n"
                f"at_15s_panels={vis_at.get(15)}\n"
                f"panels={[x[1:] for x in last]}\n"
                f"wechat_rect={wx.rect if wx else None}\n"
                f"side={side}\n{detail}\n")
    ok = bool(last)
    print("结果：" + ("PASS 面板可见" if ok else "FAIL 无可见面板"))
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
