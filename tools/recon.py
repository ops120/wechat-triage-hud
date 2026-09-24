"""微信环境侦察：版本、控件树可读性、屏幕阅读器标志位。
输出 UTF-8 文件，避免终端 GBK 编码问题。
"""
import ctypes
import ctypes.wintypes as wt
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

OUT = os.path.join(OUT_DIR, "recon.json")
os.makedirs(os.path.dirname(OUT), exist_ok=True)

res = {}

# ---------- 1. 微信进程与版本 ----------
import psutil  # noqa: E402

procs = []
for p in psutil.process_iter(["pid", "name", "exe"]):
    try:
        if p.info["name"] and p.info["name"].lower() in ("weixin.exe", "wechat.exe"):
            procs.append({"pid": p.info["pid"], "name": p.info["name"],
                          "exe": p.info["exe"]})
    except Exception:
        pass
res["processes"] = procs

# 从 exe 文件读版本号
def file_version(path):
    if not path or not os.path.exists(path):
        return None
    size = ctypes.windll.version.GetFileVersionInfoSizeW(path, None)
    if not size:
        return None
    buf = ctypes.create_string_buffer(size)
    if not ctypes.windll.version.GetFileVersionInfoW(path, 0, size, buf):
        return None
    ptr = ctypes.c_void_p()
    ln = ctypes.c_uint()
    if not ctypes.windll.version.VerQueryValueW(
            buf, "\\", ctypes.byref(ptr), ctypes.byref(ln)):
        return None
    ffi = ctypes.cast(ptr, ctypes.POINTER(ctypes.c_uint * 13)).contents
    ms, ls = ffi[2], ffi[3]
    return f"{ms >> 16}.{ms & 0xFFFF}.{ls >> 16}.{ls & 0xFFFF}"

res["version"] = None
for pr in procs:
    v = file_version(pr.get("exe"))
    if v:
        res["version"] = v
        pr["version"] = v

# 从窗口标题/属性兜底（微信 4.x 主窗口 class = Qt51514QWindowIcon）
import win32gui  # noqa: E402

SPI_GETSCREENREADER = 0x0046
flag = ctypes.c_int()
ok = ctypes.windll.user32.SystemParametersInfoW(
    SPI_GETSCREENREADER, 0, ctypes.byref(flag), 0)
res["spi_screenreader_set"] = bool(flag.value) if ok else None

# ---------- 2. 顶层窗口 ----------
wins = []


def cb(hwnd, _):
    try:
        if not win32gui.IsWindowVisible(hwnd):
            return True
        cls = win32gui.GetClassName(hwnd)
        title = win32gui.GetWindowText(hwnd)
        pid = ctypes.c_ulong()
        ctypes.windll.user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        if pid.value in {p["pid"] for p in procs}:
            r = win32gui.GetWindowRect(hwnd)
            wins.append({"hwnd": hwnd, "class": cls, "title": title,
                         "rect": list(r),
                         "size": [r[2] - r[0], r[3] - r[1]]})
    except Exception:
        pass
    return True


win32gui.EnumWindows(cb, None)
res["wechat_windows"] = wins

# ---------- 3. UIA 控件树可读性 ----------
try:
    import comtypes.client as cc
    cc.GetModule("UIAutomationCore.dll")
    import comtypes.gen.UIAutomationClient as uiac

    uia = cc.CreateObject("{ff48dba4-60ef-4201-aa87-54103eef594e}",
                          interface=uiac.IUIAutomation)
    root = uia.GetRootElement()
    res["uia_available"] = True

    # 对所有微信窗口，统计后代数量与 mmui:: 类名
    walker = uia.RawViewWalker
    tree = []
    for w in wins:
        try:
            el = uia.ElementFromHandle(w["hwnd"])
        except Exception as e:
            tree.append({"hwnd": w["hwnd"], "error": str(e)})
            continue
        classes = {}
        stack = [(el, 0)]
        n = 0
        while stack and n < 4000:
            e, d = stack.pop()
            n += 1
            try:
                cn = e.CurrentClassName or ""
                classes[cn] = classes.get(cn, 0) + 1
                child = walker.GetFirstChildElement(e)
                while child:
                    stack.append((child, d + 1))
                    child = walker.GetNextSiblingElement(child)
            except Exception:
                pass
        mmui = {k: v for k, v in classes.items() if k.startswith("mmui::")}
        tree.append({
            "hwnd": w["hwnd"], "class": w["class"], "title": w["title"],
            "descendants": n,
            "mmui_classes": dict(sorted(mmui.items(), key=lambda kv: -kv[1])),
            "other_classes": dict(sorted(
                ((k, v) for k, v in classes.items() if not k.startswith("mmui::")),
                key=lambda kv: -kv[1])[:12]),
        })
    res["uia_tree"] = tree
except ImportError as e:
    res["uia_available"] = False
    res["uia_error"] = f"comtypes 未安装: {e}"
except Exception as e:
    res["uia_available"] = False
    res["uia_error"] = f"{type(e).__name__}: {e}"

with open(OUT, "w", encoding="utf-8") as f:
    json.dump(res, f, ensure_ascii=False, indent=2)
print("written:", OUT)
