# -*- coding: utf-8 -*-
"""F-22 设置：一个本机设置文件 + 读写校验（频率上限/成本上限/阈值/API Key）。

设计要点
- **默认值写在这里**，业务代码不再散落魔法数字；`SETTINGS` 是进程内单例，
  面板保存后立即生效（不需要重启）。
- 校验在**写入前**做：越界一律拒绝并说明原因，绝不静默钳到边界（那会让用户以为改成功了）。
- 存 `out/settings.json`（out/ 已 gitignore）；Key 本身仍只存在 `.env`，这里只存"非密钥"配置。
"""
from __future__ import annotations

import json
import os

from .paths import SETTINGS_PATH

# 范围与默认值：UI 与校验共用同一份定义，避免两处不一致
RANGES = {
    "per_hour": (1, 1000, 60),           # 频率上限：每群每小时最多几次调用
    "min_interval_s": (1, 600, 20),      # 最小重判间隔（秒）
    "daily_usd": (0.0, 100.0, 0.50),     # 每日成本上限（美元）
    "history_msgs": (0, 50, 12),        # 每人本地累积的历史消息条数（0 = 不累积）
    "thr_noul_hit": (0.05, 0.95, 0.35),  # Noul 判定门槛（风险/是否点我/潜台词）
    "thr_choice_clear": (0.05, 0.99, 0.60),  # Choice 顶项达到多少才算"结论清晰"
    "thr_strong": (0.05, 0.99, 0.75),    # 最高打扰级别的高门槛
}
LABELS = {
    "per_hour": "每群每小时调用上限（次）",
    "min_interval_s": "同一会话最小重判间隔（秒）",
    "daily_usd": "每日成本上限（美元）",
    "history_msgs": "每人本地累积历史（条，0=不累积）",
    "thr_noul_hit": "Noul 门槛（风险 / 是否点我 / 潜台词）",
    "thr_choice_clear": "Choice 结论清晰门槛",
    "thr_strong": "最高打扰级别门槛",
}

# 每个旋钮的「这是什么 + 推荐多少 + 为什么」——**唯一来源**：
# 设置对话框用 `what`（一行提示）与 `why`（悬停详解），README/文档同样从这里抄，
# 免得"界面写一套、文档写一套"。`RANGES` 里的第 3 个值就是**推荐值**（= 默认值）：
# 它们按这份模型的实测校准过（见 why 里的数字），不确定就别改。
HELP = {
    "per_hour": {
        "what": "本群本小时最多判几次，超了就这一小时只看不判",
        "why": "一次按人判断实测约 1-2 秒；推荐 60 相当于每分钟最多一次，正常聊天用不到。"
               "调小更省，调大更及时。",
    },
    "min_interval_s": {
        "what": "同一会话两次重判之间的最短间隔；切到别的会话不受它限制（各会话各算）",
        "why": "防的是 OCR 抖动 / 连续刷屏导致的重复计费 —— 实测没有它时 20 秒内提交过 6 次。"
               "调小 = 换消息更快出结论（更容易重复花钱），调大 = 更省。",
    },
    "daily_usd": {
        "what": "当天累计花费到它就停（只看不判），防止意外刷爆",
        "why": "实测一次按人判断约 $0.0002（3571 输入 token）；推荐 $0.50 约合 2500 次。",
    },
    "history_msgs": {
        "what": "每人记住多少条已滚出屏幕的消息，判断时一起送（0 = 不累积）",
        "why": "实测带上 5 条滚出屏幕的消息：结论只微调（置信 0.58→0.59），成本 +312 token（约 +8%）。"
               "调大 = 可能更有上下文，也更容易多花钱。",
    },
    "thr_noul_hit": {
        "what": "「风险 / 是否在点我 / 潜台词」这类概率超过它才算命中",
        "why": "模型对这类 Noul 题实测欠自信（T≈0.66：它给 0.40 也可能真是风险），"
               "所以推荐值偏低 —— 宁可多报一次，也别漏掉一个诈骗号。调高 = 更少打扰，但可能漏报。",
    },
    "thr_choice_clear": {
        "what": "顶项概率达到它才敢给明确结论；达不到就显示「判不了」",
        "why": "模型对 Choice 类题实测过度自信（T≈3.29：错的时候也给满概率），"
               "所以不给「91%」这种假精确。调高 = 更保守（更多「判不了」）。",
    },
    "thr_strong": {
        "what": "要打扰到你（⚠ 高亮）所需的把握",
        "why": "最高打扰级别的门槛。调高 = 更少 ⚠，调低 = 更容易被打扰。",
    },
}


