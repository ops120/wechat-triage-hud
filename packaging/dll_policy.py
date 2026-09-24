# -*- coding: utf-8 -*-
"""打包时要"额外借"哪些 DLL —— 抽成纯函数，好让自检能验它。

背景（三类环境，两种都真踩过）：
  · **conda 本体**（python 直接在 conda 里）：conda 把 Qt / shiboken / libffi 的 DLL 放在
    `<prefix>\\Library\\bin`，PyInstaller 不会自动收 → 要借 **stdlib 依赖 + Qt/shiboken**。
  · **用 conda 的 python 建的 venv**（`python -m venv`）：PySide6 来自 pip 轮子、Qt DLL 自带在
    包内，**不能再借 conda 的 Qt**（两套混装 → `DLL load failed while importing QtGui:
    找不到指定的程序`）；但 `_ctypes` 仍依赖 **base_prefix** 里那个 ffi-8.dll → 只借 stdlib 那批。
  · **没有 conda 的用户**（python.org + pip venv）：`Library\\bin` 根本不存在 →
    **一个都不借**，PyInstaller 原生就能处理 CPython 与轮子里的 DLL ✓
    （这是 PyInstaller 的常规路径；本项目 spec 里的 conda 逻辑对他们完全是空操作 ——
     自检里有一条断言专门钉住这件事，免得以后改坏了）
"""
from __future__ import annotations

import os

# stdlib 扩展要的（_ctypes → ffi-8.dll；_lzma/_bz2/pyexpat/_decimal 各自对应）
STDLIB_DLLS = ("ffi*.dll", "liblzma.dll", "libbz2.dll", "libexpat.dll",
               "zlib.dll", "libmpdec*.dll")
# conda 的 PySide6 把 Qt/shiboken 也放在 Library\bin（pip 轮子里是在包内）
QT_DLLS = ("shiboken6*.dll", "pyside6*.dll", "Qt6Core.dll", "Qt6Gui.dll",
           "Qt6Widgets.dll", "icudt*.dll", "icuin*.dll", "icuuc*.dll")


def lib_dirs(prefix: str | None, base_prefix: str | None, isdir=os.path.isdir) -> list:
    """要搜索的 `<...>\\Library\\bin` 目录（去重、保持顺序）。非 conda 时返回空列表。"""
    out = []
    for p in (prefix, base_prefix):
        cand = os.path.join(p or "", "Library", "bin")
        if cand and cand not in out and isdir(cand):
            out.append(cand)
    return out


def dll_patterns(prefix: str | None, base_prefix: str | None) -> list:
    """该借哪些 DLL 文件名模式。

    判断依据是"当前是不是 conda 本体"：`prefix == base_prefix` 说明没套 venv。
    套了 venv（或压根不是 conda）就**不借 Qt** —— 那份 Qt 由环境里的 PySide6 自己负责。
    """
    in_conda = os.path.normcase(prefix or "") == os.path.normcase(base_prefix or "")
    return list(STDLIB_DLLS) + (list(QT_DLLS) if in_conda else [])


def all_patterns(prefix: str | None, base_prefix: str | None,
                 isdir=os.path.isdir) -> list:
    """把"目录 × 模式"展开成实际要收的文件清单（给 spec 用）。"""
    import glob
    out = []
    pats = dll_patterns(prefix, base_prefix)
    for d in lib_dirs(prefix, base_prefix, isdir):
        for pat in pats:
            for f in glob.glob(os.path.join(d, pat)):
                if f.endswith(".dll"):
                    out.append(f)
    return out


# Qt 的插件（平台插件 qwindows.dll 是**必需**的：缺了启动就报
# "Could not find the Qt platform plugin windows"）。PySide6 6.11 把插件放在
# <site-packages>/PySide6/plugins/ 下，而 PyInstaller 的钩子没收集它们
# （venv 版实测：包里连 plugins 目录都没有）→ 这里显式收，装到包内 `PySide6/plugins/<类>`，
# 与 PyInstaller 的 PySide6 运行时钩子期望的位置一致。
PYSIDE_PLUGIN_KINDS = ("platforms", "styles", "imageformats", "iconengines", "platformthemes")


def pyside_plugin_files(pyside_dir: str | None, kind: str) -> list:
    """某个插件类目下的文件（给 spec 用；目录不存在就返回空）。"""
    if not pyside_dir:
        return []
    import glob
    d = os.path.join(pyside_dir, "plugins", kind)
    return sorted(f for f in glob.glob(os.path.join(d, "*.dll")) if f.endswith(".dll"))
