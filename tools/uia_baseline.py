"""对微信所有"有实际尺寸"的 Qt 窗口做控件树 dump，作为基线。
区分 login window（会暴露 mmui 树）与主窗口（疑似不暴露）。
"""
import ctypes
import json
import os
import sys


sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from uia_dump import UIA, all_windows, dump_tree  # noqa: E402

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


def main():
    wins = all_windows(visible_only=False)
    # 只看有实际内容的 Qt 窗口
    cands = [w for w in wins
             if w["size"][0] * w["size"][1] > 8000
             and w["class"].startswith("Qt")]
    cands.sort(key=lambda w: -w["area"])

    print(f"候选窗口 {len(cands)} 个（>8000 px² 且 Qt 类）\n")
    report = []
    for w in cands:
        t = dump_tree(w["hwnd"])
        mmui = {k: v for k, v in t.get("classes", {}).items()
                if k.startswith("mmui::")}
        w["tree_summary"] = {
            "node_count": t.get("node_count"),
            "mmui_count": sum(mmui.values()),
            "mmui_classes": mmui,
            "all_classes": t.get("classes", {}),
            "error": t.get("error"),
        }
        w["tree_nodes"] = t.get("nodes", [])
        verdict = ("✅ 有 mmui 树" if mmui else "❌ 无 mmui 树")
        print(f"[{verdict}] hwnd={w['hwnd']} {w['size'][0]}x{w['size'][1]} "
              f"vis={int(w['visible'])} title={w['title'][:30]!r}")
        print(f"    节点数={t.get('node_count')}  mmui 节点={sum(mmui.values())}")
        if mmui:
            for k, v in sorted(mmui.items(), key=lambda kv: -kv[1])[:10]:
                print(f"      {v:>4}  {k}")
        if w["tree_nodes"]:
            print("      前几个节点:")
            for n in w["tree_nodes"][:6]:
                print(f"        d={n['d']} cls={n['cls'][:38]!r} "
                      f"name={n['name'][:34]!r} ct={n['ctype']}")
        print()
        report.append(w)

    path = os.path.join(OUT, "uia_baseline.json")
    with open(path, "w", encoding="utf-8") as f:
        json.dump({"spi_flag": False, "windows": report}, f,
                  ensure_ascii=False, indent=2)
    print("→", path)


if __name__ == "__main__":
    main()
