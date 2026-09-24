# -*- coding: utf-8 -*-
r"""真机复核 RM2 · 步骤 1b：面板启动时序（进程 spawn → 首个窗口 hwnd 出现）。

第一次跑发现"启动后 15s 枚举不到窗口"，这里量清楚到底要多久，避免把
"导入慢"误报成"面板没起来"。
同时验证：清理 → 启动 → 15s 枚举 → 吸附关系。跑完**保留面板运行**。

用法（会等 ~60s，建议后台跑）：python .tests\dev_rm2_startup_timing.py
"""
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
fails = []


def check(name, ok, detail=""):
    print(f"  {'PASS' if ok else 'FAIL'} {name}" + (f"  [{detail}]" if detail else ""), flush=True)
    if not ok:
        fails.append(name)
    return ok


def hud_procs():
    me = os.getpid()
    return sorted(p.info["pid"] for p in psutil.process_iter(["pid", "name", "cmdline"])
                  if p.info["pid"] != me and "python" in (p.info["name"] or "").lower()
                  and "wechat_triage_hud.hud" in " ".join(p.info["cmdline"] or []))


def hud_wins():
    out = []

    def cb(h, _):
        try:
            if win32gui.GetClassName(h) == CLS:
                r = win32gui.GetWindowRect(h)
                out.append({"hwnd": h, "rect": tuple(r),
                            "size": (r[2] - r[0], r[3] - r[1]),
                            "vis": bool(win32gui.IsWindowVisible(h)),
                            "title": win32gui.GetWindowText(h)})
        except Exception:
            pass
        return True

    win32gui.EnumWindows(cb, None)
    return out


def main():
    print("=" * 70)
    print("RM2 步骤 1b：启动时序 + 枚举 + 吸附")
    print("=" * 70, flush=True)

    print("\n[1a] 清理")
    r = subprocess.run([sys.executable, os.path.join(PROJECT_DIR, ".tests",
                                                     "dev_hud_cleanup.py")],
                       cwd=PROJECT_DIR, capture_output=True, encoding="utf-8",
                       errors="replace")
    print("\n".join("     " + x for x in r.stdout.strip().splitlines()))
    check("清理后 0 个 HUD 进程", not hud_procs(), f"{hud_procs()}")

    open(DEBUG_PATH, "w", encoding="utf-8").close()   # 用首行时间对齐"跑到 __init__"

    env = dict(os.environ, JEV_HUD_DEBUG="1", PYTHONIOENCODING="utf-8")
    t0 = time.time()
    print(f"\n[1b] t0={time.strftime('%H:%M:%S')} 启动 "
          f"`$env:JEV_HUD_DEBUG=1; python -m wechat_triage_hud.hud`（后台）")
    p = subprocess.Popen([sys.executable, "-u", "-m", "wechat_triage_hud.hud"],
                         cwd=PROJECT_DIR, env=env,
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    at15, first = None, None
    for _ in range(400):                       # 最多 100s
        el = time.time() - t0
        w = hud_wins()
        if w and first is None:
            first = el
        if el >= 15.0:
            at15 = w
            break
        time.sleep(0.25)
    print(f"    t=+15s 枚举：{len(at15 or [])} 个 {CLS} 窗口  →  {at15}")
    check("15s 时面板窗口已出现（可见）",
          bool(at15 and [x for x in at15 if x["vis"]]),
          (f"窗口在 +{first:.1f}s 才出现" if first else "100s 内未出现"))

    for _ in range(200):
        if hud_wins():
            break
        time.sleep(0.25)
    time.sleep(1.5)
    if first is None:
        first = time.time() - t0

    wins = hud_wins()
    vis = [x for x in wins if x["vis"]]
    print(f"\n[1c] t=+{time.time()-t0:.1f}s  枚举 {CLS}（含隐藏）：{len(wins)} 个")
    for x in wins:
        print(f"     hwnd={x['hwnd']} visible={x['vis']} "
              f"size={x['size'][0]}x{x['size'][1]} rect={x['rect']} title={x['title']!r}")
    check("存在可见面板窗口", bool(vis), f"可见 {len(vis)} / 共 {len(wins)}")
    check("只 1 个可见面板窗口（单实例保护）", len(vis) == 1, f"{len(vis)} 个")
    if not vis:
        return 1
    h = max(vis, key=lambda x: x["size"][0] * x["size"][1])
    hl, ht, hr, hb = h["rect"]
    check("窗口标题 = wechat-triage-hud", h["title"] == "wechat-triage-hud", h["title"])

    print("\n[1d] 微信窗口 + 吸附关系（follow() 的 GAP=6px 贴边规则）")
    w = ww.find_main()
    if not w:
        check("找到微信主窗口", False)
        return 1
    wl, wt_, wr, wb = w.rect
    print(f"     微信 hwnd={w.hwnd} class={w.cls!r} title={w.title!r}")
    print(f"     微信 rect={w.rect} minimized={w.minimized} visible={w.visible}")
    print(f"     面板 rect=({hl},{ht},{hr},{hb}) size={hr-hl}x{hb-ht}")
    print(f"     dy = 面板顶 {ht} - 微信顶 {wt_} = {ht-wt_}")
    print(f"     面板右 - 微信左 = {hr-wl}（>0 在微信右侧并留该间隙）")
    check("微信可见且未最小化", w.visible and not w.minimized,
          f"vis={w.visible} min={w.minimized}")
    check("微信标题非空（会话名）", bool(w.title.strip()), w.title)
    check("面板垂直对齐微信顶（dy=0）", abs(ht - wt_) <= 3, f"dy={ht-wt_}")
    site = ("下方" if abs(ht - (wb + 6)) <= 3 else
            "右侧" if abs(hl - (wr + 6)) <= 3 else
            "左侧" if abs(hr - (wl - 6)) <= 3 else "其它")
    print(f"     → 吸附位：{site}")
    check("面板贴在微信边（间隙 = 6px = GAP）", site in ("下方", "右侧", "左侧"),
          f"吸附位={site} 面板右-微信左={hr-wl} 面板左-微信右={hl-wr} "
          f"面板顶-微信底={ht-wb}")
    check("面板不压住微信矩形", hr <= wl or hl >= wr or hb <= wt_ or ht >= wb,
          f"面板({hl},{ht},{hr},{hb}) 微信({wl},{wt_},{wr},{wb})")

    print("\n[1e] 调试日志（本次启动新增）")
    lines = [x for x in open(DEBUG_PATH, encoding="utf-8").read().splitlines() if x.strip()]
    print(f"     {len(lines)} 行，前 10 行：")
    for x in lines[:10]:
        print("       " + x)
    bad = [x for x in lines if "Traceback" in x or "Error" in x or "Exception" in x]
    check("调试日志无异常", not bad, f"{len(bad)} 行：{bad[:2]}")

    print("\n" + "=" * 70)
    print(f"结论：窗口首次可见 +{first:.1f}s（其中含 python 导入与 OCR 初始化）")
    print("失败项：" + (", ".join(fails) if fails else "无"))
    print(f"面板 pid={p.pid} —— 保留运行")
    return 1 if fails else 0


if __name__ == "__main__":
    raise SystemExit(main())
