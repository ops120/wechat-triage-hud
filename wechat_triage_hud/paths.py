"""统一路径 —— 全项目唯一一份，禁止各自用 __file__ 推算。

为什么要单独一个文件：脚本/文档各搬各的目录之后，
如果每个脚本还各自 dirname(__file__) 推算，就会出现
「产品写 out/jev_audit.jsonl、测试脚本写 .tmp/out/jev_audit.jsonl」
这种把审计日志拆成两份的情况 —— 而日志是"每个数字都能追溯"的唯一凭据，
一旦分裂，追溯就断了。

目录约定（GitHub 标准布局）：
    wechat-triage-hud/
      README.md  .gitignore  .env.example  requirements.txt
      （自用文档不进仓库：PRD / 备忘录 / 调研含真实群名与人名，只在本机维护）
      tests/          验证脚本（可重复跑的验收）
      screenshots/    样例图（虚构数据渲染，可入库）
      tools/           诊断工具（OCR 探针、控件树 dump、屏幕阅读器标志）
      wechat_triage_hud/   产品代码（本包）
      out/             运行产物：审计日志、群元数据、调试日志（gitignore）
      .env             API Key（gitignore）
"""
from __future__ import annotations

import os

PKG_DIR = os.path.dirname(os.path.abspath(__file__))       # wechat_triage_hud/
PROJECT_DIR = os.path.dirname(PKG_DIR)                     # 仓库根

TESTS_DIR = os.path.join(PROJECT_DIR, "tests")
TOOLS_DIR = os.path.join(PROJECT_DIR, "tools")
OUT_DIR = os.path.join(PROJECT_DIR, "out")                 # 运行产物，不入库

ENV_PATH = os.path.join(PROJECT_DIR, ".env")
AUDIT_PATH = os.path.join(OUT_DIR, "jev_audit.jsonl")      # 审计日志（唯一）
META_PATH = os.path.join(OUT_DIR, "group_meta.json")       # 群元数据（群昵称等）
DEBUG_PATH = os.path.join(OUT_DIR, "hud_debug.log")        # 调试日志（JEV_HUD_DEBUG=1）
UI_PATH = os.path.join(OUT_DIR, "ui_prefs.json")           # 面板外观：小球/位置/形态
SETTINGS_PATH = os.path.join(OUT_DIR, "settings.json")      # F-22 设置（频率/成本/阈值）
HIST_PATH = os.path.join(OUT_DIR, "speaker_history.json")   # 按人累积的历史消息（F-25）
ASSETS_DIR = os.path.join(PKG_DIR, "assets")              # 产品自带资源（图标等）
BALL_IMG = os.path.join(ASSETS_DIR, "ball.png")            # 收起态小球用图（用户提供的图）


def ensure_out() -> None:
    os.makedirs(OUT_DIR, exist_ok=True)
