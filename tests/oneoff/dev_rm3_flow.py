# -*- coding: utf-8 -*-
r"""RM3 真机专项（步骤 3a–3d）：全部真鼠标 / 真会话 / 真文件 / 真 API。

  3a  真点「日志」两次 → out\jev_audit.jsonl 归零 → 等一轮真实分诊 →
      逐行 json.loads 合法 + seq 从 1 重排
  3b  真点微信另一个会话 → 面板标题换新、左列不残留旧会话人名、
      小球颜色按新结论（截图留证）
  3c  1 秒内真点左列两个人 → 两次提交都进 debug 日志、两人都能在右栏看到各自判断
  3d  切到单聊 → 真实触发一轮 → 从 out\jev_audit.jsonl 的 scene=dm 记录确认
      「对话.关系」/「我.身份」进了 request_raw 且 http 200；
      面板顶部状态行小区域截图 rm3_* 留证

一切结论来自真实 win32 / mss / RapidOCR / 真实日志文件；不改产品代码。
用法（后台）：Start-Process python -ArgumentList '.tests\dev_rm3_flow.py'
"""
from __future__ import annotations

import ctypes
import json
import os
import re
import shutil
import sys
import time

PROJECT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PROJECT_DIR)

import mss          # noqa: E402
import numpy as np  # noqa: E402
import win32gui     # noqa: E402
from PIL import Image                       # noqa: E402
from rapidocr_onnxruntime import RapidOCR   # noqa: E402

from wechat_triage_hud.paths import AUDIT_PATH, DEBUG_PATH  # noqa: E402
from wechat_triage_hud import wechat_window as ww          # noqa: E402

u32 = ctypes.windll.user32
CLS = "Qt692QWindowToolSaveBits"
EVID = os.path.join(PROJECT_DIR, "out", "verify_20260923")
BALL_PALETTE = {"alert(橙红)": (194, 65, 12), "todo(暗黄)": (201, 154, 46),
                "silent(灰)": (176, 176, 176), "扫描异常(红)": (220, 38, 38)}
fails, checks, ev = [], [], {"phases": {}}
_OCR = RapidOCR()


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


def ocr_rows(l, t, w, h, scale=3.0, tag=""):
    """OCR 屏幕区域 → [(text, cx, cy, score)]（坐标回屏幕系）；tag 非空则存放大截图。"""
    img = Image.fromarray(grab(l, t, w, h))
    if scale != 1.0:
        img = img.resize((int(img.width * scale), int(img.height * scale)), Image.LANCZOS)
    res, _ = _OCR(np.array(img.convert("RGB")))
    out = []
    for box, txt, score in (res or []):
        xs = [p[0] for p in box]
        ys = [p[1] for p in box]
        out.append((txt, int(l + (min(xs) + max(xs)) / 2 / scale),
                    int(t + (min(ys) + max(ys)) / 2 / scale), score))
    if tag:
        save_png(np.array(img), tag)
    print(f"    OCR {tag or ''} ({l},{t})+{w}x{h}: " +
          " | ".join(f"{a}({x},{y})" for a, x, y, _ in out), flush=True)
    return out


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


def panel_bbox():
    """只取 (l, t, r, b)。panel_rect() 是 5 元组（含 hwnd），
    直接拿去当 rect 用会把 hwnd 当左边 → grab() 报 'Region has zero or negative size'
    （本脚本第一版就这么崩过）。"""
    r = panel_rect()
    return (r[1], r[2], r[3], r[4]) if r else None


def click(x, y, pause=0.06):
    u32.SetCursorPos(int(x), int(y))
    time.sleep(0.12)
    u32.mouse_event(0x0002, 0, 0, 0, 0)
    time.sleep(pause)
    u32.mouse_event(0x0004, 0, 0, 0, 0)


def activate(hwnd):
    """点微信会话行之前必须让微信在前台，否则点击被前台窗口吃掉（实测过）。"""
    u32.ShowWindow(hwnd, 9)
    ft = u32.GetForegroundWindow()
    t1 = u32.GetWindowThreadProcessId(ft, None)
    t2 = ctypes.windll.kernel32.GetCurrentThreadId()
    u32.AttachThreadInput(t1, t2, True)
    u32.BringWindowToTop(hwnd)
    ok = u32.SetForegroundWindow(hwnd)
    u32.AttachThreadInput(t1, t2, False)
    time.sleep(0.5)
    return ok, u32.GetForegroundWindow()


