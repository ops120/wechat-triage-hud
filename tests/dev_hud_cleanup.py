# -*- coding: utf-8 -*-
"""清掉所有面板实例（之前只 kill 了 head -1，留下了好几个），只留一个。
用法：python tests/dev_hud_cleanup.py
"""
import os
import subprocess
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import psutil  # noqa: E402

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def procs():
    out = []
    for p in psutil.process_iter(["pid", "cmdline"]):
        try:
            cl = " ".join(p.info["cmdline"] or [])
        except Exception:
            continue
        if "wechat_triage_hud.hud" in cl:
            out.append(p.info["pid"])
    return sorted(out)


def main() -> int:
    before = procs()
    print(f"清理前 HUD 进程：{before}")
    for p in before:
        r = subprocess.run(["taskkill", "/F", "/PID", str(p)],
                           capture_output=True, encoding="gbk", errors="replace")
        print(f"  kill {p}: {r.returncode} {(r.stdout or '').strip()[:40]}")
    time.sleep(1.5)
    left = procs()
    print(f"清理后：{left if left else '0 个'}")
    return 1 if left else 0


if __name__ == "__main__":
    raise SystemExit(main())
