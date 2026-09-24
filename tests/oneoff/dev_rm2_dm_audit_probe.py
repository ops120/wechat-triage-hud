# -*- coding: utf-8 -*-
r"""真机复核 RM2 · 步骤 7 补充：拿到一条**新鲜的** scene=dm 审计记录，验证
request_raw 里有「对话.关系」「我.身份」。

为什么需要补充：清空日志后，DM 的细判走了面板缓存（同一发言人+同一批消息+同一身份指纹），
没有产生新 API 记录；清空后写下的两条都是粗筛记录（meta=null，没有 scene/speaker）。
这里切到一个**没判过的单聊**（文件传输助手）强制走一次真实调用。

用法（后台 ~2 分钟）：Start-Process python .tests\dev_rm2_dm_audit_probe.py
"""
from __future__ import annotations

import json
import os
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__))), ".tests"))
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import dev_rm2_flow_4to7 as F  # noqa: E402

EVID = F.EVID


def audit_recs():
    if not os.path.exists(F.AUDIT_PATH):
        return []
    return [json.loads(l) for l in open(F.AUDIT_PATH, encoding="utf-8")
            if l.strip()]


def main():
    print("=" * 70)
    print("步骤 7 补充：强制一次真实单聊细判，取 scene=dm 记录  "
          + time.strftime("%H:%M:%S"))
    print("=" * 70, flush=True)
    w = F.ww.find_main()
    print(f"激活微信：{F.activate(w.hwnd)}", flush=True)
    before = audit_recs()
    print(f"切换前审计 {len(before)} 条，scenes="
          f"{[(r.get('meta') or {}).get('scene') for r in before]}", flush=True)

    rows = F.ocr_rows(*F.WX, 2.0, "rm2_s7b_wxlist.png")
    for frag in ("文件传输助手", "李工", "云海朱明"):
        row = F.wx_row(frag, rows, y_min=100)
        if row:
            break
    print(f"目标会话行 {row}", flush=True)
    ok, y, px = F.select_row(row[1], row[2])
    print(f"选中={ok} y={y} px={px}", flush=True)
    n0 = len(F.dbg_lines())
    hits, _ = F.wait_dbg_after(n0, "提交分诊", timeout=90)
    print(f"分诊触发：{hits[:2]}", flush=True)

    end = time.time() + 150
    got = []
    while time.time() < end:
        recs = audit_recs()
        got = [r for r in recs if (r.get("meta") or {}).get("scene") == "dm"]
        if got:
            break
        time.sleep(3.0)
    print(f"轮询结束：审计 {len(audit_recs())} 条，dm 记录 {len(got)} 条", flush=True)
    ok_all = False
    if got:
        rr = got[-1].get("request_raw") or ""
        info = {"seq": got[-1].get("seq"),
                "qset": (got[-1].get("meta") or {}).get("qset_version"),
                "speaker": (got[-1].get("meta") or {}).get("speaker"),
                "raw_len": len(rr), "has_rel": "对话.关系" in rr,
                "has_id": "我.身份" in rr, "http": got[-1].get("http_status")}
        print(f"最新 dm 记录：{json.dumps(info, ensure_ascii=False)}", flush=True)
        ok_all = bool(info["has_rel"] and info["has_id"])
    else:
        info = {}
    F.check("审计里出现 scene=dm 的记录", bool(got), f"{len(got)} 条")
    F.check("该记录 request_raw 含「对话.关系」", bool(got) and info.get("has_rel"))
    F.check("该记录 request_raw 含「我.身份」", bool(got) and info.get("has_id"))
    F.check("该记录是私聊题集", "dm" in str(info.get("qset")), f"{info.get('qset')}")
    with open(os.path.join(EVID, "rm2_step7b_dm_audit.json"), "w",
              encoding="utf-8") as f:
        json.dump({"row": row, "selected": ok, "triage": hits, "dm_record": info,
                   "n_audit": len(audit_recs())}, f, ensure_ascii=False, indent=2)
    print(f"\n结论：{ok_all}   证据 → out/verify_20260923/rm2_step7b_dm_audit.json",
          flush=True)
    return 0 if ok_all else 1


if __name__ == "__main__":
    raise SystemExit(main())
