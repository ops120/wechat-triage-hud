# -*- coding: utf-8 -*-
r"""真机复核 RM2 · 步骤 1+2：清理确认 → JEV_HUD_DEBUG=1 后台启动 → 精确等 15s → 枚举可见面板。

输出 JSON 证据：out/verify_20260923/rm2_step2.json
跑完**保留面板运行**（后续步骤 3–7 继续用同一个进程）。

用法（后台，因 shell 30s 上限）：Start-Process python .tests\dev_rm2_step2.py
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import time

PROJECT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PROJECT_DIR)

import psutil      # noqa: E402
import win32gui    # noqa: E402

from wechat_triage_hud import wechat_window as ww  # noqa: E402
from wechat_triage_hud.paths import DEBUG_PATH      # noqa: E402

CLS = "Qt692QWindowToolSaveBits"
EVID = os.path.join(PROJECT_DIR, "out", "verify_20260923")
os.makedirs(EVID, exist_ok=True)


def hud_pids():
    me = os.getpid()
    out = []
    for p in psutil.process_iter(["pid", "cmdline"]):
        try:
            if p.info["pid"] == me:
                continue
            if "wechat_triage_hud.hud" in " ".join(p.info["cmdline"] or []):
                out.append(p.info["pid"])
        except Exception:
            pass
    return sorted(out)


def hud_wins(only_visible=True):
    out = []

    def cb(h, _):
        try:
            if win32gui.GetClassName(h) == CLS:
                vis = bool(win32gui.IsWindowVisible(h))
                if only_visible and not vis:
                    return True
                l, t, r, b = win32gui.GetWindowRect(h)
                out.append({"hwnd": h, "rect": [l, t, r, b],
                            "size": [r - l, b - t], "visible": vis,
                            "title": win32gui.GetWindowText(h),
                            "pid": win32gui.GetWindowThreadProcessId(h)[1],
                            "t": time.strftime("%H:%M:%S")})
        except Exception:
            pass
        return True

    win32gui.EnumWindows(cb, None)
    return out


def main():
    ev = {"t0": time.strftime("%H:%M:%S"), "steps": {}}

    print("=" * 70)
    print("RM2 步骤 1：python tests/dev_hud_cleanup.py  → 确认 0 进程", flush=True)
    print("=" * 70, flush=True)
    r = subprocess.run([sys.executable, os.path.join(PROJECT_DIR, ".tests",
                                                     "dev_hud_cleanup.py")],
                       cwd=PROJECT_DIR, capture_output=True, encoding="utf-8",
                       errors="replace")
    print(r.stdout.strip(), flush=True)
    after = hud_pids()
    print(f"清理后自查：{after if after else '0 个'}", flush=True)
    ev["steps"]["1_cleanup"] = {"stdout": r.stdout.strip(), "pids_after": after,
                               "pass": not after}

    print("\n" + "=" * 70, flush=True)
    print("RM2 步骤 2：JEV_HUD_DEBUG=1 后台启动 → 等 15s → 枚举可见面板", flush=True)
    print("=" * 70, flush=True)

    # 用首行时间对齐后续 debug 日志
    if os.path.exists(DEBUG_PATH):
        open(DEBUG_PATH + ".rm2.bak", "w", encoding="utf-8").write(
            open(DEBUG_PATH, encoding="utf-8").read())
    open(DEBUG_PATH, "w", encoding="utf-8").close()
    dbg_n0 = 0

    env = dict(os.environ, JEV_HUD_DEBUG="1", PYTHONIOENCODING="utf-8")
    t0 = time.time()
    print(f"t0={time.strftime('%H:%M:%S')} 启动 "
          f"`$env:JEV_HUD_DEBUG=1; python -m wechat_triage_hud.hud`（后台）", flush=True)
    p = subprocess.Popen([sys.executable, "-u", "-m", "wechat_triage_hud.hud"],
                         cwd=PROJECT_DIR, env=env,
                         stdout=open(os.path.join(EVID, "rm2_hud_stdout.log"), "ab"),
                         stderr=open(os.path.join(EVID, "rm2_hud_stderr.log"), "ab"))
    print(f"pid={p.pid}（保留运行）", flush=True)

    first_at = None
    at15 = None
    while time.time() - t0 < 40:
        el = time.time() - t0
        w = hud_wins()
        if w and first_at is None:
            first_at = el
            print(f"  +{el:.1f}s 首次枚举到可见窗口 {w[0]['rect']}", flush=True)
        if el >= 15.0:
            at15 = w
            break
        time.sleep(0.4)

    el = time.time() - t0
    print(f"\n+t={el:.1f}s（≥15s）枚举类名 {CLS} 的**可见**窗口：{len(at15 or [])} 个", flush=True)
    for w in (at15 or []):
        print(f"  hwnd={w['hwnd']} pid={w['pid']} rect={tuple(w['rect'])} "
              f"size={w['size'][0]}x{w['size'][1]} title={w['title']!r}", flush=True)
    vis_pass = bool(at15)
    print(f"  {'PASS' if vis_pass else 'FAIL'} 存在可见面板窗口", flush=True)

    w = ww.find_main()
    wx = ({"hwnd": w.hwnd, "title": w.title, "rect": list(w.rect),
           "minimized": w.minimized, "visible": w.visible} if w else None)
    print(f"\n微信窗口：{wx}", flush=True)

    ev["steps"]["2_start"] = {"pid": p.pid, "first_visible_at_s":
                              round(first_at, 2) if first_at else None,
                              "at15_at_s": round(el, 2), "windows_at15": at15 or [],
                              "visible_pass": vis_pass, "wechat": wx}
    with open(os.path.join(EVID, "rm2_step2.json"), "w", encoding="utf-8") as f:
        json.dump(ev, f, ensure_ascii=False, indent=2)
    print(f"\n证据 → out/verify_20260923/rm2_step2.json", flush=True)
    print(f"面板 pid={p.pid} —— 保留运行", flush=True)
    return 0 if vis_pass else 1


if __name__ == "__main__":
    raise SystemExit(main())
