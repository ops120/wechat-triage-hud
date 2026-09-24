# -*- coding: utf-8 -*-
r"""RM3 补充：独立复算 dev_p0_verify.py 的 [C]（滚轮）到底该不该动。

背景：p0 的 [C] 现在有两条出口——像素差 >1 直接过；像素差 ≈0 时，只有产品自己在
`out/hud_debug.log` 里记了「滚动状态：内容 N ≤ 视口 M → 一屏放下」才算过。
这里独立走一遍真机路径，把「产品自报的度量」和「真滚轮像素观测」摆在一起。

不改产品代码。用法：python .tests\dev_rm3_scroll_check.py
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
fails, checks, ev = [], [], {}


def check(name, ok, detail=""):
    print(f"  {'PASS' if ok else 'FAIL'} {name}" + (f"  [{detail}]" if detail else ""), flush=True)
    checks.append({"item": name, "pass": bool(ok), "detail": str(detail)})
    if not ok:
        fails.append(name)
    return ok


def grab(l, t, w, h):
    with mss.MSS() as s:
        return np.array(s.grab({"left": int(l), "top": int(t),
                                "width": int(w), "height": int(h)}))[:, :, :3][:, :, ::-1]


def save_png(arr, name):
    Image.fromarray(arr).save(os.path.join(EVID, name))


def panel_rect():
    out = []

    def cb(h, _):
        try:
            if win32gui.GetClassName(h) == CLS and win32gui.IsWindowVisible(h):
                out.append((h,) + tuple(win32gui.GetWindowRect(h)))
        except Exception:
            pass
        return True

    win32gui.EnumWindows(cb, None)
    return max(out, key=lambda r: (r[3] - r[1]) * (r[4] - r[2])) if out else None


def dbg_lines():
    if not os.path.exists(DEBUG_PATH):
        return []
    return [x for x in open(DEBUG_PATH, encoding="utf-8").read().splitlines() if x.strip()]


def last_scroll_note():
    n = [x for x in dbg_lines() if "滚动状态：" in x]
    return n[-1].split("滚动状态：")[-1] if n else ""


def click(x, y, pause=0.06):
    u32.SetCursorPos(int(x), int(y))
    time.sleep(0.12)
    u32.mouse_event(0x0002, 0, 0, 0, 0)
    time.sleep(pause)
    u32.mouse_event(0x0004, 0, 0, 0, 0)


def wheel(x, y, notches=6):
    u32.SetCursorPos(int(x), int(y))
    time.sleep(0.25)
    for _ in range(notches):
        u32.mouse_event(0x0800, 0, 0, -120, 0)
        time.sleep(0.12)


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
    return ok


def select_row(x, y, tries=(0, -6, 6, -12, 12)):
    for dy in tries:
        click(x, y + dy, pause=0.15)
        time.sleep(1.5)
        px = grab(x + 75, y + dy, 3, 3).reshape(-1, 3).mean(axis=0)
        if px[1] > px[0] + 20 and px[1] > px[2] + 20:
            return True, y + dy
        time.sleep(0.4)
    return False, y


def wait_dbg_after(n0, needle, timeout=80):
    end = time.time() + timeout
    while time.time() < end:
        hit = [x for x in dbg_lines()[n0:] if needle in x]
        if hit:
            return hit
        time.sleep(1.0)


def main():
    t0 = time.time()
    os.makedirs(EVID, exist_ok=True)
    print("=" * 74)
    print("[C] 独立复算：滚轮滚动右栏（真点会话 → 真点人 → 真滚轮）  t0=" +
          time.strftime("%H:%M:%S"))
    print("=" * 74, flush=True)
    pr = panel_rect()
    if not pr:
        check("面板在跑（前置条件）", False, "找不到可见面板窗口")
        return 1
    hid, l, t, r, b = pr
    print(f"面板 hwnd={hid} rect=({l},{t},{r},{b})", flush=True)

    wx = ww.find_main()
    if not wx:
        check("找到微信主窗口（前置）", False, "找不到微信")
        return 1
    wl, wt, wr, wb = wx.rect
    img = Image.fromarray(grab(wl, wt, 240, wb - wt))
    img = img.resize((img.width * 2, img.height * 2), Image.LANCZOS)
    save_png(np.array(img), "rm3_scroll_wxlist.png")
    res, _ = _OCR(np.array(img.convert("RGB")))
    cands = []
    for box, txt, score in (res or []):
        cy = int((min(p[1] for p in box) + max(p[1] for p in box)) / 2 / 2) + wt
        cx = int((min(p[0] for p in box) + max(p[0] for p in box)) / 2 / 2) + wl
        cands.append((txt, cx, cy, score))
    dedup = []
    for c in sorted(cands, key=lambda z: z[2]):
        if c[2] < wt + 60 or len(c[0]) < 2 or "搜索" in c[0]:
            continue
        if not any(abs(c[2] - d[2]) < 12 for d in dedup):
            dedup.append(c)
    print(f"    微信左列候选 {[(a, y) for a, x, y, s in dedup][:8]}", flush=True)

    rows, scrollable_found = [], False
    for i, c in enumerate(dedup[:3], 1):
        print(f"\n  --- 候选 {i} {c[0]!r} @ ({c[1]},{c[2]}) ---", flush=True)
        n0 = len(dbg_lines())
        activate(wx.hwnd)
        ok, yy = select_row(wl, c[1], c[2])
        print(f"    真点并校验选中：{ok}（y={yy}）", flush=True)
        if not ok:
            continue
        if not wait_dbg_after(n0, "提交分诊", timeout=80):
            print("    80s 内无「提交分诊」，跳过", flush=True)
            continue
        time.sleep(3.0)
        pr = panel_rect() or (l, t, r, b)
        pl, pt, prr, pb = pr
        click(pl + 80, pt + 62)                 # 真点左列第一行 → 右栏出详情
        time.sleep(2.8)
        pr = panel_rect() or pr
        pl, pt, prr, pb = pr
        note = last_scroll_note()
        rl, rt, rw, rh = pl + 250, pt + 88, prr - pl - 252, pb - pt - 120
        before = grab(rl, rt, rw, rh)
        wheel(prr - 200, pt + (pb - pt) // 2, notches=6)
        time.sleep(0.8)
        after = grab(rl, rt, rw, rh)
        diff = float(np.abs(before.astype(int) - after.astype(int)).mean())
        save_png(np.array(Image.fromarray(
            np.concatenate([before, after], axis=1)).resize(
            (rw * 2, rh * 2), Image.LANCZOS)), f"rm3_scroll_c{i}_before_after.png")
        need_scroll = "需要滚动" in note
        rows.append({"session": c[0], "note": note, "diff": round(diff, 3),
                     "rect": list(pr), "png": f"rm3_scroll_c{i}_before_after.png"})
        print(f"    面板自报：{note}\n    真滚轮 6 格后像素差={diff:.2f}"
              f"  → 截图 rm3_scroll_c{i}_before_after.png", flush=True)
        if need_scroll:
            scrollable_found = True
            check(f"[C-真滚动] 会话 {c[0]!r}：内容超视口 → 滚轮必须改变画面",
                  diff > 1.0, f"像素差 {diff:.2f}（自报：{note}）")
            break
        check(f"[C-一致性] 会话 {c[0]!r}：自报一屏放下 → 滚轮不应改变画面",
              diff <= 1.0, f"像素差 {diff:.2f}（自报：{note}）")

    ev["rounds"] = rows
    ev["scrollable_found"] = scrollable_found
    ev["checks"] = checks
    ev["fails"] = fails
    ev["elapsed_s"] = round(time.time() - t0, 1)
    with open(os.path.join(EVID, "rm3_scroll_check.json"), "w", encoding="utf-8") as f:
        json.dump(ev, f, ensure_ascii=False, indent=2)
    print("\n" + "=" * 74)
    if not rows:
        check("[C] 至少完成一次真机会话轮次", False, "所有候选都点不中/没触发")
    if not scrollable_found:
        print("  注：本机试过的会话都是「内容 ≤ 视口 → 一屏放下」，"
              "因此**没能在真机上构造出可滚动内容**；上表给出的是"
              "「自报度量 ↔ 像素观测」的一致性。滚动通道本身在 rm2 真机"
              "（同批有内容时像素差 8.23）已验证过。")
    print(f"耗时 {ev['elapsed_s']}s  失败项：" + (", ".join(fails) if fails else "无"))
    print("证据 → out/verify_20260923/rm3_scroll_check.json")
    return 1 if fails else 0


if __name__ == "__main__":
    raise SystemExit(main())