def recommended(key: str):
    """推荐值（= RANGES 里的默认值，按模型实测校准过）。"""
    return RANGES[key][2]


def help_text(key: str) -> str:
    """一行提示：推荐 X · 这是什么。"""
    return f"推荐 {recommended(key)} · {HELP.get(key, {}).get('what', '')}"


def help_tooltip(key: str) -> str:
    """悬停详解：这是什么 + 为什么推荐它（含实测数字）+ 取值范围。"""
    lo, hi, d = RANGES[key]
    h = HELP.get(key, {})
    return (f"{LABELS.get(key, key)}\n\n{h.get('what', '')}\n\n{h.get('why', '')}\n\n"
            f"推荐（默认）：{d}　可填范围：{lo} ~ {hi}")


class Settings:
    def __init__(self, path: str = SETTINGS_PATH):
        self.path = path
        self.data: dict = {}
        self.load()

    # ---------- 读写 ----------
    def load(self) -> dict:
        raw = {}
        try:
            with open(self.path, "r", encoding="utf-8") as f:
                raw = json.load(f)
            if not isinstance(raw, dict):
                raw = {}
        except Exception:
            raw = {}
        self.data = {}
        for k, (_lo, _hi, default) in RANGES.items():
            v = raw.get(k, default)
            try:
                v = float(v) if isinstance(default, float) else int(v)
            except (TypeError, ValueError):
                v = default
            if not (RANGES[k][0] <= v <= RANGES[k][1]):
                v = default                      # 存档被改坏 → 回默认，不带到运行里
            self.data[k] = v
        return self.data

    def get(self, key: str, default=None):
        if key in RANGES:
            return self.data.get(key, RANGES[key][2])
        return self.data.get(key, default)

    def validate(self, key: str, value) -> tuple[bool, str]:
        if key not in RANGES:
            return False, f"未知设置项 {key!r}"
        lo, hi, _ = RANGES[key]
        try:
            v = float(value)
        except (TypeError, ValueError):
            return False, f"{LABELS.get(key, key)}：不是数字（{value!r}）"
        if not (lo <= v <= hi):
            return False, f"{LABELS.get(key, key)}：应在 {lo}~{hi} 之间，收到 {v}"
        return True, ""

    def set(self, key: str, value) -> tuple[bool, str]:
        ok, why = self.validate(key, value)
        if not ok:
            return False, why
        lo, _hi, default = RANGES[key]
        self.data[key] = float(value) if isinstance(default, float) else int(float(value))
        return True, ""

    def save(self) -> tuple[bool, str]:
        try:
            os.makedirs(os.path.dirname(self.path), exist_ok=True)
            with open(self.path, "w", encoding="utf-8") as f:
                json.dump(self.data, f, ensure_ascii=False, indent=2)
            return True, ""
        except Exception as e:                    # noqa: BLE001
            return False, f"保存失败：{e}"

    def reset(self) -> None:
        self.data = {k: RANGES[k][2] for k in RANGES}


SETTINGS = Settings()          # 进程内单例：面板改完立即生效，不用重启


def sync_thresholds() -> None:
    """把设置里的阈值同步到 qset（它按模块常量读，改常量即可全局生效）。

    这样 `gate()` / `gate_dm()` 一行都不用动 —— 阈值仍是"代码决定"，
    只是来源从写死变成了本机设置（PRD §4.3 的前提没变）。
    """
    try:
        from . import qset
        qset.NOUL_HIT = float(SETTINGS.get("thr_noul_hit"))
        qset.CHOICE_CLEAR = float(SETTINGS.get("thr_choice_clear"))
        qset.STRONG = float(SETTINGS.get("thr_strong"))
    except Exception as e:                        # noqa: BLE001
        import sys
        print(f"[settings] 同步阈值失败: {e}", file=sys.stderr)
