# -*- coding: utf-8 -*-
r"""RM3：带 faulthandler 的面板启动器 —— 用来拿"扫描停摆"时的线程栈。

为什么需要它：真机实测到面板在「日志」清空后不再扫描（GUI 还活着、微信内容
确实变了、stderr 0 字节、debug 日志 9 分钟零输出）。要判"卡在哪"，就得看
线程栈 —— 但面板是后台起的，没法 attach。
`faulthandler.dump_traceback_later(period, repeat=True, file=...)` 会在**同一进程内**
周期性地把所有线程的 Python 栈打到文件里，正好够用。

本脚本**不修改产品代码**：只是先装好 faulthandler，再用 runpy 以 __main__ 身份
跑 wechat_triage_hud.hud（等价于 `python -m wechat_triage_hud.hud`）。

用法（后台）：
  $env:JEV_HUD_DEBUG='1'; python .tests\dev_rm3_hud_fh.py
栈 dump → out/verify_20260923/rm3_fh_dump.log
"""
from __future__ import annotations

import faulthandler
import os
import runpy
import sys
import time

PROJ = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
EVID = os.path.join(PROJ, "out", "verify_20260923")
os.makedirs(EVID, exist_ok=True)
sys.path.insert(0, PROJ)
os.chdir(PROJ)
os.environ.setdefault("JEV_HUD_DEBUG", "1")

DUMP = os.path.join(EVID, "rm3_fh_dump.log")
PERIOD = float(os.environ.get("RM3_FH_PERIOD", "30"))

_f = open(DUMP, "a", encoding="utf-8", buffering=1)
_f.write(f"\n\n########## faulthandler 启动 {time.strftime('%H:%M:%S')} "
         f"pid={os.getpid()} period={PERIOD}s ##########\n")
_f.flush()
faulthandler.dump_traceback_later(PERIOD, repeat=True, file=_f, exit=False)
faulthandler.enable(file=_f, all_threads=True)

print(f"[fh] pid={os.getpid()} 每 {PERIOD}s dump 线程栈 → {DUMP}", flush=True)
runpy.run_module("wechat_triage_hud.hud", run_name="__main__", alter_sys=True)
