# -*- coding: utf-8 -*-
r"""RM3 步骤串联器：用 Python 直接落盘 UTF-8 证据，避免 PowerShell 管道的编码损坏。

为什么不用 PowerShell 串联：
  PS 5.1 用控制台代码页（本机 GBK）解码子进程的 UTF-8 输出，实测把
  「清理前 HUD 进程：[]」写成 UTF-16 的乱码「娓呯悊鍓?HUD 杩涚▼锛歔]」——
  证据文件一旦是乱码，复核就失去了意义。所以每次子进程输出都按 UTF-8 解码，
  由 Python 自己写文件。

用法：
  python .tests\dev_rm3_chain.py A FLOW ACD P1 CLEAN
（每组 = 一组真机步骤；控制台只打印 ASCII 状态行，中文全部进文件。）
"""
from __future__ import annotations

import os
import subprocess
import sys
import time

PROJ = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
EVID = os.path.join(PROJ, "out", "verify_20260923")
PY = sys.executable

GROUPS = {
    # 步骤1 清进程 → 环境归一 → 真机启动面板+15s 枚举 ；步骤2 原样跑 dev_p0_verify.py
    "A": [
        (["python", r".tests\dev_hud_cleanup.py"], "rm3_step1_cleanup"),
        (["python", r".tests\dev_rm3_normalize.py"], "rm3_step1_normalize"),
        (["python", r".tests\dev_rm3_start_hud.py"], "rm3_step2_start"),
        (["python", r".tests\dev_p0_verify.py"], "rm3_p0_run1"),
    ],
    # 步骤3 前置：环境归一 + 真机重启面板（步骤2 的 [D] 已把面板真关掉了）
    "START": [
        (["python", r".tests\dev_rm3_normalize.py"], "rm3_step3_normalize"),
        (["python", r".tests\dev_rm3_start_hud.py"], "rm3_step3_start"),
    ],
    # 步骤3a–3d：真机专项
    "FLOW": [
        (["python", r".tests\dev_rm3_flow.py"], "rm3_flow_run1"),
    ],
    # 补充：[C] 滚轮独立复算
    "SCROLL": [
        (["python", r".tests\dev_rm3_scroll_check.py"], "rm3_scroll_run1"),
    ],
    # 补充：A/B/C/D 独立复算（D 会真关掉面板）
    "ACD": [
        (["python", r".tests\dev_rm3_p0_acd.py"], "rm3_p0_acd"),
    ],
    # 步骤4：真实 API 回归
    "P1": [
        (["python", r".tests\dev_p1_verify.py"], "rm3_p1"),
    ],
    # 步骤5：收尾清进程
    "CLEAN": [
        (["python", r".tests\dev_hud_cleanup.py"], "rm3_step9_cleanup"),
    ],
}


def main() -> int:
    which = sys.argv[1:] or ["A"]
    os.makedirs(EVID, exist_ok=True)
    logp = os.path.join(EVID, "rm3_chain.log")
    log = open(logp, "a", encoding="utf-8")
    status = 0
    for g in which:
        for cmd, name in GROUPS.get(g, []):
            t0 = time.time()
            print(f"[chain] {name} ...", flush=True)
            logp_tmp = os.path.join(EVID, name + ".log")
            with open(logp_tmp, "w", encoding="utf-8") as out, \
                    open(os.path.join(EVID, name + ".err"), "w", encoding="utf-8") as err:
                p = subprocess.Popen(cmd, cwd=PROJ, stdout=subprocess.PIPE,
                                     stderr=err, text=True, encoding="utf-8",
                                     errors="replace", bufsize=1)
                # 实时落盘：长步骤（会话切换要等 80s）中途也能看进度
                for line in p.stdout:
                    out.write(line)
                    out.flush()
                rc = p.wait()
            dt = time.time() - t0
            r = type("R", (), {"returncode": rc, "stdout": "", "stderr": ""})()
            log.write(f"\n===== {name}  cmd={' '.join(cmd)}  exit={rc}  "
                      f"{dt:.1f}s  {time.strftime('%H:%M:%S')} =====\n")
            log.flush()
            print(f"[chain] {name} exit={rc} {dt:.1f}s", flush=True)
            if rc != 0:
                status = 1
    log.close()
    print(f"[chain] ALL DONE status={status}")
    return status


if __name__ == "__main__":
    raise SystemExit(main())
