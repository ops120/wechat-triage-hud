"""判定：面板进程会不会自己退出。

背景：复核发现面板启动日志正常，但 12 秒后进程数为 0 —— 而当时微信正被别的窗口遮挡，
面板按设计会隐藏自己。若进程因此退出，那是真 bug（应该继续活着并显示"被遮挡"）。

这个测试同时抓：
  · 进程存活时长
  · closeEvent 是否被触发（调试日志）
  · 窗口是"隐藏"还是"已销毁"
"""
import os
import subprocess
import sys
import threading
import time

PROJECT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PROJECT_DIR)

import psutil  # noqa: E402
import win32gui  # noqa: E402
from wechat_triage_hud.paths import DEBUG_PATH  # noqa: E402

WATCH_S = 25
me = os.getpid()


def hud_procs():
    out = []
    for p in psutil.process_iter(["pid", "cmdline"]):
        try:
            if p.info["pid"] == me:
                continue
            argv = p.info["cmdline"] or []
            if "-m" in argv and "wechat_triage_hud.hud" in argv:
                out.append(p.info["pid"])
        except Exception:
            pass
    return out


def hud_windows(include_hidden=True):
    found = []

    def cb(h, _):
        try:
            cls = win32gui.GetClassName(h)
            if "QWindowToolSaveBits" in cls:
                found.append((h, bool(win32gui.IsWindowVisible(h)),
                              win32gui.GetWindowRect(h)))
        except Exception:
            pass
        return True

    win32gui.EnumWindows(cb, None)
    return found


def main():
    if os.path.exists(DEBUG_PATH):
        os.remove(DEBUG_PATH)

    # 先清掉已在运行的实例：单实例保护会让新实例立刻退出（那是正确行为，
    # 但会让本测试误判为"自己退出了"）。测试必须自己掌控这个外部状态。
    existing = hud_procs()
    if existing:
        print(f"发现 {len(existing)} 个已在运行的实例，先关掉（否则单实例保护会拦住本次启动）")
        for pid in existing:
            try:
                psutil.Process(pid).kill()
            except Exception:
                pass
        time.sleep(2)

    env = dict(os.environ, JEV_HUD_DEBUG="1")
    p = subprocess.Popen([sys.executable, "-u", "-m", "wechat_triage_hud.hud"],
                         cwd=PROJECT_DIR, env=env,
                         stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    print(f"已启动，PID={p.pid}，观察 {WATCH_S}s\n")

    died_at = None
    for i in range(WATCH_S):
        time.sleep(1)
        alive = p.poll() is None
        wins = hud_windows()
        vis = sum(1 for _, v, _ in wins if v)
        if i % 5 == 0 or not alive:
            print(f"  t={i+1:>2}s  进程{'存活' if alive else '已退出'}  "
                  f"窗口 {len(wins)} 个（可见 {vis}）")
        if not alive:
            died_at = i + 1
            break

    # 注意顺序：先终止再读。进程仍存活时 read() 会一直等 stdout 关闭 —— 会死锁。
    if p.poll() is None:
        p.terminate()
        try:
            p.wait(timeout=8)
        except Exception:
            p.kill()
    out = ""
    if p.stdout:
        buf = {}

        def _reader():
            try:
                buf["s"] = p.stdout.read() or ""
            except Exception:
                buf["s"] = ""

        th = threading.Thread(target=_reader, daemon=True)
        th.start()
        th.join(timeout=5)          # 兜底：即便读不动也不卡住测试
        out = buf.get("s", "（读取超时，已跳过）")
    print(f"\n--- 进程输出 ---\n{out.strip() or '（无）'}")

    if os.path.exists(DEBUG_PATH):
        logs = open(DEBUG_PATH, encoding="utf-8").read().strip().splitlines()
        print(f"\n--- 调试日志（{len(logs)} 行，尾 6 行）---")
        for ln in logs[-6:]:
            print("  " + ln)
        if any("closeEvent" in l for l in logs):
            print("\n  ⚠ 出现 closeEvent —— 说明是被关闭流程结束的，不是崩溃")
    else:
        print("\n--- 调试日志：无（dbg 未被调用）---")

    print("\n" + "=" * 60)
    if died_at:
        print(f"❌ 面板在 {died_at}s 时自己退出了（退出码 {p.returncode}）—— 这是缺陷：")
        print("   微信被遮挡时面板应当隐藏但**继续存活**，否则用户看不到'被遮挡'提示，")
        print("   也无法在遮挡解除后自动恢复。")
    else:
        print(f"✅ 面板在 {WATCH_S}s 内持续存活（符合预期：遮挡时隐藏但不停机）")
    p.terminate()
    try:
        p.wait(timeout=10)
    except Exception:
        p.kill()
    return 1 if died_at else 0


if __name__ == "__main__":
    raise SystemExit(main())