def dbg_lines():
    if not os.path.exists(DEBUG_PATH):
        return []
    return [x for x in open(DEBUG_PATH, encoding="utf-8").read().splitlines() if x.strip()]


def audit_stat():
    if not os.path.exists(AUDIT_PATH):
        return {"exists": False, "size": -1, "lines": 0}
    sz = os.path.getsize(AUDIT_PATH)
    n = len([x for x in open(AUDIT_PATH, encoding="utf-8").read().splitlines() if x.strip()]) if sz else 0
    return {"exists": True, "size": sz, "lines": n}


def audit_recs():
    """逐行 json.loads；返回 (记录列表, 非法行列表)。"""
    if not os.path.exists(AUDIT_PATH):
        return [], []
    recs, bad = [], []
    for ln in open(AUDIT_PATH, encoding="utf-8", errors="replace").read().splitlines():
        if not ln.strip():
            continue
        try:
            recs.append(json.loads(ln))
        except Exception as e:
            bad.append(f"{type(e).__name__}: {ln[:80]}")
    return recs, bad


def dot_color(l, t):
    """标题栏最左侧 ● 的颜色（面板形态下的状态指示色）。"""
    a = grab(l + 6, t + 8, 40, 26).astype(int)
    d = np.abs(a - np.array([250, 250, 250])).sum(axis=2)
    y, x = np.unravel_index(d.argmax(), d.shape)
    return [int(v) for v in a[y, x]], int(d.max())


def wait_dbg_after(n0, needle, timeout=85):
    end = time.time() + timeout
    while time.time() < end:
        new = dbg_lines()[n0:]
        hit = [x for x in new if needle in x]
        if hit:
            return hit, new
        time.sleep(1.0)
    return [], dbg_lines()[n0:]


def select_row(x, y, tries=(0, -6, 6, -12, 12)):
    """点微信会话行并**验证**（选中行背景变绿）：点不中就在行内上下微调重试。"""
    for i, dy in enumerate(tries):
        click(x, y + dy, pause=0.15)
        time.sleep(1.5)
        px = grab(x + 75, y + dy, 3, 3).reshape(-1, 3).mean(axis=0)
        ok = bool(px[1] > px[0] + 20 and px[1] > px[2] + 20)
        print(f"      尝试{i+1}: click({x},{y+dy}) → 该行像素 {[int(v) for v in px]} "
              f"{'✅已选中' if ok else '❌未选中'}", flush=True)
        if ok:
            return True, y + dy, [int(v) for v in px]
        time.sleep(0.4)
    return False, y, None


def rows_of(left_texts):
    """把左列 OCR 文本按 y 归成「每个人一行」，nick 取该行最长文本。"""
    buckets = {}
    for a, x, y in left_texts:
        buckets.setdefault(round(y / 20) * 20, []).append(a)
    return sorted(((y, max(v, key=len)) for y, v in buckets.items()))


def nearest_palette(rgb):
    best, bd = None, 1e9
    for name, c in BALL_PALETTE.items():
        d = sum((int(a) - int(b)) ** 2 for a, b in zip(rgb, c))
        if d < bd:
            best, bd = name, d
    return best, bd ** 0.5




HEAD_SKIP = {"填群昵称", "日志", "私聊", "私即", "细判", "今日", "填关系", "群性质",
             "扫描", "完成", "需我", "点两次"}
LEFT_Y0, LEFT_Y1, LEFT_W = 88, 236, 250


def panel_texts(l, t, r, b, tag):
    p = ocr_rows(l - 4, t - 4, r - l + 8, b - t + 8, 3.0, tag)
    head = [(a, x, y) for a, x, y, _ in p if y < t + 36]
    left = [(a, x, y) for a, x, y, _ in p if x < l + LEFT_W and t + LEFT_Y0 < y < t + LEFT_Y1]
    return p, head, left


def title_of(head):
    """会话标题 = 标题栏里**最靠左**的那个非按钮/非徽标文本（面板把标题放在圆点右边）。"""
    cand = [(x, a) for a, x, y in head
            if not any(s in a for s in HEAD_SKIP) and len(a) >= 2]
    return min(cand)[1] if cand else None


