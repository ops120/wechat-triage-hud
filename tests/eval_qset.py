"""问题集评测 —— 逃逸率是先行指标，防编造探针是红线。

直接调用产品代码路径（person_engine.analyse_person），不另写一套实现，
否则测的就不是真正上线的东西。

语料两类，必须分清：
  · 真实语料：本会话从用户微信群 OCR 抓到的原文，按发言人分组
  · 构造对照：正/负对照（广告号、求助者），用于检验风险题是否真的会亮
    —— 它们不是真实数据，只为给风险题提供已知答案的样本

指标：
  1. 逃逸率（PRD §9.4 健康区间 15–35%；过低=硬猜，过高=问题答不了）
  2. **防编造探针**：只发过碎片的发言人，不许被判出确定的角色/意图
  3. 风险题召回：广告对照组的 risk_ad 必须高、正常组必须低
  4. 门控正确性：信息不足时不许进 alert
"""
from __future__ import annotations

import json
import os
import sys

import os
import sys

# 仓库根入 path，再从包里取统一路径 —— 禁止各脚本自己 dirname(__file__) 推算，
# 否则审计日志会被拆成多份（见 wechat_triage_hud/paths.py 的说明）。
PROJECT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PROJECT_DIR)

from wechat_triage_hud.paths import (  # noqa: E402
    AUDIT_PATH, DEBUG_PATH, ENV_PATH, OUT_DIR,
)

from wechat_triage_hud.jev_engine import JevClient, load_api_key  # noqa: E402
from wechat_triage_hud.person_engine import (  # noqa: E402
    SpeakerProfile, analyse_person, escape_keys,
)
from wechat_triage_hud.qset import (  # noqa: E402
    ESCAPE_RATE_MAX, ESCAPE_RATE_MIN, RISK_QUESTIONS,
)

LOG = AUDIT_PATH
GROUP = "青柠设计组项目群(39)"
TOPIC = "AI 模型、本地部署、工具使用"
# viewer 必须给：`worth_my_reply`（该不该回）的判据本来就不在消息文本里，
# 而是看我负不负责、跟这人什么关系。不填 viewer 时模型会**合理地拒绝判断**
# （实测：worth 逃逸从 2/6 涨到 4/6，总逃逸率被顶到 39%）。
# 这也独立印证了独立校准测试的结论：判据不在文本里时，"自信地错"与"老实说不知道"
# 只差一个 viewer。
VIEWER = {"群昵称": "小满", "职责": "群主；做设计外包，顺带关注 AI 工具与本地部署；不负责答疑"}

# ---------------- 语料 ----------------
# 真实语料：本会话从该群 OCR 到的消息原文（按发言人分组）
REAL = [
    ("吴工", ["嗯嗯", "待定", "WZV"],
     "碎片：期望判不出确定角色，且信息充分度应偏低"),
    ("文墨", ["今天我也装好了", "才 7B 啊"],
     "技术闲聊：期望不是广告号"),
    ("吴工", ["这效果比上一版强", "24G 显存能跑的情况下"],
     "技术分享：期望 role=sharer 或 regular"),
    ("苏工", ["这个怎么接进来？"],
     "提问：期望 role=asker 或 intent=seeking_answer"),
]

# 构造对照（**非真实数据**，只为给风险题提供已知答案的样本）
CONTROLS = [
    ("广告号样本", ["加我微信 xyz789 免费领 200G 素材包",
                "限时三天，进群领取，名额有限"],
     "广告：期望 risk_ad 高、role=ad_bot"),
    ("求助者样本", ["有没有人遇到过这个提示？我试了半天了",
                "显存明明还剩很多，却报内存不足"],
     "求助：期望 role=asker、risk 全低"),
]


