"""列出微信进程的所有窗口（含隐藏），并 dump 指定窗口的 UIA 控件树。
用法：
    python uia_dump.py            # 列举所有窗口 + 自动 dump 最大窗口
    python uia_dump.py <hwnd>     # dump 指定窗口
    python uia_dump.py --deep     # dump 全部窗口
"""
import ctypes
import json
import os
import sys

import comtypes.client as cc
import psutil
import win32gui

cc.GetModule("UIAutomationCore.dll")
import comtypes.gen.UIAutomationClient as uiac  # noqa: E402

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

UIA = cc.CreateObject("{ff48dba4-60ef-4201-aa87-54103eef594e}",
                      interface=uiac.IUIAutomation)


def wechat_pids():
    pids = set()
    for p in psutil.process_iter(["pid", "name"]):
        try:
            if p.info["name"] and p.info["name"].lower() in ("weixin.exe", "wechat.exe"):
                pids.add(p.info["pid"])
        except Exception:
            pass
    return pids


def all_windows(visible_only=False):
    pids = wechat_pids()
    found = []

    def cb(hwnd, _):
        try:
            vis = bool(win32gui.IsWindowVisible(hwnd))
            if visible_only and not vis:
                return True
            pid = ctypes.c_ulong()
            ctypes.windll.user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
            if pid.value in pids:
                r = win32gui.GetWindowRect(hwnd)
                found.append({
                    "hwnd": hwnd,
                    "class": win32gui.GetClassName(hwnd),
                    "title": win32gui.GetWindowText(hwnd),
                    "visible": vis,
                    "rect": list(r),
                    "size": [r[2] - r[0], r[3] - r[1]],
                    "area": (r[2] - r[0]) * (r[3] - r[1]),
                })
        except Exception:
            pass
        return True

    win32gui.EnumWindows(cb, None)
    return found


def dump_tree(hwnd, max_nodes=6000):
    walker = UIA.RawViewWalker
    try:
        root = UIA.ElementFromHandle(hwnd)
    except Exception as e:
        return {"error": f"{type(e).__name__}: {e}"}
    nodes = []
    stack = [(root, 0)]
    n = 0
    while stack and n < max_nodes:
        el, d = stack.pop()
        n += 1
        try:
            cn = el.CurrentClassName or ""
            nm = el.CurrentName or ""
            ct = el.CurrentControlType
            aid = ""
            try:
                aid = el.CurrentAutomationId or ""
            except Exception:
                pass
            # 只记录深度 <= 6 的节点，避免刷屏
            if d <= 6:
                nodes.append({"d": d, "cls": cn, "name": nm[:60],
                              "ctype": ct, "aid": aid})
            child = walker.GetFirstChildElement(el)
            kids = []
            while child:
                kids.append(child)
                child = walker.GetNextSiblingElement(child)
            for k in reversed(kids):
                stack.append((k, d + 1))
        except Exception:
            pass
    # 汇总类名
    classes = {}
    for x in nodes:
        classes[x["cls"]] = classes.get(x["cls"], 0) + 1
    return {"nodes": nodes, "node_count": n,
            "classes": dict(sorted(classes.items(), key=lambda kv: -kv[1]))}


def main():
    args = sys.argv[1:]
    wins = all_windows(visible_only=False)
    print(f"微信窗口总数（含隐藏）: {len(wins)}")
    for w in sorted(wins, key=lambda x: -x["area"]):
        print(f"  hwnd={w['hwnd']:<10} vis={int(w['visible'])} "
              f"size={w['size'][0]}x{w['size'][1]:<5} "
              f"class={w['class']:<26} title={w['title'][:40]!r}")

    if not wins:
        print("没有找到微信窗口")
        return

    os.makedirs(OUT, exist_ok=True)
    result = {"windows": sorted(wins, key=lambda x: -x["area"])}

    targets = []
    if args and args[0].isdigit():
        h = int(args[0])
        targets = [w for w in wins if w["hwnd"] == h] or [wins[0]]
    elif "--deep" in args:
        targets = sorted(wins, key=lambda x: -x["area"])
    else:
        targets = [max(wins, key=lambda x: x["area"])]

    for t in targets:
        print(f"\n=== dump hwnd={t['hwnd']} class={t['class']} "
              f"size={t['size'][0]}x{t['size'][1]} ===")
        tree = dump_tree(t["hwnd"])
        t["tree"] = tree
        if "error" in tree:
            print("  ERROR:", tree["error"])
            continue
        print(f"  节点数={tree['node_count']}")
        print("  类名分布:")
        for k, v in list(tree["classes"].items())[:25]:
            flag = "  <== mmui" if k.startswith("mmui::") else ""
            print(f"    {v:>4}  {k}{flag}")

    path = os.path.join(OUT, "uia_tree.json")
    with open(path, "w", encoding="utf-8") as f:
        json.dump(result, f, ensure_ascii=False, indent=2)
    print(f"\n完整结果 → {path}")


if __name__ == "__main__":
    main()
