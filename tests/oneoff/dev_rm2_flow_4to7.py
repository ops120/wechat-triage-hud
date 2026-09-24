# -*- coding: utf-8 -*-
r"""真机复核 RM2 · 步骤 4–7：真实鼠标操作 + 真实日志/审计断言。

  4) 「日志」连点两次（<4s）→ 审计文件大小必须为 0；新记录逐行 json.loads 合法、seq 从 1 起。
  5) 微信切另一个会话 → 面板标题换新、左列不残留旧会话人名、颜色指示按新结论。
  6) 1s 内快速点左列两个人 → 两次点击都进 debug 日志、两人都看得到判断。
  7) 切到单聊 → 徽标「私聊 · 直接细判」、无异常堆栈、审计 scene=dm 的 request_raw
     里有「对话.关系」「我.身份」。

一切结论来自真实 win32/mss/日志；不改产品代码。
用法（后台，全程 ~3 分钟）：Start-Process python .tests\dev_rm2_flow_4to7.py
"""
from __future__ import annotations

import ctypes
import json
import os
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
WX = (1240, 21, 240, 903)      # 微信左侧会话列表（实测 微信 rect=(1240,21,1897,924)）
OLD_TITLE = "林可"         # 切换前面板标题（实测）
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
        img.save(os.path.join(EVID, tag))
    print(f"    OCR {tag or ''} ({l},{t})+{w}x{h}: " +
          " | ".join(f"{a}({x},{y})" for a, x, y, _ in out), flush=True)
    return out


def panel_rect():
    out = []

    def cb(h, _):
        try:
            if win32gui.GetClassName(h) == CLS and win32gui.IsWindowVisible(h):
                out.append(tuple(win32gui.GetWindowRect(h)))
        except Exception:
            pass
        return True

    win32gui.EnumWindows(cb, None)
    return max(out, key=lambda r: (r[2] - r[0]) * (r[3] - r[1])) if out else None


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
    n = len([x for x in open(AUDIT_PATH, encoding="utf-8").read().splitlines() if x.strip()]) \
        if sz else 0
    return {"exists": True, "size": sz, "lines": n}


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


def wx_row(frag, rows, y_min=None, y_max=None):
    cand = [(a, x, y) for a, x, y, _ in rows
            if frag in a and (y_min is None or y >= y_min) and (y_max is None or y <= y_max)]
    return cand[0] if cand else None


def phase4(l, t):
    print("\n" + "-" * 74)
    print("[步骤 4] 「日志」连点两次（<4s）→ 审计文件必须为 0 字节")
    print("-" * 74, flush=True)
    bak = os.path.join(EVID, "rm2_audit_before_clear.jsonl")
    if os.path.exists(AUDIT_PATH):
        shutil.copy2(AUDIT_PATH, bak)
    before = audit_stat()
    print(f"    清空前：size={before['size']} 行数={before['lines']} → 备份 {os.path.basename(bak)}",
          flush=True)
    hdr = ocr_rows(l - 4, t - 4, 3, 36, 2.0, "rm2_s4_header_before.png")
    hit = next(((x, y, a) for a, x, y, _ in hdr if "日志" in a or "志" in a), None)
    if hit:
        lx, ly, ltxt = hit
        print(f"    定位按钮 {ltxt!r} @ ({lx},{ly})", flush=True)
    else:
        lx, ly, ltxt = l + 412, t + 20, "(offset)"
        print(f"    OCR 未定位到「日志」，退化偏移 ({lx},{ly})", flush=True)
    n0 = len(dbg_lines())
    click(lx, ly)
    c1 = time.time()
    time.sleep(1.8)
    click(lx, ly)
    gap = time.time() - c1
    time.sleep(1.0)
    after = audit_stat()
    ev["phases"]["4"] = {"before": before, "after": after, "gap_s": round(gap, 2),
                         "logbtn": [lx, ly, ltxt], "backup": bak,
                         "dbg_new": dbg_lines()[n0:]}
    check(f"两次点击间隔 <4s（实测 {gap:.2f}s）", gap < 4.0, f"{gap:.2f}s")
    check("点击后 out/jev_audit.jsonl 大小为 0", after["size"] == 0,
          f"size={after['size']} exists={after['exists']}")
    return after


