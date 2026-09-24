# -*- coding: utf-8 -*-
r"""真机复核 RM2 · 步骤 1（完整可复现）：清理 → 启动 → 枚举 → 吸附关系。

严格按验收步骤执行，并把"窗口何时才出现"也如实记下来
（第一次跑发现 15s 时窗口还没建出来，这里改成"15s 检查一次 + 最多等 120s 并记录出现时刻"）。

用法：python .tests\dev_rm2_start_probe.py
"""
import ctypes
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
fails = []


def check(name, ok, detail=""):
    print(f"  {'PASS' if ok else 'FAIL'} {name}" + (f"  [{detail}]" if detail else ""), flush=True)
    if not ok:
        fails.append(name)
    return ok


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


def find_hud():
    out = []

    def cb(h, _):
        try:
            if win32gui.GetClassName(h) == CLS:
                r = win32gui.GetWindowRect(h)
                out.append({"hwnd": h, "rect": tuple(r),
                            "size": (r[2] - r[0], r[3] - r[1]),
                            "vis": bool(win32gui.IsWindowVisible(h)),
                            "title": win32gui.GetWindowText(h),
                            "pid": win32gui.GetWindowThreadProcessId(h)[1]})
        except Exception:
            pass
        return True

    win32gui.EnumWindows(cb, None)
    return out


def main():
    print("=" * 70)
    print("RM2 步骤 1：清理 → JEV_HUD_DEBUG=1 启动 → 枚举 → 吸附关系")
    print("=" * 70, flush=True)

    # ---------- 1a 清理 ----------
    print("\n[1a] python tests/dev_hud_cleanup.py")
    r = subprocess.run([sys.executable, os.path.join(PROJECT_DIR, ".tests",
                                                     "dev_hud_cleanup.py")],
                       cwd=PROJECT_DIR, capture_output=True, encoding="utf-8",
                       errors="replace")
    print(r.stdout.strip())
    check("清理后 0 个 HUD 进程", not hud_pids(), f"残留 {hud_pids()}")

    # ---------- 1b 启动 ----------
    n0 = len(open(DEBUG_PATH, encoding="utf-8").read().splitlines()) \
        if os.path.exists(DEBUG_PATH) else 0
    env = dict(os.environ, JEV_HUD_DEBUG="1", PYTHONIOENCODING="utf-8")
    t0 = time.time()
    p = subprocess.Popen(
        [sys.executable, "-u", "-m", "wechat_triage_hud.hud"],
        cwd=PROJECT_DIR, env=env,
        stdout=open(os.path.join(EVID, "rm2_hud_stdout.log"), "ab"),
        stderr=open(os.path.join(EVID, "rm2_hud_stderr.log"), "ab"))
    print(f"\n[1b] 已启动 python -m wechat_triage_hud.hud  (pid={p.pid}, "
          f"JEV_HUD_DEBUG=1, t0={time.strftime('%H:%M:%S')})")

    # 15s 处按验收要求检查一次
    first_at = None
    at15 = None
    for _ in range(120):
        el = time.time() - t0
        w = find_hud()
        if w and first_at is None:
            first_at = el
        if el >= 15.0 and at15 is None:
            at15 = w
        if first_at is not None and el >= 15.0:
            break
        time.sleep(0.5)
    print(f"    15s 时枚举结果：{len(at15 or [])} 个 {CLS} 窗口")
    check("15s 时面板窗口已出现", bool(at15),
          f"实测：窗口 {first_at:.1f}s 后才出现" if first_at else "120s 内未出现")
    if not w:
        return 1

    time.sleep(2)
    wins = find_hud()
    vis = [x for x in wins if x["vis"]]
    print(f"\n[1c] 枚举 {CLS} 窗口（含隐藏）：{len(wins)} 个")
    for x in wins:
        print(f"    hwnd={x['hwnd']} pid={x['pid']} visible={x['vis']} "
              f"size={x['size'][0]}x{x['size'][1]} rect={x['rect']} title={x['title']!r}")
    check("存在可见面板窗口", bool(vis), f"可见 {len(vis)}")
    check("只有 1 个可见面板窗口（单实例）", len(vis) == 1, f"{len(vis)} 个")
    h = max(vis, key=lambda x: x["size"][0] * x["size"][1])
    hl, ht, hr, hb = h["rect"]
    check("面板标题 = wechat-triage-hud", h["title"] == "wechat-triage-hud", h["title"])

    # ---------- 1d 微信 + 吸附 ----------
    print("\n[1d] 微信窗口与吸附关系")
    ww_ = ww.find_main()
    if not ww_:
        check("找到微信主窗口", False, "未找到")
        return 1
    print(f"    微信 hwnd={ww_.hwnd} class={ww_.cls!r} title={ww_.title!r}")
    print(f"    微信 rect={ww_.rect} minimized={ww_.minimized} visible={ww_.visible}")
    wl, wt_, wr, wb = ww_.rect
    dx, dy = hl - wl, ht - wt_
    print(f"    面板 rect=({hl},{ht},{hr},{hb}) size={hr-hl}x{hb-ht}")
    print(f"    dx = {dx}   dy = {dy}   宽差 = {(hr-hl)-(wr-wl)}")
    check("微信窗口可用（非最小化且可见）", ww_.visible and not ww_.minimized,
          f"vis={ww_.visible} min={ww_.minimized}")
    check("标题含会话名/微信（非空）", bool(ww_.title.strip()), ww_.title)
    check("吸附 dx ≈ 0", abs(dx) <= 3, f"dx={dx}")
    check("面板未压住微信矩形", hr <= wl, f"面板右 {hr} vs 微信左 {wl}")

    # ---------- 1e debug 日志 ----------
    print("\n[1e] 调试日志（新增部分）")
    lines = open(DEBUG_PATH, encoding="utf-8").read().splitlines()
    new = [x for x in lines[n0:] if x.strip()]
    print(f"    新增 {len(new)} 行：")
    for x in new[:20]:
        print("      " + x)
    bad = [x for x in new if "Traceback" in x or "Error" in x or "Exception" in x]
    check("新增调试日志无异常", not bad, f"{len(bad)} 行命中：{bad[:2]}")

    print("\n" + "=" * 70)
    print(f"面板启动耗时：{first_at:.1f}s" if first_at else "面板启动耗时：>120s")
    print("失败项：" + (", ".join(fails) if fails else "无"))
    print(f"面板 pid={p.pid}（保留运行，后续步骤继续用它）")
    return 1 if fails else 0


if __name__ == "__main__":
    raise SystemExit(main())
