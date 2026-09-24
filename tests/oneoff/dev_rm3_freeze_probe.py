# -*- coding: utf-8 -*-
r"""RM3 冻结探针：面板在「日志」清空之后是否还在扫描？

现象（2026-09-24 02:54 真机）：真点「日志」两次把 out/jev_audit.jsonl 清零后，
`out/hud_debug.log` 在 02:54:45 之后再无任何一行 —— 包括我在微信里真点换了会话之后。
面板窗口还在、进程还在、stderr 0 字节。

这个探针按顺序取证，把「GUI 线程是否活着」「微信内容是否真的变了」
「扫描线程是否还在跑」三件事分开来测：

  1) 真点面板空白处 → 看 debug 日志是否记 HudBar.mousePress（GUI 线程活着）
  2) 截图微信消息区 → 真点另一个会话（选中高亮校验）→ 再截图 → 像素差（内容真的变了）
  3) 之后 25s 内看 debug 日志新增行（扫描线程是否发现变化）

不改产品代码。用法：python .tests\dev_rm3_freeze_probe.py
"""
from __future__ import annotations

import ctypes
import json
import os
import sys
import time

PROJ = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
EVID = os.path.join(PROJ, "out", "verify_20260923")
sys.path.insert(0, PROJ)

import mss          # noqa: E402
import numpy as np  # noqa: E402
import win32gui     # noqa: E402
from PIL import Image                       # noqa: E402
from rapidocr_onnxruntime import RapidOCR   # noqa: E402

from wechat_triage_hud.paths import DEBUG_PATH          # noqa: E402
from wechat_triage_hud import wechat_window as ww       # noqa: E402

u32 = ctypes.windll.user32
CLS = "Qt692QWindowToolSaveBits"
_OCR = RapidOCR()
out = {"steps": []}


def grab(l, t, w, h):
    with mss.MSS() as s:
        return np.array(s.grab({"left": int(l), "top": int(t),
                                "width": int(w), "height": int(h)}))[:, :, :3][:, :, ::-1]


def save_png(arr, name):
    Image.fromarray(arr).save(os.path.join(EVID, name))


def panels():
    res = []

    def cb(h, _):
        try:
            if win32gui.GetClassName(h) == CLS and win32gui.IsWindowVisible(h):
                res.append((h,) + tuple(win32gui.GetWindowRect(h)))
        except Exception:
            pass
        return True

    win32gui.EnumWindows(cb, None)
    return sorted(res, key=lambda x: -((x[3] - x[1]) * (x[4] - x[2])))


def dbg_lines():
    if not os.path.exists(DEBUG_PATH):
        return []
    return [x for x in open(DEBUG_PATH, encoding="utf-8").read().splitlines() if x.strip()]


def click(x, y, pause=0.08):
    u32.SetCursorPos(int(x), int(y))
    time.sleep(0.15)
    u32.mouse_event(0x0002, 0, 0, 0, 0)
    time.sleep(pause)
    u32.mouse_event(0x0004, 0, 0, 0, 0)


def activate(hwnd):
    u32.ShowWindow(hwnd, 9)
    ft = u32.GetForegroundWindow()
    t1 = u32.GetWindowThreadProcessId(ft, None)
    t2 = ctypes.windll.kernel32.GetCurrentThreadId()
    u32.AttachThreadInput(t1, t2, True)
    u32.BringWindowToTop(hwnd)
    ok = u32.SetForegroundWindow(hwnd)
    u32.AttachThreadInput(t1, t2, False)
    time.sleep(0.5)


