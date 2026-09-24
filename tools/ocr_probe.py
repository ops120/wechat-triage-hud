"""OCR 探针：截取真实微信窗口，OCR 出文字与坐标，用于推断布局与收发判定规则。
结果写 UTF-8 JSON，避免终端编码问题。
"""
import json
import os
import sys

import mss
import numpy as np
from rapidocr_onnxruntime import RapidOCR

import os
import sys

# 仓库根入 path，再从包里取统一路径 —— 禁止各脚本自己 dirname(__file__) 推算，
# 否则审计日志会被拆成多份（见 wechat_triage_hud/paths.py 的说明）。
PROJECT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PROJECT_DIR)

from wechat_triage_hud.paths import (  # noqa: E402
    AUDIT_PATH, DEBUG_PATH, ENV_PATH, OUT_DIR,
)

OUT = OUT_DIR
os.makedirs(OUT, exist_ok=True)


def find_wechat_window():
    import ctypes

    import win32gui
    best = None

    def cb(h, _):
        nonlocal best
        try:
            if not win32gui.IsWindowVisible(h):
                return True
            if not win32gui.GetClassName(h).startswith("Qt51514QWindowIcon"):
                return True
            r = win32gui.GetWindowRect(h)
            area = (r[2] - r[0]) * (r[3] - r[1])
            title = win32gui.GetWindowText(h)
            # 排除托盘消息窗/无标题小窗
            if area > 100000 and title and title != "Weixin":
                pid = ctypes.c_ulong()
                ctypes.windll.user32.GetWindowThreadProcessId(h, ctypes.byref(pid))
                if best is None or area > best["area"]:
                    best = {"hwnd": h, "pid": pid.value, "rect": list(r),
                            "area": area, "title": title}
        except Exception:
            pass
        return True

    win32gui.EnumWindows(cb, None)
    return best


def main():
    win = find_wechat_window()
    if not win:
        print("未找到微信主窗口")
        return 1
    l, t, r, b = win["rect"]
    print(f"窗口: hwnd={win['hwnd']} {r-l}x{b-t} at ({l},{t}) title={win['title']!r}")

    with mss.mss() as sct:
        img = np.array(sct.grab({"left": l, "top": t, "width": r - l, "height": b - t}))
    img = img[:, :, :3]  # BGRA -> BGR

    ocr = RapidOCR()
    result, _ = ocr(img)
    if not result:
        print("OCR 未识别到任何文字")
        return 1

    lines = []
    for box, text, score in result:
        xs = [p[0] for p in box]
        ys = [p[1] for p in box]
        lines.append({
            "text": text, "score": round(float(score), 3),
            "x0": int(min(xs)), "x1": int(max(xs)),
            "y0": int(min(ys)), "y1": int(max(ys)),
            "cy": int((min(ys) + max(ys)) / 2),
        })
    lines.sort(key=lambda x: (x["cy"], x["x0"]))

    print(f"识别到 {len(lines)} 行文字\n")
    print(f"窗口宽 {r-l}，高 {b-t}")
    print(f"{'y0':>5} {'x0':>5} {'x1':>5}  文本")
    for ln in lines:
        print(f"{ln['y0']:>5} {ln['x0']:>5} {ln['x1']:>5}  {ln['text'][:44]}")

    path = os.path.join(OUT, "ocr_probe.json")
    with open(path, "w", encoding="utf-8") as f:
        json.dump({"window": win, "size": [r - l, b - t], "lines": lines},
                  f, ensure_ascii=False, indent=2)
    print(f"\n→ {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