def wx_geom():
    w = ww.find_main()
    if not w:
        return None, None
    wl, wt, wr, wb = w.rect
    return w, (wl, wt, 240, wb - wt)


def phase_a_clear(l, t, r, b):
    print("\n" + "-" * 74)
    print("[3a] 真点「日志」两次（<4s）→ out\\jev_audit.jsonl 归零")
    print("-" * 74, flush=True)
    bak = os.path.join(EVID, "rm3_audit_before_clear.jsonl")
    if os.path.exists(AUDIT_PATH):
        shutil.copy2(AUDIT_PATH, bak)
    before = audit_stat()
    print(f"    清空前 size={before['size']} 行数={before['lines']} → 备份 {os.path.basename(bak)}",
          flush=True)
    hdr = ocr_rows(l - 4, t - 4, r - l + 8, 70, 3.0, "rm3_flow_a_header_before.png")
    hit = next(((x, y, a) for a, x, y, _ in hdr if "日志" in a), None)
    attempts = []
    if hit:
        lx, ly, ltxt = hit
        print(f"    真实定位到按钮 {ltxt!r} @ ({lx},{ly})", flush=True)
    else:
        lx, ly, ltxt = r - 150, t + 20, "(offset)"
        print(f"    OCR 未定位到「日志」，退化偏移 ({lx},{ly})", flush=True)
    n0 = len(dbg_lines())
    after = None
    for k in (1, 2):
        click(lx, ly)
        c1 = time.time()
        time.sleep(1.8)
        click(lx, ly)
        gap = time.time() - c1
        time.sleep(1.2)
        after = audit_stat()
        attempts.append({"src": ltxt, "xy": [lx, ly], "gap_s": round(gap, 2),
                         "size_after": after["size"]})
        print(f"    第 {k} 次双点 @({lx},{ly})：间隔 {gap:.2f}s → size={after['size']}", flush=True)
        if after["size"] == 0:
            break
        # 没清掉 → 用整面板 OCR 重新定位「日志」（窄条 OCR 会读丢，实测）
        full = ocr_rows(l - 4, t - 4, r - l + 8, b - t + 8, 3.0,
                        f"rm3_flow_a_header_relocate_{k}.png")
        inhead = [(x, y, a) for a, x, y, _ in full if "日志" in a and y < t + 60]
        if not inhead:
            print("    整面板 OCR 也没读到「日志」→ 无重试坐标", flush=True)
            break
        lx, ly, ltxt = inhead[0]
        print(f"    整面板 OCR 重新定位到 {ltxt!r} @ ({lx},{ly})，再试一次", flush=True)
    gap = attempts[-1]["gap_s"]
    new = dbg_lines()[n0:]
    print(f"    清空后 size={after['size']}，debug 新增 {len(new)} 行：{new}", flush=True)
    ev["phases"]["3a"] = {"before": before, "after": after, "gap_s": gap,
                          "logbtn": [lx, ly, ltxt], "backup": bak, "dbg_new": new,
                          "attempts": attempts}
    check(f"两次「日志」点击间隔 <4s（实测 {gap:.2f}s）", gap < 4.0, f"{gap:.2f}s")
    check("真点两次后 out/jev_audit.jsonl 归零", after["size"] == 0,
          f"size={after['size']} exists={after['exists']}；尝试 {attempts}")
    return after


