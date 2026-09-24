# -*- coding: utf-8 -*-
r"""真机复核 RM2 · 步骤 2（重测）：清理 → 无控制台窗口后台启动 → 每 0.5s 采样 22s。

第一次跑在 +15.3s 快照到 0 个可见面板窗口。`follow()`(hud.py:1045-1046) 只在
`find_main()` 返回空、或微信 minimized/not visible 时 `setVisible(False)` ——
所以这里同时采样「面板窗口可见性」和「微信候选窗口」，把当时的真实原因一起记下来。

用法（后台）：Start-Process python .tests\dev_rm2_step2b_timeline.py
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


def hud_pids():
    me = os.getpid()
    out = []
    for p in psutil.process_iter(["pid", "cmdline"]):
        try:
            if p.info["pid"] != me and \
                    "wechat_triage_hud.hud" in " ".join(p.info["cmdline"] or []):
                out.append(p.info["pid"])
        except Exception:
            pass
    return sorted(out)


def cls_windows():
    """所有该 class 的顶层窗口（含隐藏）。"""
    out = []

    def cb(h, _):
        try:
            if win32gui.GetClassName(h) == CLS:
                l, t, r, b = win32gui.GetWindowRect(h)
                out.append({"hwnd": h, "visible": bool(win32gui.IsWindowVisible(h)),
                            "rect": [l, t, r, b], "size": [r - l, b - t],
                            "title": win32gui.GetWindowText(h)})
        except Exception:
            pass
        return True

    win32gui.EnumWindows(cb, None)
    return out


def wx_state():
    ws = ww.list_windows()
    return [{"hwnd": w.hwnd, "title": w.title, "vis": w.visible,
             "min": w.minimized, "normal": list(w.normal_rect)} for w in ws]


def main():
    ev = {"t0": time.strftime("%H:%M:%S"), "samples": []}
    print("=" * 70)
    print("RM2 步骤 2（重测）：无控制台窗口启动 + 每 0.5s 采样 22s")
    print("=" * 70, flush=True)

    r = subprocess.run([sys.executable, os.path.join(PROJECT_DIR, ".tests",
                                                     "dev_hud_cleanup.py")],
                       cwd=PROJECT_DIR, capture_output=True, encoding="utf-8",
                       errors="replace")
    print("[1] 清理：" + " | ".join(r.stdout.strip().splitlines()), flush=True)
    print(f"    清理后 HUD 进程：{hud_pids() or '0 个'}", flush=True)

    open(DEBUG_PATH, "w", encoding="utf-8").close()
    env = dict(os.environ, JEV_HUD_DEBUG="1", PYTHONIOENCODING="utf-8")
    DETACHED = 0x00000008 | 0x08000000          # DETACHED_PROCESS | CREATE_NO_WINDOW
    t0 = time.time()
    p = subprocess.Popen([sys.executable, "-u", "-m", "wechat_triage_hud.hud"],
                         cwd=PROJECT_DIR, env=env, creationflags=DETACHED,
                         stdout=open(os.path.join(EVID, "rm2_hud_stdout.log"), "ab"),
                         stderr=open(os.path.join(EVID, "rm2_hud_stderr.log"), "ab"))
    print(f"\n[2] t0={time.strftime('%H:%M:%S')} 启动（无控制台窗口，pid={p.pid}）", flush=True)
    print("    t(s)  面板窗口(该class)          微信候选", flush=True)

    first_at = None
    at15 = None
    while True:
        el = time.time() - t0
        if el > 22.0:
            break
        ws = cls_windows()
        vis = [x for x in ws if x["visible"]]
        wx = wx_state()
        if vis and first_at is None:
            first_at = el
        if el >= 15.0 and at15 is None:
            at15 = {"el": round(el, 2), "windows": ws, "wechat": wx}
        if el >= 15.0 and at15 and at15.get("el") != round(el, 2) and el >= 15.6:
            pass
        line = (f"    {el:5.1f}  共{len(ws)}个/可见{len(vis)}个 "
                f"{[(x['hwnd'], tuple(x['rect'])) for x in ws]}  "
                f"{[(x['hwnd'], x['title'], x['vis'], x['min']) for x in wx]}")
        print(line, flush=True)
        ev["samples"].append({"el": round(el, 2), "windows": ws, "wechat": wx})
        time.sleep(0.5)

    tgt = next((s for s in ev["samples"] if s["el"] >= 15.0), None)
    print("\n[3] 结论", flush=True)
    print(f"    首次可见面板：{f'+{first_at:.1f}s' if first_at else '22s 内未出现'}", flush=True)
    if tgt:
        vis = [x for x in tgt["windows"] if x["visible"]]
        print(f"    +{tgt['el']}s（≥15s）枚举 {CLS} 可见窗口：{len(vis)} 个", flush=True)
        for x in tgt["windows"]:
            print(f"      hwnd={x['hwnd']} visible={x['visible']} rect={tuple(x['rect'])} "
                  f"size={x['size'][0]}x{x['size'][1]} title={x['title']!r}", flush=True)
        print(f"    同时刻微信候选：{tgt['wechat']}", flush=True)
        print(f"    → {'PASS' if vis else 'FAIL'} 存在可见面板窗口  面板 rect={tuple(vis[0]['rect']) if vis else None}",
              flush=True)

    hid = [s for s in ev["samples"] if s["el"] >= 15.0
           and not [x for x in s["windows"] if x["visible"]]]
    print(f"    ≥15s 之后仍采样到「可见 0 个」的次数：{len(hid)}"
          f"（若 >0，说明快照失败不是一次性巧合）", flush=True)
    ev["conclusion"] = {"first_visible_at_s": round(first_at, 2) if first_at else None,
                        "at15": tgt, "blank_samples_after_15s": len(hid)}
    with open(os.path.join(EVID, "rm2_step2b_timeline.json"), "w", encoding="utf-8") as f:
        json.dump(ev, f, ensure_ascii=False, indent=2)
    print("    证据 → out/verify_20260923/rm2_step2b_timeline.json", flush=True)
    print(f"\n面板 pid={p.pid} —— 保留运行", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
