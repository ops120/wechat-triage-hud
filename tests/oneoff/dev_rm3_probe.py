# -*- coding: utf-8 -*-
"""RM3 真机复核辅助探针（只读：枚举面板 / 截图 / 裁剪放大）。

用法：
  python .tests\\dev_rm3_probe.py enum            枚举可见面板窗口
  python .tests\\dev_rm3_probe.py shot <名字>      整面板截图 -> out/verify_20260923/rm3_<名字>.png
  python .tests\\dev_rm3_probe.py header <名字>    顶部状态行小区域截图并放大 4 倍
  python .tests\\dev_rm3_probe.py audit [N]        审计日志尾部（JSONL 解析）
不修改任何产品代码，也不改产品产物（除 out/ 下新文件）。
"""
from __future__ import annotations

import json
import os
import sys

PROJ = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PROJ)

import win32gui  # noqa: E402

from wechat_triage_hud.paths import AUDIT_PATH, DEBUG_PATH  # noqa: E402

CLS = "Qt692QWindowToolSaveBits"
OUT = os.path.join(PROJ, "out", "verify_20260923")
os.makedirs(OUT, exist_ok=True)


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


def grab(rect):
    import mss
    import numpy as np
    l, t, r, b = rect
    with mss.MSS() as s:
        return np.array(s.grab({"left": l, "top": t, "width": r - l, "height": b - t}))


def save_png(arr, path):
    import cv2
    cv2.imwrite(path, arr)


def main() -> int:
    args = sys.argv[1:]
    cmd = args[0] if args else "enum"
    ps = panels()
    print(f"visible panels: {len(ps)}")
    for h, l, t, r, b in ps:
        print(f"  hwnd={h} rect=({l},{t},{r},{b}) size={r - l}x{b - t}")
    if not ps:
        return 1
    hwnd, l, t, r, b = ps[0]

    if cmd == "enum":
        return 0
    if cmd == "shot":
        name = args[1] if len(args) > 1 else "panel"
        p = os.path.join(OUT, f"rm3_{name}.png")
        save_png(grab((l, t, r, b)), p)
        print(f"saved {p}")
        return 0
    if cmd == "header":
        # 顶部状态行：面板第 2 行文字（标题栏之下），取高度 30px、整宽，放大 4 倍
        name = args[1] if len(args) > 1 else "header"
        import cv2
        y0 = args[2] if len(args) > 2 else 34
        y0 = int(y0)
        hh = 30
        crop = grab((l, t + y0, r, t + y0 + hh))
        big = cv2.resize(crop, None, fx=4.0, fy=4.0, interpolation=cv2.INTER_NEAREST)
        p = os.path.join(OUT, f"rm3_{name}.png")
        save_png(big, p)
        print(f"saved {p}  crop=({l},{t + y0})-({r},{t + y0 + hh}) x4")
        return 0
    if cmd == "audit":
        n = int(args[1]) if len(args) > 1 else 10
        if not os.path.exists(AUDIT_PATH):
            print("no audit file")
            return 1
        lines = [x for x in open(AUDIT_PATH, encoding="utf-8").read().splitlines() if x.strip()]
        print(f"audit lines={len(lines)}")
        for ln in lines[-n:]:
            try:
                d = json.loads(ln)
            except Exception as e:
                print(f"  ILLEGAL JSON: {e} :: {ln[:120]}")
                continue
            keys = list(d.keys())
            raw = str(d.get("request_raw", ""))
            print(f"  seq={d.get('seq')} keys={keys} raw_len={len(raw)} "
                  f"has_rel={'对话.关系' in raw} has_id={'我.身份' in raw} "
                  f"scene={d.get('scene')} speaker={str(d.get('speaker'))[:16]!r}")
        return 0
    if cmd == "debugtail":
        n = int(args[1]) if len(args) > 1 else 15
        lines = [x for x in open(DEBUG_PATH, encoding="utf-8").read().splitlines() if x.strip()]
        for ln in lines[-n:]:
            print("  " + ln)
        return 0
    print(f"unknown cmd {cmd}")
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