def phase_b_switch(l, t, r, b):
    """3b：真点微信另一个会话 → 面板标题换新 / 左列不残留旧人名 / 球色按新结论。"""
    print("\n" + "-" * 74)
    print("[3b] 真点微信另一个会话 → 面板标题换新 / 左列不残留旧会话人名 / 球色")
    print("-" * 74, flush=True)
    p, head, left = panel_texts(l, t, r, b, "rm3_flow_b_panel_before.png")
    old_title = title_of(head)
    left_before = rows_of(left)
    dot_before, sat = dot_color(l, t)
    print(f"    切换前面板标题={old_title!r} 左列 {len(left_before)} 行={left_before} "
          f"标题栏彩色像素={dot_before}(饱和差 {sat})", flush=True)
    check("切换前能读到面板标题", bool(old_title), f"{old_title!r}")

    wx, geo = wx_geom()
    if not geo:
        check("找到微信主窗口（前置）", False, "找不到微信")
        return None
    list_rows = ocr_rows(*geo, 2.0, "rm3_flow_b_wxlist_before.png")
    cands = [r for r in list_rows if r[2] > geo[1] + 60 and len(r[0]) >= 2
             and "搜索" not in r[0] and "通讯录" not in r[0]]
    dedup = []
    for c in sorted(cands, key=lambda z: z[2]):
        if not any(abs(c[2] - d[2]) < 12 for d in dedup):
            dedup.append(c)
    print(f"    微信左列候选会话行 {len(dedup)} 个：{[(a, y) for a, x, y, s in dedup][:8]}", flush=True)
    check("微信左列能读到会话行（前置）", len(dedup) >= 2, f"{len(dedup)} 行")

    n1 = len(dbg_lines())
    accepted = None
    for i, c in enumerate(dedup, 1):
        print(f"    → 真点候选 {i} {c[0]!r} @ ({c[1]},{c[2]})", flush=True)
        if wx:
            print(f"      激活微信={activate(wx.hwnd)}", flush=True)
        ok, yy, px = select_row(c[1], c[2])
        if not ok:
            print("      → 点不中，跳过该候选", flush=True)
            continue
        hits, new = wait_dbg_after(n1, "提交分诊", timeout=85)
        if not hits:
            print(f"      → 点了但 85s 内没有「提交分诊」，日志尾部：{new[-3:]}", flush=True)
            continue
        time.sleep(3.0)
        pr = panel_bbox() or (l, t, r, b)
        p2, head2, left2 = panel_texts(pr[0], pr[1], pr[2], pr[3], f"rm3_flow_b_panel_after_{i}.png")
        accepted = {"row": c[0], "row_xy": [c[1], c[2]], "selected_y": yy, "px": px,
                    "hits": hits, "rect": list(pr), "title_after": title_of(head2),
                    "left_after": left2,
                    "panel_texts": [a for a, x, y, _ in p2]}
        break
    if not accepted:
        check("真点到另一个会话（并触发新一轮分诊）", False, "所有候选都没点中/没触发")
        return None
    check("真点到另一个会话并触发新一轮分诊（debug 日志）", bool(accepted["hits"]),
          f"{accepted['hits'][:1]}")
    t_after = accepted["title_after"]
    check("面板标题换新（不再是旧会话）", bool(t_after) and t_after != old_title,
          f"旧={old_title!r} 新={t_after!r}")
    old_names = [n for y, n in left_before]
    stale = [n for y, n in rows_of(accepted["left_after"]) if n in old_names]
    check("左列不残留旧会话人名", not stale, f"残留 {stale}（旧名单 {old_names}）")
    dot_after, sat2 = dot_color(accepted["rect"][0], accepted["rect"][1])
    print(f"    切换后标题栏彩色像素={dot_after}(饱和差 {sat2})", flush=True)
    ev["phases"]["3b"] = {k: v for k, v in accepted.items() if k != "panel_texts"}
    ev["phases"]["3b"]["old_title"] = old_title
    ev["phases"]["3b"]["left_before"] = [[y, n] for y, n in left_before]
    ev["phases"]["3b"]["dot_before"] = dot_before
    ev["phases"]["3b"]["dot_after"] = dot_after
    return accepted


