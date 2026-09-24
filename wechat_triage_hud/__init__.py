"""wechat-triage-hud —— PC 微信群聊按人分诊工具。

对当前打开的群会话做 OCR 采集，按**发言人**聚合后交给 TypeSafe Jev 判断：
这个人在群里是什么角色、想干什么、是否在点我、该不该回、有没有风险。
默认静默，只有"明确需要我"才提示；每一次 API 调用都完整落进审计日志。

入口：
    python -m wechat_triage_hud.hud
"""

__version__ = "0.1.0"