def phase5(l, t, r, b):
    print("\n" + "-" * 74)
    print("[步骤 5] 微信切到另一个会话 → 面板标题换新 / 不残留旧人名 / 颜色按新结论")
    print("-" * 74, flush=True)
    pb = ocr_rows(l - 4, t - 4, r - l + 8, b - t + 8, 3.0, "rm2_s5_panel_before.png")
    dot_before, sat = dot_color(l, t)
    print(f"    切换前标题栏彩色像素 RGB={dot_before}（饱和度差 {sat}）", flush=True)

    wx = ocr_rows(*WX, 2.0, "rm2_s5_wxlist_before.png")
    cands = []
    for frag in ("青柠设计组项目群", "群聊", "小婷"):
        c = wx_row(frag, wx, y_min=100)
        if c and not any(abs(c[2] - u[2]) < 12 for u in cands):
            cands.append(c)
    check("微信列表里找到候选会话行", bool(cands), f"{cands}")
    if not cands:
        return None
    wxh = ww.find_main()
    tries, accepted = [], None
    for i, c in enumerate(cands, 1):
        print(f"    → 激活微信后点击候选 {i} {c[0]!r} @ ({c[1]},{c[2]})", flush=True)
        if wxh:
            print(f"      激活={activate(wxh.hwnd)}", flush=True)
        n1 = len(dbg_lines())
        sel_ok, sel_y, sel_px = select_row(c[1], c[2])
        if not sel_ok:
            print(f"      → 候选 {i} 点不中（5 次尝试都没选中），跳过", flush=True)
            tries.append({"row": c, "selected": False})
            continue
        hits, new = wait_dbg_after(n1, "提交分诊", timeout=85)
        time.sleep(2.5)
        pa = ocr_rows(l - 4, t - 4, r - l + 8, b - t + 8, 3.0,
                      f"rm2_s5_panel_after_{i}.png")
        head = [a for a, x, y, _ in pa if y < t + 60]
        left = [(a, x, y) for a, x, y, _ in pa if x < l + 250 and t + 88 < y < t + 232]
        rows = rows_of(left)
        n_log = max([int(m) for m in __import__("re").findall(r"提交分诊: (\d+) 人",
                                                             " ".join(hits))] or [0])
        tries.append({"row": c, "selected": [sel_y, sel_px], "triage_hits": hits,
                      "header": head, "left": left, "n_rows": len(rows),
                      "n_people_log": n_log})
        if len(rows) >= 2 or n_log >= 2:
            accepted = {"row": c, "hits": hits, "head": head, "left": left,
                        "rows": rows, "pa": pa, "n_log": n_log}
            break

    check("切会话后触发新一轮分诊（debug 日志）", any(t["triage_hits"] for t in tries),
          f"{[t['triage_hits'][:1] for t in tries]}")
    ev["phases"]["5"] = {"candidates": cands, "tries": tries,
                         "accepted": (accepted or {}).get("row"),
                         "dot_before": dot_before}
    if not accepted:
        check("找到一个有 ≥2 人的会话（用于步骤 6）", False,
              f"试了 {len(tries)} 个候选，都是 {[t['n_rows'] for t in tries]} 行")
        return None

    title_new = next((a for a in accepted["head"] if a not in ("填关系",)
                      and "日志" not in a and "私聊" not in a and "今日" not in a), None)
    pr2 = panel_rect() or (l, t, r, b)
    if pr2 != (l, t, r, b):
        print(f"    ⚠ 切换后面板尺寸变了：{(l,t,r,b)} → {pr2}（后续阶段用新 rect）", flush=True)
        # 面板变宽后重新取一次标题/左列，避免用旧 rect 的坐标
        pa2 = ocr_rows(pr2[0] - 4, pr2[1] - 4, pr2[2] - pr2[0] + 8, pr2[3] - pr2[1] + 8,
                       3.0, "rm2_s5_panel_after_swap.png")
        head2 = [a for a, x, y, _ in pa2 if y < pr2[1] + 60]
        title2 = next((a for a in head2 if a not in ("填关系",)
                       and "日志" not in a and "私聊" not in a and "今日" not in a), None)
        if title2:
            title_new = title2
        left_after = [(a, x, y) for a, x, y, _ in pa2
                      if x < pr2[0] + 250 and pr2[1] + 88 < y < pr2[1] + 232]
        accepted["rows"] = rows_of(left_after)
    dot_after, sat2 = dot_color(l, t)
    ev["phases"]["5"]["title_new"] = title_new
    ev["phases"]["5"]["dot_after"] = dot_after
    left_after = accepted["left"]
    check("面板标题换新（不再是旧 DM）",
          bool(title_new) and OLD_TITLE not in (title_new or ""),
          f"新标题={title_new!r} 旧={OLD_TITLE!r}")
    check("左列不残留旧会话人名", not [x for x in left_after if OLD_TITLE in x[0]],
          f"残留 {[x for x in left_after if OLD_TITLE in x[0]]}")
    check("新会话左列有 ≥2 人可点", len(accepted["rows"]) >= 2,
          f"{len(accepted['rows'])} 行 {accepted['rows'][:4]}")
    print(f"    切换后标题栏彩色像素 RGB={dot_after}", flush=True)
    return {"left_after": left_after, "pa": accepted["pa"],
            "rows": accepted["rows"], "rect": pr2}