def ball_color_check(rect):
    """真点 ◍ 收成小球 → 采样球体颜色 → 真点球再展开。"""
    print("\n" + "-" * 74)
    print("[3b-附] 小球颜色按新会话结论（真点 ◍ / 真点球展开）")
    print("-" * 74, flush=True)
    r0 = panel_bbox()
    if not r0:
        check("真点 ◍ 收成小球（前置）", False, "找不到面板窗口")
        return None
    click(r0[2] - 23, r0[1] + 16)          # ◍ 在标题栏右端（r-23, t+16）
    time.sleep(2.2)
    b1 = panel_rect()
    if not b1 or (b1[3] - b1[1]) > 200:
        check("真点 ◍ 收成小球（前置）", False, f"rect={b1[1:] if b1 else None}")
        return None
    check("真点 ◍ 收成小球（前置）", True, f"rect={b1[1:]} 尺寸 {b1[3]-b1[1]}x{b1[4]-b1[2]}")
    inner = grab(b1[1] + 14, b1[2] + 14, b1[3] - b1[1] - 28, b1[4] - b1[2] - 28).astype(int)
    med = [int(v) for v in np.median(inner.reshape(-1, 3), axis=0)]
    save_png(grab(b1[1] - 6, b1[2] - 6, b1[3] - b1[1] + 12, b1[4] - b1[2] + 12),
             "rm3_flow_b_ball.png")
    name, dist = nearest_palette(med)
    print(f"    小球中位色 RGB={med} → 最接近 {name}（距离 {dist:.1f}）"
          f"；对照 alert=(194,65,12)/todo=(201,154,46)/silent=(176,176,176)", flush=True)
    ev["phases"]["3b_ball"] = {"rect": list(b1[1:]), "median_rgb": med,
                               "nearest": name, "dist": round(dist, 1),
                               "png": "rm3_flow_b_ball.png"}
    check("小球颜色能真机采样并按结论映射（截图留证）", dist < 40,
          f"RGB={med} → {name} 距离 {dist:.1f}，见 rm3_flow_b_ball.png")
    cx, cy = (b1[1] + b1[3]) // 2, (b1[2] + b1[4]) // 2
    click(cx, cy)
    time.sleep(2.2)
    b2 = panel_rect()
    check("真点小球能再展开回面板", bool(b2) and (b2[3] - b2[1]) > 200,
          f"rect={b2[1:] if b2 else None}")
    return (list(b2) if b2 else list(rect))

    new = dbg_lines()[n0:]
    print(f"    两次点击间隔 {gap:.2f}s；清空后 size={after['size']}，"
          f"debug 新增 {len(new)} 行：{new}", flush=True)
    ev["phases"]["3a"] = {"before": before, "after": after, "gap_s": round(gap, 2),
                          "logbtn": [lx, ly, ltxt], "backup": bak, "dbg_new": new}
    check(f"两次「日志」点击间隔 <4s（实测 {gap:.2f}s）", gap < 4.0, f"{gap:.2f}s")
    check("真点两次后 out/jev_audit.jsonl 归零", after["size"] == 0,
          f"size={after['size']} exists={after['exists']}")


def phase_a_verify(round_tag):
    """3a 后半：等一轮真实分诊后，逐行 json.loads + seq 从 1 重排。"""
    print("\n" + "-" * 74)
    print("[3a] 等一轮真实分诊后校验审计日志：逐行 json.loads + seq 从 1 重排")
    print("-" * 74, flush=True)
    recs, bad = audit_recs()
    st = audit_stat()
    seqs = [x.get("seq") for x in recs]
    rows = [[x.get("seq"), (x.get("meta") or {}).get("scene"),
             str((x.get("meta") or {}).get("speaker"))[:18], x.get("http_status"),
             (x.get("meta") or {}).get("qset_version"),
             len(x.get("request_raw") or "")] for x in recs]
    print(f"    触发轮次={round_tag}  清空后新文件 size={st['size']} 行数={len(recs)}", flush=True)
    for r_ in rows:
        print(f"      seq={r_[0]} scene={r_[1]} speaker={r_[2]!r} http={r_[3]} "
              f"qset={r_[4]} raw_len={r_[5]}", flush=True)
    print(f"    seq 序列={seqs}", flush=True)
    ev["phases"]["3a_verify"] = {"lines": len(recs), "size": st["size"], "bad_json": bad,
                                 "seqs": seqs, "rows": rows, "round": round_tag}
    check("清空后确实又有一轮真实分诊写进审计（行数 ≥1）", len(recs) >= 1, f"{len(recs)} 行")
    check("每一行都是合法 JSON（逐行 json.loads）", not bad, f"非法 {bad[:2]}")
    check("第一条 seq 从 1 开始（清空后重排）", bool(seqs) and seqs[0] == 1, f"seqs={seqs[:8]}")
    check("seq 严格递增无重复",
          bool(seqs) and all(isinstance(a, int) and isinstance(b, int) and b > a
                             for a, b in zip(seqs, seqs[1:])), f"seqs={seqs}")
    return recs