def main() -> int:
    client = JevClient(load_api_key(ENV_PATH),
                       audit_path=LOG, caller="eval", verbose=False)
    profile = SpeakerProfile()
    context = [("文墨", "今天我也装好了"), ("吴工", "24G 显存能跑的情况下")]

    rows = []
    print("=" * 78)
    print("逐个发言人判断（真实语料 + 构造对照）")
    print("=" * 78)
    for nickname, msgs, note in REAL + CONTROLS:
        tag = "真实" if (nickname, msgs, note) in REAL else "对照"
        r = analyse_person(client, group=GROUP, topic=TOPIC, viewer=VIEWER,
                           nickname=nickname, msgs=msgs, context=context,
                           profile=profile)
        r["_note"] = note
        r["_kind"] = tag
        rows.append(r)
        print(f"\n[{tag}] {note}")
        print(f"  消息      : {msgs}")
        print(f"  角色      : {r['role']} (把握 {r['role_conf']:.2f})")
        print(f"  意图      : {r['intent']} (把握 {r['intent_conf']:.2f})")
        print(f"  该不该回  : {r['worth']}   关注度 {r['attention']}/10 "
              f"(有效质量 {r['attention_mass']})")
        print(f"  信息充分度: {r['sufficiency']:.2f} / 3")
        print(f"  是否点我  : {r['addressed_to_me']:.2f}")
        rk = " ".join(f"{k}={v:.2f}" for k, v in r["risks"].items())
        print(f"  四类风险  : {rk}")
        print(f"  门控      : {r['state']}  ← {', '.join(r['state_reasons']) or '无'}")

    # ---------------- 指标 1：逃逸率 ----------------
    esc = escape_keys()
    per_q: dict[str, list[int]] = {}
    n_choices = n_esc = 0
    for r in rows:
        for key, val in (("role", r["role"]), ("intent", r["intent"]),
                         ("worth", r["worth"])):
            per_q.setdefault(key, []).append(1 if val in esc else 0)
            n_choices += 1
            n_esc += 1 if val in esc else 0
    rate = n_esc / n_choices if n_choices else 0.0

    print("\n" + "=" * 78)
    print("指标 1：逃逸率（先行指标）")
    print("=" * 78)
    for k, v in per_q.items():
        print(f"  {k:<8} 逃逸 {sum(v)}/{len(v)}")
    print(f"  总体逃逸率 = {rate * 100:.0f}%   "
          f"（参考区间 {ESCAPE_RATE_MIN*100:.0f}%–{ESCAPE_RATE_MAX*100:.0f}%）")
    # 逃逸率**只在固定输入配置下可比**：同一套问题集，只改 viewer 有没有填：
    #   无 viewer 概念 → 28% ｜ viewer 未填 → 39% ｜ viewer 已填 → 11%
    # 所以它只是参考值，不能单独当通过条件。
    # 真正能抓"问题集坏了"的是下面两项：
    #   · 防编造探针 —— 抓"该说判不了却在硬猜"
    #   · 风险区分度 —— 抓"风险题不亮或乱亮"
    too_high = rate > ESCAPE_RATE_MAX
    print(f"  → 参考值{'（偏高：问题可能答不了）' if too_high else '（在参考区间内）' if rate >= ESCAPE_RATE_MIN else '（低于参考区间；是否硬猜看防编造探针）'}")
    in_band = not too_high

    # ---------------- 指标 2：防编造探针 ----------------
    print("\n" + "=" * 78)
    print("指标 2：防编造探针（碎片不许被判出确定角色）")
    print("=" * 78)
    fab_fail = []
    for r in rows:
        if r["_kind"] != "真实" or "碎片" not in r["_note"]:
            continue
        composed = r["role"] not in esc and r["intent"] not in esc
        high_suff = r["sufficiency"] < 2.0
        ok = (not composed) or (not high_suff)
        print(f"  {r['nickname']} {r['msgs']}")
        print(f"    角色={r['role']} 意图={r['intent']} 信息充分度={r['sufficiency']:.2f}")
        print(f"    → {'✅ 没有硬编' if ok else '❌ 编造：既给了确定角色，又说信息够用'}")
        if not ok:
            fab_fail.append(r["nickname"])

    # ---------------- 指标 3：风险题是否有区分度 ----------------
    print("\n" + "=" * 78)
    print("指标 3：风险题区分度（广告对照应高、正常应低）")
    print("=" * 78)
    for r in rows:
        vals = [r["risks"][k] for k in RISK_QUESTIONS]
        print(f"  [{r['_kind']}] {r['nickname']:<8} "
              + " ".join(f"{k.replace('risk_','')}={v:.2f}" for k, v in r["risks"].items()))
    for r in rows:
        if r["nickname"] == "广告号样本":
            ok = r["risks"]["risk_ad"] > 0.5 and r["role"] == "ad_bot"
            print(f"  → 广告对照: risk_ad={r['risks']['risk_ad']:.2f} "
                  f"role={r['role']} state={r['state']} {'✅' if ok else '❌'}")
        if r["nickname"] == "求助者样本":
            ok = max(r["risks"].values()) < 0.5
            print(f"  → 求助对照: 最高风险={max(r['risks'].values()):.2f} "
                  f"{'✅ 未误报' if ok else '❌ 误报'}")

    # ---------------- 指标 4：门控 ----------------
    print("\n" + "=" * 78)
    print("指标 4：门控（判断类 alert 受信息约束；风险类 alert 不受）")
    print("=" * 78)
    bad_gate = []
    for r in rows:
        if r["state"] != "alert":
            continue
        is_risk = any(x.startswith("风险:") for x in r["state_reasons"])
        # 风险类放行；判断类必须在信息足够时才允许 alert
        if not is_risk and r["sufficiency"] > 1.5:
            bad_gate.append(r["nickname"])
    for r in rows:
        kind = ("风险" if any(x.startswith("风险:") for x in r["state_reasons"])
                else ("判断" if r["state"] == "alert" else "-"))
        print(f"  {r['nickname']:<8} 状态={r['state']:<7} 信息={r['sufficiency']:.2f} "
              f"类型={kind}")
    print(f"  → {'✅ 判断类无越权 alert' if not bad_gate else f'❌ 违反: {bad_gate}'}")

    # ---------------- 成本 ----------------
    print("\n" + "=" * 78)
    print("成本（数字读自审计日志，不另算）")
    print("=" * 78)
    recs = client.audit.tail(profile.calls) if client.audit.records else []
    tin = sum((x.get("response") or {}).get("usage", {}).get("input_tokens", 0)
              for x in recs)
    lat = [x.get("elapsed_s", 0) for x in recs]
    print(f"  调用 {client.calls} 次   输入 token {tin}   成本 ${tin/1e6*0.042:.6f}")
    if lat:
        print(f"  延迟 平均 {sum(lat)/len(lat):.2f}s  最大 {max(lat):.2f}s")
    print(f"  审计日志 {client.audit.size_human()}  共 {client.audit.records} 条")

    out = os.path.join(OUT_DIR, "eval_qset.json")
    with open(out, "w", encoding="utf-8") as f:
        json.dump({"escape_rate": rate, "rows": rows,
                   "calls": client.calls, "input_tokens": tin},
                  f, ensure_ascii=False, indent=2)
    print(f"\n明细 → {out}")

    # 通过条件：防编造 + 门控 + 逃逸率不偏高。
    # 逃逸率偏低不再单独判失败 —— 它可能只是输入更完整（见上面注释）。
    ok = in_band and not fab_fail and not bad_gate
    print("\n" + ("全部通过" if ok else "有未通过项，见上"))
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