def green_at(x, y):
    px = grab(x, y, 3, 3).reshape(-1, 3).mean(axis=0)
    return [int(v) for v in px], bool(px[1] > px[0] + 20 and px[1] > px[2] + 20)


def select_row(x, y, tries=(0, -6, 6, -12, 12)):
    """点微信会话行并**验证**（选中行背景变绿）：点不中就在行内上下微调重试。

    实测教训：合成点击在微信会话列表上时灵时不灵（同一坐标一次成功一次失败），
    不验证就会把「没切过去」误判成「面板不更新」。
    """
    for i, dy in enumerate(tries):
        click(x, y + dy, pause=0.15)
        time.sleep(1.5)
        px, ok = green_at(1315, y + dy)
        print(f"      尝试{i+1}: click({x},{y+dy}) → 该行像素 {px} "
              f"{'✅已选中' if ok else '❌未选中'}", flush=True)
        if ok:
            return True, y + dy, px
        time.sleep(0.4)
    return False, y, None


def rows_of(left_texts):
    """把左列 OCR 文本按 y 归成「每个人一行」，nick 取该行最长文本。"""
    buckets = {}
    for a, x, y in left_texts:
        buckets.setdefault(round(y / 20) * 20, []).append(a)
    return sorted(((y, max(v, key=len)) for y, v in buckets.items()))