def phase_c_rapid(l, t, r, b):
    """3c：1 秒内真点左列两个人 → 两次都进 debug 日志，右栏各有各的判断。"""
    print("\n" + "-" * 74)
    print("[3c] 1s 内真点左列两个人 → 两次提交都进 debug 日志 / 两人都能看到判断")
    print("-" * 74, flush=True)
    p, head, left = panel_texts(l, t, r, b, "rm3_flow_c_panel_before.png")
    rows = rows_of(left)
    print(f"    左列 OCR {len(rows)} 行：{rows[:6]}", flush=True)
    check("左列至少有 2 个人可点（OCR）", len(rows) >= 2, f"{len(rows)} 行")
    if len(rows) >= 2:
        (y1, n1), (y2, n2) = rows[0], rows[1]
    else:
        y1, y2 = t + 112, t + 136
        n1, n2 = "row1(几何)", "row2(几何)"
    cx = l + 72
    n0 = len(dbg_lines())
    print(f"    → 快速真点 ({cx},{y1})={n1!r} 与 ({cx},{y2})={n2!r}", flush=True)
    click(cx, y1)
    s1 = time.time()
    time.sleep(0.35)
    click(cx, y2)
    s2 = time.time()
    time.sleep(1.6)
    new = dbg_lines()[n0:]
    clicks = [x for x in new if "on_person_click" in x]
    keys = {c.split("on_person_click")[-1].strip() for c in clicks}
    print(f"    间隔 {s2-s1:.2f}s；debug 新增：{new}", flush=True)
    ev["phases"]["3c"] = {"row1": [cx, y1, n1], "row2": [cx, y2, n2],
                          "gap_s": round(s2 - s1, 2), "person_clicks": clicks, "dbg_new": new}
    check(f"两次点击间隔 <1s（实测 {s2-s1:.2f}s）", (s2 - s1) < 1.0, f"{s2-s1:.2f}s")
    check("两次点击都进了 debug 日志（≥2 条 on_person_click）", len(clicks) >= 2,
          f"{len(clicks)} 条：{clicks}")
    check("两条是不同的人", len(keys) >= 2, f"{keys}")

    rl, rt, rw, rh = l + 250, t + 88, r - l - 252, b - t - 120
    j = []
    for idx, (y, nick) in enumerate(((y1, n1), (y2, n2)), 1):
        click(cx, y)
        time.sleep(1.8)
        res = ocr_rows(rl, rt, rw, rh, 2.0, f"rm3_flow_c_judge_{idx}.png")
        txt = [a for a, x, yy, _ in res]
        j.append({"nick": nick, "right_texts": txt})
        print(f"      点 {nick!r} → 右栏 {len(txt)} 段：{txt}", flush=True)
    ev["phases"]["3c"]["judge"] = j
    check("点第一个人后右栏有判断", bool(j[0]["right_texts"]), f"{j[0]['right_texts'][:4]}")
    check("点第二个人后右栏有判断", bool(j[1]["right_texts"]), f"{j[1]['right_texts'][:4]}")
    check("两人判断不同（不是残留上一个人的）",
          bool(j[0]["right_texts"]) and j[0]["right_texts"] != j[1]["right_texts"],
          f"{j[0]['right_texts'][:3]} vs {j[1]['right_texts'][:3]}")
    return rows



def throttle_wait():
    """面板有 20s 最小重复判断间隔：切太近会被「跳过」。先等过窗口。"""
    last = None
    for x in dbg_lines():
        m = re.match(r"^(\d\d):(\d\d):(\d\d).*提交分诊", x)
        if m:
            last = int(m.group(1)) * 3600 + int(m.group(2)) * 60 + int(m.group(3))
    lt = time.localtime()
    now = lt.tm_hour * 3600 + lt.tm_min * 60 + lt.tm_sec
    if last is not None and 0 < last + 22 - now < 40:
        print(f"    距上次分诊 {now - last}s（<22s），按 20s 节流先等 {last + 22 - now}s", flush=True)
        time.sleep(last + 22 - now)