def main() -> int:
    t0 = time.time()
    os.makedirs(EVID, exist_ok=True)
    logf = open(os.path.join(EVID, "rm3_freeze_probe.log"), "w", encoding="utf-8")

    def say(s):
        print(s, flush=True)
        logf.write(s + "\n")
        logf.flush()

    say("=== RM3 冻结探针 " + time.strftime("%Y-%m-%d %H:%M:%S") + " ===")
    ps = panels()
    if not ps:
        say("找不到可见面板窗口 → 探针不适用")
        return 1
    hid, l, t, r, b = ps[0]
    wx = ww.find_main()
    say(f"面板 hwnd={hid} rect=({l},{t},{r},{b})；微信 rect={wx.rect}")

    # ---- 1) GUI 线程是否活着：真点面板空白处，看有没有 mousePress ----
    n0 = len(dbg_lines())
    time.sleep(3.0)                       # 先静置 3s，确认"不点就不动"
    quiet = dbg_lines()[n0:]
    say(f"[1] 静置 3s：debug 新增 {len(quiet)} 行 {quiet}")
    click(l + 70, t + 62)                 # 面板空白处（左列上方）
    time.sleep(3.0)
    after_click = dbg_lines()[n0:]
    gui_alive = any("mousePress" in x for x in after_click)
    say(f"[1] 真点面板 ({l+70},{t+62}) 后新增 {len(after_click)} 行 {after_click}")
    say(f"[1] 结论：GUI 线程{'活着（收到点击事件）' if gui_alive else '无响应（3s 内没有 mousePress）'}")
    out["steps"].append({"step": "1-gui-click", "new_lines": after_click, "gui_alive": gui_alive})

    # ---- 2) 微信内容是否真的变了：真点另一个会话 + 像素对比 ----
    wl, wt, wr, wb = wx.rect
    img = Image.fromarray(grab(wl, wt, 240, wb - wt))
    img = img.resize((img.width * 2, img.height * 2), Image.LANCZOS)
    save_png(np.array(img), "rm3_freeze_wxlist.png")
    res, _ = _OCR(np.array(img.convert("RGB")))
    rows = []
    for box, txt, score in (res or []):
        cy = int((min(p[1] for p in box) + max(p[1] for p in box)) / 2 / 2) + wt
        cx = int((min(p[0] for p in box) + max(p[0] for p in box)) / 2 / 2) + wl
        if cy > wt + 60 and len(txt) >= 2 and "搜索" not in txt:
            rows.append((txt, cx, cy))
    dedup = []
    for c in sorted(rows, key=lambda z: z[2]):
        if not any(abs(c[2] - d[2]) < 12 for d in dedup):
            dedup.append(c)
    say(f"[2] 微信左列候选 {[(a, y) for a, x, y in dedup][:8]}")
    msg_rect = (wl + 260, wt + 60, wr - wl - 270, wb - wt - 80)
    before = grab(*msg_rect)
    save_png(np.array(Image.fromarray(before)), "rm3_freeze_msg_before.png")
    picked = None
    for c in dedup[:3]:
        activate(wx.hwnd)
        click(c[1], c[2], pause=0.15)
        time.sleep(1.6)
        px = grab(c[1] + 75, c[2], 3, 3).reshape(-1, 3).mean(axis=0)
        selected = bool(px[1] > px[0] + 20 and px[1] > px[2] + 20)
        say(f"    真点 {c[0]!r} @ ({c[1]},{c[2]}) → 行像素 {[int(v) for v in px]} "
            f"{'已选中' if selected else '未选中'}")
        if selected:
            picked = c
            break
    time.sleep(2.0)
    after = grab(*msg_rect)
    save_png(np.array(Image.fromarray(after)), "rm3_freeze_msg_after.png")
    diff = float(np.abs(before.astype(int) - after.astype(int)).mean())
    say(f"[2] 消息区像素差 = {diff:.2f}（{'>0 说明微信内容真的变了' if diff > 1 else '≈0 说明内容没变'}）")
    out["steps"].append({"step": "2-session-click", "picked": picked, "msg_diff": round(diff, 3)})

    # ---- 3) 之后 25s：扫描线程有没有发现变化 ----
    say("[3] 观察 25s：扫描线程是否响应内容变化（每秒采样 debug 日志行数）")
    m0 = len(dbg_lines())
    timeline = []
    for i in range(25):
        time.sleep(1.0)
        nn = len(dbg_lines()) - m0
        timeline.append(nn)
    new = dbg_lines()[m0:]
    say(f"[3] 25s 内 debug 新增 {len(new)} 行：{new}")
    say(f"[3] 每秒累计行数：{timeline}")
    reacted = bool(new)
    say(f"[3] 结论：扫描线程{'有反应' if reacted else '完全无反应（停摆）'}"
        f"；GUI={gui_alive}；消息区像素差={diff:.2f}")
    out["steps"].append({"step": "3-watch", "new_lines": new, "timeline": timeline,
                         "gui_alive": gui_alive, "msg_diff": round(diff, 3),
                         "scanner_reacted": reacted})
    out["verdict"] = ("扫描停摆（GUI 活、内容已变、扫描线程零输出）"
                      if (gui_alive and diff > 1 and not reacted) else
                      ("扫描正常" if reacted else "无法判定（见上表）"))
    say("判定：" + out["verdict"])

    with open(os.path.join(EVID, "rm3_freeze_probe.json"), "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=2)
    say("证据 → out/verify_20260923/rm3_freeze_probe.{log,json}  "
        f"截图 rm3_freeze_msg_before.png / rm3_freeze_msg_after.png")
    say(f"耗时 {time.time()-t0:.1f}s")
    logf.close()
    return 0 if reacted else 2


if __name__ == "__main__":
    raise SystemExit(main())

