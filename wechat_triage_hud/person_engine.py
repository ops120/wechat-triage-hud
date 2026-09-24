"""按人判断引擎 —— 用 qset.py 的问题集，判断群里的某一个发言人。

判定对象是**一个人**，不是单条消息： → 角色/意图/是否点我/该不该回/四类风险/信息充分度
      · 数字（关注度 0-10）由 worth_my_reply 的分布在代码里派生
      · 三态门控在代码里定，且信息不足时不许 alert
      · 四个风险各自一个 Noul，不合成单一分数
"""
from __future__ import annotations

from dataclasses import dataclass, field

import time

from .jev_engine import JevClient
from .qset import (
    MAX_CONTEXT_LINES,
    MAX_MSGS_PER_SPEAKER,
    QSET_VERSION,
    QSET_VERSION_DM,
    RISK_QUESTIONS,
    build_questions,
    build_questions_dm,
    danger_word,
    derive_attention,
    gate,
    gate_dm,
    label,
)


class InvalidAnswer(RuntimeError):
    """Jev 返回了缺失或越界的概率。

    绝不用残缺的答案继续算结论 —— 那会产出看着很确定的错误判断。
    宁可整批不采用（参考 work-with-jev 的 core.mjs:55-57）。
    """


def _prob(answers: dict, key: str, field: str) -> float:
    v = (answers.get(key) or {}).get(field)
    if not isinstance(v, (int, float)) or not (0.0 <= float(v) <= 1.0):
        raise InvalidAnswer(f"{key}.{field} 缺失或越界: {v!r}")
    return float(v)


def _probs(answers: dict, key: str) -> dict:
    p = (answers.get(key) or {}).get("probabilities")
    if not isinstance(p, dict) or not p:
        raise InvalidAnswer(f"{key}.probabilities 缺失: {p!r}")
    tot = 0.0
    for k, v in p.items():
        if not isinstance(v, (int, float)) or not (0.0 <= float(v) <= 1.0):
            raise InvalidAnswer(f"{key}.probabilities[{k}] 越界: {v!r}")
        tot += float(v)
    if not (0.9 <= tot <= 1.1):
        raise InvalidAnswer(f"{key}.probabilities 总和异常: {tot:.3f}")
    return {k: float(v) for k, v in p.items()}