def phase_d_dm(l, t, r, b):
    """3d：真点单聊会话 → 真触发一轮 → 审计 scene=dm 含关系/身份且 http 200。"""
    print("\n" + "-" * 74)
    print("[3d] 真点单聊 → 真触发一轮 → 审计 scene=dm 的 request_raw 含关系/身份 + http 200")
    print("-" * 74, flush=True)
    throttle_wait()
    wx, geo = wx_geom()
    if not geo:
        check("找到微信主窗口（前置）", False, "找不到微信")
        return None
    list_rows = ocr_rows(*geo, 2.0, "rm3_flow_d_wxlist.png")
    cands = [r for r in list_rows if r[2] > geo[1] + 60 and len(r[0]) >= 2
             and "搜索" not in r[0] and "通讯录" not in r[0]]
    dedup = []
    for c in sorted(cands, key=lambda z: -z[2]):        # 从下往上试：单聊常在下方
        if not any(abs(c[2] - d[2]) < 12 for d in dedup):
            dedup.append(c)
    print(f"    微信左列候选（自下而上）{[(a, y) for a, x, y, s in dedup][:6]}", flush=True)
    dm_hit = None
    for i, c in enumerate(dedup[:4], 1):
        before_dm = len([x for x in audit_recs()[0] if (x.get("meta") or {}).get("scene") == "dm"])
        print(f"    → 真点候选 {i} {c[0]!r} @ ({c[1]},{c[2]})（此前 dm 记录 {before_dm} 条）", flush=True)
        if wx:
            print(f"      激活微信={activate(wx.hwnd)}", flush=True)
        n1 = len(dbg_lines())
        ok, yy, px = select_row(c[1], c[2])
        if not ok:
            print("      → 点不中，跳过", flush=True)
            continue
        hits, new = wait_dbg_after(n1, "提交分诊", timeout=85)
        if not hits:
            print(f"      → 85s 内没有「提交分诊」，尾部 {new[-2:]}", flush=True)
            continue
        time.sleep(3.5)
        pr = panel_bbox() or (l, t, r, b)
        p2, head2, left2 = panel_texts(pr[0], pr[1], pr[2], pr[3], f"rm3_flow_d_panel_{i}.png")
        head_txt = [a for a, x, y in head2]
        recs_now, _ = audit_recs()
        dm_recs = [x for x in recs_now if (x.get("meta") or {}).get("scene") == "dm"]
        print(f"      面板 head={head_txt}；audit dm 记录 {len(dm_recs)} 条", flush=True)
        if dm_recs or any("私聊" in a for a in head_txt):
            # 顶部状态行小区域截图（第 2 行：进度条 + status + 提示）
            st_l, st_t = pr[0], pr[1] + 34
            crop = grab(st_l - 2, st_t - 2, pr[2] - pr[0] + 4, 34)
            big = np.array(Image.fromarray(crop).resize(
                (crop.shape[1] * 4, crop.shape[0] * 4), Image.LANCZOS))
            save_png(big, "rm3_flow_d_header_status.png")
            st_rows = ocr_rows(st_l - 2, st_t - 2, pr[2] - pr[0] + 4, 34, 4.0, "")
            st_txt = [a for a, x, y, s in st_rows]
            print(f"      顶部状态行小区域截图 → rm3_flow_d_header_status.png；"
                  f"OCR={st_txt}", flush=True)
            dm_hit = {"row": c[0], "row_xy": [c[1], c[2]], "selected_y": yy, "hits": hits,
                      "rect": list(pr), "head": head_txt, "left": left2,
                      "status_texts": st_txt, "dm_recs": len(dm_recs)}
            break
    if not dm_hit:
        check("真点切到单聊会话（并触发新一轮分诊）", False,
              "候选都点不中/没触发/没出现私聊痕迹")
        return None
    check("真点切到单聊会话并触发新一轮分诊", True, f"{dm_hit['row']!r} → {dm_hit['hits'][:1]}")
    check("徽标/状态行出现私聊痕迹（OCR 或审计）",
          any("私聊" in a for a in dm_hit["head"]) or dm_hit["dm_recs"] > 0,
          f"head={dm_hit['head']} dm记录={dm_hit['dm_recs']}")
    if not dm_hit["status_texts"]:
        print("      注：顶部状态行像素 OCR 取不到文本 → 按约定记「真机不可判读，"
              "见 dm 审计记录」，不判 FAIL", flush=True)

    recs, bad = audit_recs()
    dm = [x for x in recs if (x.get("meta") or {}).get("scene") == "dm"]
    print(f"    审计总行数={len(recs)} 非法={len(bad)}；dm 记录 {len(dm)} 条", flush=True)
    ok200 = [x for x in dm if x.get("http_status") == 200]
    check("审计里有 scene=dm 的记录", bool(dm), f"{len(dm)} 条")
    check("dm 记录里有 http 200 的", bool(ok200), f"http={[x.get('http_status') for x in dm]}")
    last = (ok200 or dm or [{}])[-1]
    rr = last.get("request_raw") or ""
    meta = last.get("meta") or {}
    info = {"seq": last.get("seq"), "http": last.get("http_status"),
            "qset": meta.get("qset_version"), "speaker": meta.get("speaker"),
            "has_rel": "对话.关系" in rr, "has_id": "我.身份" in rr, "raw_len": len(rr),
            "rel_value_present": '"对话.关系"' in rr, "id_value_present": '"我.身份"' in rr}
    print(f"    最新 dm 记录：{json.dumps(info, ensure_ascii=False)}", flush=True)
    with open(os.path.join(EVID, "rm3_flow_d_dm_audit.json"), "w", encoding="utf-8") as f:
        json.dump({"info": info, "dm_recs": [{"seq": x.get("seq"),
                                             "http": x.get("http_status"),
                                             "speaker": (x.get("meta") or {}).get("speaker")}
                                            for x in dm]}, f, ensure_ascii=False, indent=2)
    check("dm 记录 request_raw 含「对话.关系」", info["has_rel"], f"raw_len={info['raw_len']}")
    check("dm 记录 request_raw 含「我.身份」", info["has_id"], f"raw_len={info['raw_len']}")
    check("dm 记录的 http 状态是 200", last.get("http_status") == 200,
          f"{last.get('http_status')}")
    check("dm 记录用的是私聊题集", "dm" in str(info["qset"]), f"{info['qset']}")
    ev["phases"]["3d"] = dm_hit
    ev["phases"]["3d"]["dm_audit"] = info
    return dm_hit