def phase6(l, t, r, b, info):
    print("\n" + "-" * 74)
    print("[步骤 6] 1s 内快速点击左列两个人 → 两次点击进日志 / 两人都看得到判断")
    print("-" * 74, flush=True)
    rows = rows_of(info["left_after"])
    n_log = info.get("n_log") or 0
    print(f"    左列 OCR {len(rows)} 行：{rows[:6]}（debug 日志：本轮分诊 {n_log} 人）", flush=True)
    if len(rows) >= 2:
        (y1, n1), (y2, n2) = rows[0], rows[1]
    else:
        # 面板压在窗口上时 OCR 会漏行 —— 按实测版式几何推定两行（间距仍由日志断言兜底）
        y1, y2 = t + 112, t + 136
        n1, n2 = "row1(几何)", "row2(几何)"
        print(f"    OCR 行不足 → 按版式推 y={y1},{y2}", flush=True)
    check("左列至少有 2 个人可点（OCR 或几何推定）", n_log >= 2 or len(rows) >= 2,
          f"OCR {len(rows)} 行 / 日志 {n_log} 人")
    cx = l + 72                      # 实测：nick 标签横向中心 ≈ 面板左 + 72
    n0 = len(dbg_lines())
    print(f"    → 快速点击 ({cx},{y1})={n1!r} 与 ({cx},{y2})={n2!r}", flush=True)
    click(cx, y1)
    s1 = time.time()
    time.sleep(0.35)
    click(cx, y2)
    s2 = time.time()
    time.sleep(1.5)
    new = dbg_lines()[n0:]
    clicks = [x for x in new if "on_person_click" in x]
    ev["phases"]["6a"] = {"row1": [cx, y1, n1], "row2": [cx, y2, n2],
                          "gap_s": round(s2 - s1, 2), "dbg_new": new,
                          "person_clicks": clicks}
    check(f"两次点击间隔 <1s（实测 {s2-s1:.2f}s）", (s2 - s1) < 1.0, f"{s2-s1:.2f}s")
    check("两次点击都进了 debug 日志（2 条 on_person_click）", len(clicks) >= 2,
          f"{len(clicks)} 条：{clicks}")
    keys = {c.split("on_person_click")[-1].strip() for c in clicks}
    check("两条是不同的人", len(keys) >= 2, f"{keys}")

    rl, rt, rw, rh = l + 250, t + 88, r - l - 252, b - t - 120
    j = []
    for y, nick in ((y1, n1), (y2, n2)):
        click(cx, y)
        time.sleep(1.6)
        res = ocr_rows(rl, rt, rw, rh, 2.0,
                       f"rm2_s6_judge_{'1' if not j else '2'}.png")
        txt = [a for a, x, yy, _ in res]
        j.append({"nick": nick, "right_texts": txt})
        print(f"      点 {nick!r} → 右栏：{txt}", flush=True)
    check("点第一个人后右栏有判断", bool(j[0]["right_texts"]), f"{j[0]['right_texts']}")
    check("点第二个人后右栏有判断", bool(j[1]["right_texts"]), f"{j[1]['right_texts']}")
    check("两人判断不同（不是残留上一个人的）",
          j[0]["right_texts"] != j[1]["right_texts"],
          f"{j[0]['right_texts']} vs {j[1]['right_texts']}")
    ev["phases"]["6b"] = j

    a1 = grab(rl, rt, rw, rh)
    u32.SetCursorPos(int(rl + 30), int(rt + rh // 2))
    time.sleep(0.2)
    for _ in range(6):
        u32.mouse_event(0x0800, 0, 0, -120, 0)
        time.sleep(0.1)
    time.sleep(0.8)
    a2 = grab(rl, rt, rw, rh)
    diff = float(np.abs(a1.astype(int) - a2.astype(int)).mean())
    ev["phases"]["6c_scroll"] = round(diff, 3)
    check("[C'] 群聊下滚轮滚动右栏确实变化", diff > 1.0, f"像素差 {diff:.2f}")
    return rows


def phase7(l, t, r, b):
    print("\n" + "-" * 74)
    print("[步骤 7] 切到单聊 → 徽标「私聊 · 直接细判」/ 无堆栈 / 审计 dm 记录含关系与身份")
    print("-" * 74, flush=True)
    # 面板有 20s 最小分诊间隔（hud.py:1468）：切太近会被「跳过」，且不再重试 → 先等过窗口
    import re as _re
    _last = None
    for x in dbg_lines():
        m = _re.match(r"^(\d\d):(\d\d):(\d\d).*提交分诊", x)
        if m:
            _last = int(m.group(1)) * 3600 + int(m.group(2)) * 60 + int(m.group(3))
    _lt = time.localtime()
    _now = _lt.tm_hour * 3600 + _lt.tm_min * 60 + _lt.tm_sec
    if _last is not None and 0 < _last + 22 - _now < 40:
        print(f"    距上次分诊 {_now - _last}s（<22s），按 20s 节流先等 "
              f"{_last + 22 - _now}s", flush=True)
        time.sleep(_last + 22 - _now)
    n0 = len(dbg_lines())
    wx = ocr_rows(*WX, 2.0, "rm2_s7_wxlist.png")
    dm = wx_row("林可", wx, y_min=600) or wx_row("文件传输助手", wx)
    check("微信列表里找到单聊会话行", bool(dm), f"{dm}")
    if not dm:
        return None
    print(f"    → 真鼠标点击单聊行 {dm[0]!r} @ ({dm[1]},{dm[2]})", flush=True)
    wxh = ww.find_main()
    if wxh:
        print(f"      激活={activate(wxh.hwnd)}", flush=True)
    sel_ok, sel_y, sel_px = select_row(dm[1], dm[2])
    check("单聊行已真的被选中（列表高亮像素验证）", sel_ok, f"y={sel_y} px={sel_px}")
    if not sel_ok:
        return None
    hits, new = wait_dbg_after(n0, "提交分诊", timeout=85)
    check("切单聊后触发新一轮分诊（debug 日志）", bool(hits), f"{hits[:1] or new[-3:]}")
    time.sleep(2.5)
    pr7 = panel_rect() or (l, t, r, b)
    l, t, r, b = pr7
    p = ocr_rows(l - 4, t - 4, r - l + 8, b - t + 8, 3.0, "rm2_s7_panel_dm.png")
    badge = [a for a, x, y, _ in p if "私聊" in a or "细判" in a or "私即" in a]
    head = [a for a, x, y, _ in p if y < t + 60]
    # OCR 会把「聊」认成「即」（实测 conf≈0.65），断言做同义归一
    badge_norm = " ".join(badge).replace("即", "聊")
    ev["phases"]["7"] = {"dm_row": dm, "triage_hits": hits, "badge_texts": badge,
                         "badge_norm": badge_norm, "header_texts": head,
                         "panel_rect": list(pr7)}
    check("面板出现「私聊 · 直接细判」徽标",
          "私聊" in badge_norm and "细判" in badge_norm, f"{badge}")
    allnew = dbg_lines()[n0:]
    bad = [x for x in allnew if any(k in x for k in ("Traceback", "Exception", "错误"))]
    check("切单聊全程 debug 日志无异常/堆栈", not bad, f"{bad[:2]}")
    errf = os.path.join(EVID, "rm2_hud_stderr.log")
    err = open(errf, encoding="utf-8", errors="replace").read() if os.path.exists(errf) else ""
    check("面板 stderr 无 Traceback", "Traceback" not in err, f"stderr {len(err)} 字节")

    print("\n    [4b] 校验清空后新写入的审计记录", flush=True)
    end = time.time() + 60
    while time.time() < end and audit_stat()["lines"] < 1:
        time.sleep(2.0)
    st = audit_stat()
    lines = [x for x in open(AUDIT_PATH, encoding="utf-8").read().splitlines() if x.strip()] \
        if (os.path.exists(AUDIT_PATH) and st["size"]) else []
    recs, badjson = [], []
    for i, ln in enumerate(lines, 1):
        try:
            recs.append(json.loads(ln))
        except Exception as e:
            badjson.append((i, str(e)[:60]))
    seqs = [x.get("seq") for x in recs]
    ev["phases"]["4b"] = {"lines": len(lines), "size": st["size"], "bad_json": badjson,
                          "seqs": seqs,
                          "scenes": [(x.get("meta") or {}).get("scene") for x in recs]}
    print(f"    清空后新文件：size={st['size']} 行数={len(lines)} seq={seqs}", flush=True)
    check("每一行都是合法 JSON（逐行 json.loads）", not badjson, f"非法 {badjson[:2]}")
    check("第一条 seq 从 1 开始", bool(seqs) and seqs[0] == 1, f"seqs={seqs[:6]}")
    check("seq 严格递增无重复",
          bool(seqs) and all(isinstance(a, int) and isinstance(b, int) and b > a
                             for a, b in zip(seqs, seqs[1:])), f"seqs={seqs}")

    dm_recs = [x for x in recs if (x.get("meta") or {}).get("scene") == "dm"]
    check("审计里有 scene=dm 的记录", bool(dm_recs), f"{len(dm_recs)} 条")
    if dm_recs:
        rr = dm_recs[-1].get("request_raw") or ""
        ev["phases"]["7"]["dm_record"] = {
            "seq": dm_recs[-1].get("seq"), "has_rel": "对话.关系" in rr,
            "has_id": "我.身份" in rr, "raw_len": len(rr)}
        check("dm 记录 request_raw 含「对话.关系」", "对话.关系" in rr)
        check("dm 记录 request_raw 含「我.身份」", "我.身份" in rr)
        check("dm 记录用的是私聊题集",
              "dm" in str((dm_recs[-1].get("meta") or {}).get("qset_version") or ""),
              f"{(dm_recs[-1].get('meta') or {}).get('qset_version')}")
    return recs


def ball_color_probe(l, t, r, b):
    print("\n" + "-" * 74)
    print("[附] 小球颜色按新结论（收起为小球 → 采样球体颜色 → 再展开）")
    print("-" * 74, flush=True)
    click(r - 23, t + 16)            # ◍ 按钮（实测：标题栏右端第三个图标）
    time.sleep(2.0)
    pr = panel_rect()
    if not pr or pr[2] - pr[0] > 200:
        check("点击 ◍ 收成小球", False, f"rect={pr}")
        return
    check("点击 ◍ 收成小球", True, f"rect={pr} 尺寸 {pr[2]-pr[0]}x{pr[3]-pr[1]}")
    cx, cy = (pr[0] + pr[2]) // 2, (pr[1] + pr[3]) // 2
    a = grab(pr[0] + 14, pr[1] + 14, pr[2] - pr[0] - 28, pr[3] - pr[1] - 28).astype(int)
    med = [int(v) for v in np.median(a.reshape(-1, 3), axis=0)]
    print(f"    小球中位色 RGB={med}  "
          f"（对照 alert≈(194,65,12) / todo≈(201,154,46) / silent≈(176,176,176)）", flush=True)
    ev["phases"]["ball"] = {"rect": list(pr), "median_rgb": med}
    click(cx, cy)
    time.sleep(2.0)
    pr2 = panel_rect()
    check("点小球能再展开回面板", bool(pr2) and pr2[2] - pr2[0] > 200, f"rect={pr2}")
    return med


def main():
    t0 = time.time()
    os.makedirs(EVID, exist_ok=True)
    print("=" * 74)
    print("RM2 步骤 4–7（真鼠标 + 真日志）  t0=" + time.strftime("%H:%M:%S"))
    print("=" * 74, flush=True)
    pr = panel_rect()
    if not pr:
        check("面板在跑（前置条件）", False, "找不到可见面板窗口")
        return 1
    l, t, r, b = pr
    print(f"面板 rect={pr} size={r-l}x{b-t}", flush=True)
    check("面板在跑且是面板形态（非小球）", (r - l) > 200, f"{r-l}x{b-t}")

    phase4(l, t)
    info = phase5(l, t, r, b)
    if info:
        l2, t2, r2, b2 = info["rect"]
        phase6(l2, t2, r2, b2, info)
    phase7(l, t, r, b)
    ball_color_probe(l, t, r, b)

    ev["checks"] = checks
    ev["fails"] = fails
    ev["elapsed_s"] = round(time.time() - t0, 1)
    with open(os.path.join(EVID, "rm2_flow_4to7.json"), "w", encoding="utf-8") as f:
        json.dump(ev, f, ensure_ascii=False, indent=2)
    print("\n" + "=" * 74)
    print(f"耗时 {ev['elapsed_s']}s   失败项：" + (", ".join(fails) if fails else "无"))
    print("证据 → out/verify_20260923/rm2_flow_4to7.json")
    return 1 if fails else 0


if __name__ == "__main__":
    raise SystemExit(main())