@dataclass
class SpeakerProfile:
    """跨轮次累计的发言人画像（判过的记在这里，同一内容不重复计费）。"""
    seen: dict = field(default_factory=dict)     # key -> result
    analysed: int = 0
    alert_count: int = 0
    calls: int = 0

    SEEN_MAX = 500    # 缓存条数上限：面板长开不清理会无界增长

    def key_of(self, nickname: str, msgs: list[str],
               viewer: dict | None = None, scene: str = "group",
               group: str = "", qver: str = "",
               history: list[str] | None = None,
               cache_extra: str = "") -> tuple:
        """缓存键 = 场景 + 群 + 题集版本 + 人 + 消息 + **身份指纹**。

        身份必须进键：`worth_my_reply` 的判据（群里的职责、私聊里的关系）
        **不在消息文本里**。只按 (人, 消息) 做键时，用户填完身份仍然命中旧缓存、
        拿回"填之前"的结论 —— 表现成"填了没反应"（实测确认过）。

        群 / 场景 / 题集版本也必须进键（§6.6 复核）：跨群同昵称同消息且身份指纹
        相同（两边都未填）会互串结论；群聊与私聊共用一只 profile 命名空间，
        不隔开会把群口径的结论安到私聊头上；题集改版后旧结论也必须作废。
        旧缓存条目形态对不上只会 miss，无害。
        """
        fp = tuple(sorted((str(k), str(v)) for k, v in (viewer or {}).items()))
        # 累积历史也进键：判断依据变了就不能复用旧结论（否则"攒了历史但结论没变"）
        # cache_extra：额外指纹（单条消息判断用它带上"上下文末几条"，
        # 同一句话在不同上下文里结论可能不同，不能互串）
        return (scene, group, qver, nickname, tuple(msgs), fp,
                tuple(history or []), cache_extra)

    def get(self, nickname: str, msgs: list[str], viewer: dict | None = None,
            scene: str = "group", group: str = "", qver: str = "",
            history: list[str] | None = None, cache_extra: str = ""):
        return self.seen.get(self.key_of(nickname, msgs, viewer, scene, group, qver,
                                         history, cache_extra))

    def remember(self, key: tuple, value) -> None:
        """写入缓存。seen 无界增长会让长开的面板越攒越多：
        超过上限就清掉最旧的一半（dict 保序，排前面的就是最旧的）。"""
        if len(self.seen) >= self.SEEN_MAX:
            for k in list(self.seen)[:self.SEEN_MAX // 2]:
                del self.seen[k]
        self.seen[key] = value


def _earlier(history: list[str] | None, msgs: list[str]) -> list[str]:
    """本机累积的更早消息（去掉屏幕上已经有的那几条，别重复送）。

    只保留最近的一段；没有就返回空 —— 空字段直接不放进 state（同类实现的教训：
    送空数组等于多花 token 换零信息）。
    """
    if not history:
        return []
    on_screen = [m for m in msgs]
    out = [m for m in history if m not in on_screen]
    return out[-20:]


def _state(group: str, topic: str, viewer: dict | None, nickname: str,
           msgs: list[str], context: list[tuple[str, str]],
           history: list[str] | None = None) -> dict:
    viewer = viewer or {}
    earlier = _earlier(history, msgs)
    return {
        "群": {"名称": group, "主题": topic,
               # 群性质（工作/客户/亲友/兴趣/陌生…）：同一个"发链接 + 报价"，
               # 在客户群里是常态、在亲友群里才可疑 —— 缺了它模型没有坐标系。
               "性质": viewer.get("群性质") or "（未填写）"},
        # 当前时间必须给：模型不做日期运算，但"今天之内"这类判断需要知道现在是什么时候
        "现在": time.strftime("%Y-%m-%d %H:%M %A"),
        # 职责是必需的：`worth_my_reply` 的判据不在消息文本里
        "我": {"群昵称": viewer.get("群昵称") or "（未填写）",
               "职责": viewer.get("职责") or "（未填写）"},
        "这个人": {**{"昵称": nickname or "未知",
                     "最近消息": [{"序号": i + 1, "内容": t}
                                  for i, t in enumerate(msgs[:MAX_MSGS_PER_SPEAKER])]},
                   **({"更早的消息（本机累积，供参考）":
                       [{"序号": i + 1, "内容": t} for i, t in enumerate(earlier)]}
                      if earlier else {})},
        "最近全群消息": [{"发言人": s, "内容": t}
                        for s, t in context[-MAX_CONTEXT_LINES:]],
    }


def _state_msg(group: str, viewer: dict | None, speaker: str, msg: str,
               context: list[tuple[str, str]]) -> dict:
    """判**一条消息**时的 state。

    形状贴着私聊（因为复用私聊那 11 题），但把"要判的就是这一条"写在最显眼的位置：
    `这条消息` 是判断对象，`最近的整段对话` 只是上下文。
    群身份映射进私聊题面要用的 `我.身份` / `对话.关系` —— 题面一个字都不改。
    """
    viewer = viewer or {}
    nick = viewer.get("群昵称")
    duty = viewer.get("职责")
    identity = "（未填写）"
    if nick or duty:
        identity = "、".join(x for x in (f"群昵称：{nick}" if nick else "",
                                         f"职责：{duty}" if duty else "") if x)
    rel = viewer.get("关系") or ("同一个群里的成员" if group else "（未填写）")
    return {
        "对话": {"类型": "单条消息", "关系": rel, "群": group or "（未填写）",
                 "群性质": viewer.get("群性质") or "（未填写）"},
        "现在": time.strftime("%Y-%m-%d %H:%M %A"),
        "我": {"身份": identity},
        "这条消息（要判断的就是它）": {"发言人": speaker or "对方", "内容": msg},
        "对方": {"昵称": speaker or "对方",
                 "最近消息": [{"序号": 1, "内容": msg}]},
        "最近的整段对话": [{"发言人": s_, "内容": t_}
                          for s_, t_ in (context or [])[-MAX_CONTEXT_LINES:]],
    }


def analyse_msg(client: JevClient, *, group: str, viewer: dict | None,
                speaker: str, msg: str,
                context: list[tuple[str, str]] | None = None,
                profile: SpeakerProfile | None = None) -> dict:
    """判一条消息（复用私聊 11 题口径）。一次调用；同消息 + 同上下文命中缓存不重复计费。"""
    context = context or []
    profile = profile if profile is not None else SpeakerProfile()
    # 上下文指纹：末 3 条足够区分"同一句话在不同场景下"（够短，不额外花钱）
    fp = "|".join(f"{s_}:{t_[:40]}" for s_, t_ in context[-3:])
    cached = profile.get(speaker, [msg], viewer, scene="msg", group=group,
                         qver=QSET_VERSION_DM, cache_extra=fp)
    if cached is not None:
        return {**cached, "cached": True}
    state = _state_msg(group, viewer, speaker, msg, context)
    body = client.system_one(
        state, build_questions_dm(viewer),
        meta={"qset_version": QSET_VERSION_DM, "speaker": speaker, "scene": "msg"},
    )
    profile.calls += 1
    ans = body["answers"]
    return _dm_result(ans, body, speaker, [msg], profile, viewer,
                      group=group, history=None, ctx_n=len(context),
                      scene="msg", cache_extra=fp)


def analyse_person(client: JevClient, *, group: str, topic: str = "",
                   viewer: dict | None = None, nickname: str, msgs: list[str],
                   context: list[tuple[str, str]] | None = None,
                   profile: SpeakerProfile | None = None,
                   scene: str = "group",
                   history: list[str] | None = None) -> dict:
    """判断一个人。相同 (昵称, 消息集合) 命中本机缓存，不重复调用。"""
    context = context or []
    profile = profile if profile is not None else SpeakerProfile()

    ver = QSET_VERSION_DM if scene == "dm" else QSET_VERSION
    cached = profile.get(nickname, msgs, viewer, scene=scene, group=group, qver=ver,
                         history=history)
    if cached is not None:
        return {**cached, "cached": True}

    if scene == "dm":
        state = _state_dm(group, viewer, nickname, msgs, context, history)
        qs = build_questions_dm(viewer)
    else:
        state = _state(group, topic, viewer, nickname, msgs, context, history)
        qs = build_questions(viewer)
    body = client.system_one(
        state, qs,
        meta={"qset_version": ver, "speaker": nickname, "scene": scene},
    )
    profile.calls += 1
    ans = body["answers"]
    if scene == "dm":
        return _dm_result(ans, body, nickname, msgs, profile, viewer, group=group,
                          history=history, ctx_n=len(context or []))

    # ---- 代码侧派生 ----
    # 先校验再使用：残缺的概率不许进入结论
    worth_probs = _probs(ans, "worth_my_reply")
    _probs(ans, "role")
    _probs(ans, "intent_now")
    addressed = _prob(ans, "addressed_to_me", "noul")
    risks = {k: _prob(ans, k, "noul") for k in RISK_QUESTIONS}
    suff = (ans.get("sufficiency") or {}).get("score")
    if not isinstance(suff, (int, float)):
        raise InvalidAnswer(f"sufficiency.score 缺失: {suff!r}")

    attention, mass = derive_attention(worth_probs)
    state, reasons = gate(ans)

    r = {
        "nickname": nickname,
        "msgs": list(msgs),
        "role": ans["role"]["choice"],
        "role_conf": ans["role"]["confidence"],
        "role_probs": _probs(ans, "role"),
        "intent": ans["intent_now"]["choice"],
        "intent_conf": ans["intent_now"]["confidence"],
        "addressed_to_me": addressed,
        "qset_version": QSET_VERSION,
        "worth": ans["worth_my_reply"]["choice"],
        "worth_probs": worth_probs,
        "worth_conf": ans["worth_my_reply"]["confidence"],
        "risks": risks,
        "sufficiency": suff,
        "sufficiency_name": (
            ans["sufficiency"].get("legend", {}).get(str(round(suff)), "")
            or ans["sufficiency"].get("legend", {}).get("0", "")[:0]
        ),
        "attention": attention,
        "attention_mass": round(mass, 3),
        "state": state,
        "state_reasons": reasons,
        "model": body.get("model"),
        "usage": body.get("usage", {}),
        "cached": False,
        # 本机累积的更早消息（已与屏幕上这批去重）：面板要显示，用户才知道累积在起作用
        "earlier": _earlier(history, msgs),
        # 本机一共攒了多少条（含还在屏幕上的）—— 面板要显示"累积在起作用"，
        # 否则"都还在屏幕上"看起来就像没累积
        "hist_total": len(history or []),
        "ctx_n": len(context or []),   # 整段对话条数（面板要写清判断范围）
    }
    profile.analysed += 1
    if state == "alert":
        profile.alert_count += 1
    profile.remember(profile.key_of(nickname, msgs, viewer, scene="group",
                                    group=group, qver=QSET_VERSION,
                                    history=history), r)
    return r



def _state_dm(contact: str, viewer: dict | None, nickname: str, msgs: list[str],
              context: list[tuple[str, str]],
              history: list[str] | None = None) -> dict:
    """私聊的 state 形状：没有"群"，只有"对话"和"对方"。

    关系是**必需输入**：群聊里 `worth_my_reply` 的判据不在消息文本里（靠 `我.职责`），
    私聊里对应的是"我和这人什么关系"—— 不填就按普通联系人把握分寸，别猜成亲密关系。
    """
    viewer = viewer or {}
    earlier = _earlier(history, msgs)
    return {
        "对话": {"类型": "私聊", "关系": viewer.get("关系") or "（未填写）"},
        "现在": time.strftime("%Y-%m-%d %H:%M %A"),
        "我": {"身份": viewer.get("身份") or "（未填写）"},
        "对方": {**{"昵称": contact or "（未识别）",
                   "最近消息": [{"序号": i + 1, "内容": t}
                                for i, t in enumerate(msgs[:MAX_MSGS_PER_SPEAKER])]},
                 **({"更早的消息（本机累积，供参考）":
                     [{"序号": i + 1, "内容": t} for i, t in enumerate(earlier)]}
                    if earlier else {})},
        "最近的整段对话": [{"发言人": s, "内容": t}
                          for s, t in (context or [])[-MAX_CONTEXT_LINES:]],
    }


def _dm_result(ans: dict, body: dict, nickname: str, msgs: list[str],
               profile: "SpeakerProfile", viewer: dict | None = None,
               group: str = "",
               history: list[str] | None = None,
               ctx_n: int = 0, scene: str = "dm",
               cache_extra: str = "") -> dict:
    """私聊结果的构造。字段与群聊结果**保持同名**（hud 直接吃），另外多出
    true_intent / she_needs / danger / danger_word / literal / tension_resolved。

    viewer 必须传进来：缓存键里含身份指纹，落缓存时要用（漏传会 NameError）。
    """
    worth_probs = _probs(ans, "worth_my_reply")
    risks = {k: _prob(ans, k, "noul") for k in RISK_QUESTIONS}
    suff = (ans.get("sufficiency") or {}).get("score")
    if not isinstance(suff, (int, float)):
        raise InvalidAnswer(f"sufficiency.score 缺失: {suff!r}")
    ti = ans.get("true_intent") or {}
    sn = ans.get("she_needs") or {}
    # danger_level 与 sufficiency 同一口径（fail-closed）：缺失/非数值即整批不采用。
    # 静默按 0 分放行会把"危险等级"显示成 0/9 安全 —— 看着很确定的错误判断。
    dl = (ans.get("danger_level") or {}).get("score")
    if not isinstance(dl, (int, float)):
        raise InvalidAnswer(f"danger_level.score 缺失: {dl!r}")
    danger = float(dl)
    state, reasons = gate_dm(ans)
    r = {
        "nickname": nickname,
        "msgs": list(msgs),
        "scene": scene,
        "role": "", "role_conf": 0.0, "role_probs": {},
        "intent": ti.get("choice") or "", "intent_conf": ti.get("confidence") or 0.0,
        "true_intent": ti.get("choice") or "",
        "she_needs": sn.get("choice") or "",
        "danger": danger, "danger_word": danger_word(danger),
        "literal": _prob(ans, "literal_question", "noul"),
        "tension_resolved": _prob(ans, "tension_resolved", "noul"),
        "addressed_to_me": None,
        "qset_version": QSET_VERSION_DM,
        "worth": ans["worth_my_reply"]["choice"],
        "worth_probs": worth_probs,
        "worth_conf": ans["worth_my_reply"]["confidence"],
        "risks": risks,
        "sufficiency": suff,
        "sufficiency_name": (ans["sufficiency"].get("legend", {}) or {}).get(str(round(suff)), ""),
        # 私聊的关注度直接由危险等级派生（0–9 → 0–10），不再从 worth 的概率分布算：
        # 私聊要看的是"这事有多急"，不是"该不该回"的分档。
        "attention": int(round(danger)),
        "attention_mass": 1.0,
        "state": state,
        "state_reasons": reasons,
        "model": body.get("model"),
        "usage": body.get("usage", {}),
        "cached": False,
        "earlier": _earlier(history, msgs),
        "hist_total": len(history or []),
        "ctx_n": ctx_n,          # 整段对话条数（面板写清判断范围用）
    }
    profile.analysed += 1
    if state == "alert":
        profile.alert_count += 1
    profile.remember(profile.key_of(nickname, msgs, viewer, scene=scene,
                                    group=group, qver=QSET_VERSION_DM,
                                    history=history, cache_extra=cache_extra), r)
    return r

def escape_keys() -> set[str]:
    """哪些选项算逃逸。用于统计逃逸率（PRD §9.4 的健康区间 15–35%）。"""
    return {"insufficient_info"}


def summary_line(r: dict) -> str:
    """一行摘要，评测与界面共用。"""
    risks = [k for k, v in r["risks"].items() if v > 0.5]
    bits = [f"{r['nickname'] or '?'}: {label(r['role'])}/{label(r['intent'])}",
            f"该回={label(r['worth'])}", f"关注度={r['attention']}/10",
            f"信息={r['sufficiency']:.2f}", f"状态={r['state']}"]
    if risks:
        bits.append("风险=" + ",".join(risks))
    return "  ".join(bits)
