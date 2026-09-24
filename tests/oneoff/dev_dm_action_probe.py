# -*- coding: utf-8 -*-
"""私聊「立刻回答 / 最佳动作」两问探针（真机数据 + 真实 API）。

要复现的效果（用户给的目标形状）：

    Jev:
    是否应该立刻回答具体内容？
    - Yes: 18%
    - No: 82%

    最佳动作
    - 搜索聊天记录: 91%
    - 硬猜: 4%
    - 转移话题: 1%
    - 装死: 4%

做法：从 `out/jev_audit.jsonl` 里取最近一次**真实私聊**的 state（原文不动），
只把问题换成这两问，走项目自己的 JevClient 发出去 —— 因此这次调用同样会落进
审计日志（caller=probe-dm-action），结论可回查。

用法：
    python tests/dev_dm_action_probe.py            # 用最近一条真实私聊
    python tests/dev_dm_action_probe.py --list     # 列出可选的私聊样本
    python tests/dev_dm_action_probe.py --pick 3   # 指定第 N 条（1 起）
"""
from __future__ import annotations

import json
import os
import sys

PROJECT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PROJECT_DIR)

from wechat_triage_hud.jev_engine import JevClient, load_api_key  # noqa: E402
from wechat_triage_hud.paths import AUDIT_PATH, ENV_PATH          # noqa: E402

# ---- 目标形状的两问（题面照用户给的形状写，选项用中文原词）----
DM_ACTION_QUESTIONS = {
    "answer_now": {
        "type": "noul",
        "instructions": (
            "对方刚发来的消息，我是否应该**立刻**回答它的具体内容？"
            "（「立刻」指这一条就正面回答他问的东西；先做别的、或先不回，都算否）"
        ),
        "criteria": {
            "true": "应该立刻回答：他在等我给具体内容，拖着我这边会出问题",
            "false": "不必立刻回答：可以先做别的、可以不回，或者直接答会出问题",
        },
    },
    "best_action": {
        "type": "choice",
        "instructions": (
            "就现在这条消息，我的**最佳动作**是哪一个？"
            "四个选项互斥，选最该做的那一个。"
        ),
        "criteria": {
            "search_history": "搜索聊天记录：先翻我和他的历史，确认他指的是哪件事再答",
            "guess": "硬猜：凭眼前这几句直接答，不查历史",
            "shift_topic": "转移话题：不正面答，把话题引开",
            "play_dead": "装死：不回",
        },
    },
}


def dm_samples() -> list[dict]:
    """审计日志里的真实私聊调用（按时间顺序）。"""
    out = []
    with open(AUDIT_PATH, "r", encoding="utf-8", errors="replace") as f:
        for ln in f:
            ln = ln.strip()
            if not ln:
                continue
            try:
                r = json.loads(ln)
            except json.JSONDecodeError:
                continue
            if (r.get("meta") or {}).get("scene") != "dm":
                continue
            if not r.get("request_raw"):
                continue
            try:
                req = json.loads(r["request_raw"])
            except json.JSONDecodeError:
                continue
            st = req.get("state") or {}
            if not (st.get("对方") or {}).get("最近消息"):
                continue
            out.append({"ts": r.get("ts"), "state": st,
                        "speaker": (r.get("meta") or {}).get("speaker")})
    return out


def main() -> int:
    args = sys.argv[1:]
    samples = dm_samples()
    if not samples:
        print("审计日志里没有可用的私聊样本。")
        return 2
    if "--list" in args:
        for i, s in enumerate(samples, 1):
            msgs = [m.get("内容") for m in s["state"]["对方"]["最近消息"]]
            print(f"[{i}] {s['ts']}  {s['speaker']}\n     {json.dumps(msgs, ensure_ascii=False)}")
        return 0

    pick = len(samples)
    if "--pick" in args:
        pick = int(args[args.index("--pick") + 1])

    sample = samples[max(1, min(pick, len(samples))) - 1]
    state = sample["state"]
    # 覆盖判据（contra 实验用）：证明"填了关系/身份"真的会改结论
    if "--rel" in args:
        state.setdefault("对话", {})["关系"] = args[args.index("--rel") + 1]
    if "--identity" in args:
        state.setdefault("我", {})["身份"] = args[args.index("--identity") + 1]
    if "--no-ctx" in args:                 # 去掉整段对话，只看对方这几条
        state["最近的整段对话"] = []
    print("=" * 62)
    print(f"样本 {sample['ts']}   对方：{sample['state']['对方'].get('昵称')}")
    print(f"关系：{(sample['state'].get('对话') or {}).get('关系')}　"
          f"我：{(sample['state'].get('我') or {}).get('身份')}")
    print("-" * 62)
    for m in state["对方"]["最近消息"]:
        print(f"  {m.get('序号')}. {m.get('内容')}")
    print("=" * 62)

    client = JevClient(load_api_key(ENV_PATH), audit_path=AUDIT_PATH,
                       caller="probe-dm-action", verbose=True)
    body = client.system_one(state, DM_ACTION_QUESTIONS)
    ans = body["answers"]

    # ---- 按目标形状渲染 ----
    yes = float(ans["answer_now"]["noul"])
    ac = ans["best_action"]
    probs = ac["probabilities"]
    zh = {"search_history": "搜索聊天记录", "guess": "硬猜",
          "shift_topic": "转移话题", "play_dead": "装死"}
    lines = [
        "",
        "Jev:",
        "是否应该立刻回答具体内容？",
        f"- Yes: {round(yes * 100)}%",
        f"- No: {round((1 - yes) * 100)}%",
        "",
        "最佳动作",
    ]
    for k, v in sorted(probs.items(), key=lambda kv: -kv[1]):
        lines.append(f"- {zh.get(k, k)}: {round(v * 100)}%")
    print("\n".join(lines))
    print("-" * 62)
    print(f"choice={zh.get(ac['choice'], ac['choice'])}　"
          f"confidence={ac['confidence']:.2f}　model={body.get('model')}")
    bad = abs(sum(probs.values()) - 1.0) > 0.1
    print(f"概率和={sum(probs.values()):.3f}{'  ⚠ 越界' if bad else ''}")
    out = os.path.join(PROJECT_DIR, "out", "dm_action_probe.json")
    with open(out, "w", encoding="utf-8") as f:
        json.dump({"ts": sample["ts"], "state": state, "answers": ans,
                   "model": body.get("model"), "usage": body.get("usage"),
                   "source": "真实私聊样本 + 真实 API（caller=probe-dm-action）"},
                  f, ensure_ascii=False, indent=2)
    print(f"原始响应 → {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
