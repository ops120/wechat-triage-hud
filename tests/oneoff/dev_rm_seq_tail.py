# -*- coding: utf-8 -*-
"""真机复核第二轮用的只读探针：打印审计日志尾部的 seq 递增情况。

用法：python tests/dev_rm_seq_tail.py [尾行数，默认 6]

为什么单独写它：真机上抓到的缺陷是"大记录下 seq 恒为 1"（见 MEMO §6.8），
验证修复的唯一办法就是**看真记录里 seq 是否严格递增** —— 离线测试的小记录测不出来。
"""
from __future__ import annotations

import json
import os
import sys

PROJECT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PROJECT_DIR)

from wechat_triage_hud.paths import AUDIT_PATH  # noqa: E402

N = int(sys.argv[1]) if len(sys.argv) > 1 else 6


def main() -> int:
    with open(AUDIT_PATH, encoding="utf-8") as f:
        lines = [x for x in f.read().splitlines() if x.strip()]
    print(f"审计日志 {os.path.basename(AUDIT_PATH)}：共 {len(lines)} 行")
    recs = [json.loads(x) for x in lines[-N:]]
    for r in recs:
        print(f"  seq={r.get('seq')}  ts={r.get('ts')}  "
              f"who={r.get('meta', {}).get('speaker')}  "
              f"qset={r.get('meta', {}).get('qset_version')}  "
              f"http={r.get('http_status')}")
    seqs = [r.get("seq") for r in recs]
    inc = all(isinstance(a, int) and isinstance(b, int) and b > a
              for a, b in zip(seqs, seqs[1:]))
    print(f"尾 {len(seqs)} 条 seq = {seqs} → "
          f"{'严格递增 ✅' if inc else '有重复或回退 ❌'}")
    return 0 if inc else 1


if __name__ == "__main__":
    raise SystemExit(main())