def main():
    t0 = time.time()
    os.makedirs(EVID, exist_ok=True)
    print("=" * 74)
    print("RM3 步骤 3a–3d（真鼠标 + 真会话 + 真文件 + 真 API）  t0=" +
          time.strftime("%H:%M:%S"))
    print("=" * 74, flush=True)
    pr = panel_rect()
    if not pr:
        check("面板在跑（前置条件）", False, "找不到可见面板窗口")
        return 1
    hid, l, t, r, b = pr
    wx = ww.find_main()
    print(f"面板 hwnd={hid} rect=({l},{t},{r},{b}) size={r-l}x{b-t}", flush=True)
    print(f"微信 rect={wx.rect if wx else None}  前台=" +
          f"{win32gui.GetWindowText(win32gui.GetForegroundWindow())[:20]!r}", flush=True)
    check("面板在跑且是面板形态（非小球）", (r - l) > 200, f"{r-l}x{b-t}")

    phase_a_clear(l, t, r, b)
    sw = phase_b_switch(l, t, r, b)
    if sw:
        l, t, r, b = sw["rect"]
    ball_rect = ball_color_check((l, t, r, b))
    if ball_rect:
        l, t, r, b = ball_rect
    phase_a_verify("3b 切会话触发的那一轮")
    phase_c_rapid(l, t, r, b)
    phase_d_dm(l, t, r, b)

    new = dbg_lines()
    tb = [x for x in new if "Traceback" in x or "未捕获异常" in x]
    check("全程 debug 日志无异常/堆栈", not tb, f"{tb[:2]}")
    se = os.path.join(EVID, "rm3_hud_stderr.log")
    if os.path.exists(se):
        txt = open(se, encoding="utf-8", errors="replace").read()
        check("面板 stderr 无 Traceback", "Traceback" not in txt, f"{len(txt)} 字节")

    ev["checks"] = checks
    ev["fails"] = fails
    ev["elapsed_s"] = round(time.time() - t0, 1)
    with open(os.path.join(EVID, "rm3_flow.json"), "w", encoding="utf-8") as f:
        json.dump(ev, f, ensure_ascii=False, indent=2)
    print("\n" + "=" * 74)
    print(f"耗时 {ev['elapsed_s']}s   通过 {len(checks)-len(fails)}/{len(checks)}"
          f"   失败项：" + (", ".join(fails) if fails else "无"))
    print("证据 → out/verify_20260923/rm3_flow.json")
    return 1 if fails else 0


if __name__ == "__main__":
    raise SystemExit(main())
