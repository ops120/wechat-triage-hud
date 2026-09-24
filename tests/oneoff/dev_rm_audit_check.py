# -*- coding: utf-8 -*-
"""审计日志 / 调试日志体检（只读）。

用法：python tests/dev_rm_audit_check.py [检查尾行数，默认 50]
打印：文件大小、行数、逐行 json.loads 结果、首尾 seq、debug 日志里的异常/警告关键字。
"""
from __future__ import annotations

import json
import os
import sys

PROJECT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PROJECT_DIR)

from wechat_triage_hud.paths import AUDIT_PATH, DEBUG_PATH  # noqa: E402

N = int(sys.argv[1]) if len(sys.argv) > 1 else 50
BAD = ("Traceback", "SKIM_WARN", "Exception", "Error", "错误", "失败", "异常")


def main() -> int:
    print(f"审计日志 {AUDIT_PATH}")
    size = os.path.getsize(AUDIT_PATH) if os.path.exists(AUDIT_PATH) else -1
    print(f"  大小 = {size} 字节")
    lines = []
    if size > 0:
        with open(AUDIT_PATH, encoding="utf-8") as f:
            lines = [x for x in f.read().splitlines() if x.strip()]
    print(f"  行数 = {len(lines)}")
    tail = lines[-N:]
    bad = 0
    for i, ln in enumerate(tail):
        try:
            json.loads(ln)
        except Exception as e:
            bad += 1
            print(f"  ❌ 尾部第 {i + 1} 行非法 JSON：{type(e).__name__}: {e} | {ln[:80]}")
    print(f"  尾部 {len(tail)} 行 json.loads 校验：合法 {len(tail) - bad} / 非法 {bad}")
    if lines:
        seqs = []
        for ln in lines[-N:]:
            try:
                seqs.append(json.loads(ln).get("seq"))
            except Exception:
                pass
        try:
            first_rec = json.loads(lines[0])
        except Exception:
            first_rec = {}
        print(f"  全文首条 seq = {first_rec.get('seq')}")
        print(f"  尾 {len(seqs)} 条 seq = {seqs[:5]} ... {seqs[-5:]}")
    print(f"调试日志 {DEBUG_PATH}")
    if os.path.exists(DEBUG_PATH):
        dl = [x for x in open(DEBUG_PATH, encoding="utf-8").read().splitlines() if x.strip()]
        print(f"  行数 = {len(dl)}")
        hits = [x for x in dl if any(k in x for k in BAD)]
        print(f"  含异常/警告关键字行数 = {len(hits)}")
        for x in hits[-10:]:
            print("   ! " + x)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
