# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller 规格文件（onedir + 无控制台）。

用法：`pyinstaller --noconfirm packaging/wechat-triage-hud.spec`（或直接跑 build.bat）

为什么这么配（都是踩过/查过的）：
- **onedir 而不是 onefile**：onefile 每次启动要把几百 MB 解包到临时目录，面板启动要等十几秒，
  而且解包目录会被当成"程序目录"（`paths.resolve_project_dir` 已处理，但没必要受这个罪）。
- **windowed（无控制台）**：这是常驻 HUD，弹个黑框很难看；无控制台时 `sys.stdout` 是 None，
  所以入口用 `packaging/run_hud.py`（它先把 stdout/stderr 换成桩）。
- **rapidocr 必须 collect_all**（不能只 collect_data_files）：它的识别器是运行时按名字
  import 子模块的，只收数据文件会让打包版一开 OCR 就报
  `module 'ch_ppocr_v3_det' has no attribute 'TextDetector'`。
- `win32gui/win32con/win32api` 是 pywin32 的子模块，PyInstaller 有时抓不全，显式列上。
- **conda 与 venv 都支持**，见下面 DLL 那段的分工。
"""
import os

block_cipher = None

# spec 文件里没有 __file__（PyInstaller 用 exec 跑它）—— 用官方注入的 SPECPATH
ROOT = os.path.dirname(os.path.abspath(SPECPATH))
ICON = os.path.join(ROOT, "wechat_triage_hud", "assets", "ball.ico")
if not os.path.exists(ICON):
    ICON = None            # 没生成图标也能打包（build.bat 会尝试用 Pillow 从 ball.png 生成）

datas = []
binaries = []

# 要借哪些"环境里的 DLL"：策略抽在 packaging/dll_policy.py（纯函数，自检会验），
# 这里只负责把结果交给 PyInstaller。一句话：conda 本体借 stdlib+Qt，venv 只借 stdlib，
# 非 conda（python.org）一个都不借 —— PyInstaller 常规路径，对他们这段是空操作。
import glob                                     # noqa: F401 （保留：老 spec 依赖过 glob）
import sys as _sys
# SPECPATH 本身就是 spec 所在目录（= packaging/）；别再 dirname 一次，否则加的是仓库根。
_sys.path.insert(0, os.path.abspath(SPECPATH))
from dll_policy import all_patterns as _all_patterns
for _f in _all_patterns(_sys.prefix, getattr(_sys, "base_prefix", None)):
    binaries.append((_f, "."))

# Qt 插件（平台插件是必需的）：PySide6 6.11 起插件在 <site-packages>/PySide6/plugins/，
# 而 PyInstaller 的钩子没收集（venv 版实测缺 plugins → 启动报
# "Could not find the Qt platform plugin windows"）。显式收一遍，装到 PySide6/plugins/<类>。
try:
    import PySide6
    _PYSIDE_DIR = os.path.dirname(os.path.abspath(PySide6.__file__))
except Exception:                                # noqa: BLE001
    _PYSIDE_DIR = None
from dll_policy import PYSIDE_PLUGIN_KINDS as _KINDS, pyside_plugin_files as _plugin_files
for _kind in _KINDS:
    for _f in _plugin_files(_PYSIDE_DIR, _kind):
        datas.append((_f, os.path.join("PySide6", "plugins", _kind)))

hidden = ["win32gui", "win32con", "win32api"]
try:
    from PyInstaller.utils.hooks import collect_all, collect_submodules
    # **必须 collect_all，不能只 collect_data_files**：rapidocr 的识别器是运行时按名字
    # 去 import 子模块的（`ch_ppocr_v3_det.text_detect.TextDetector`），只收数据文件的话
    # 打包版一开 OCR 就报 `module 'ch_ppocr_v3_det' has no attribute 'TextDetector'`
    # （用户真机撞到过）。collect_all = 数据 + 二进制 + **子模块** 一起收。
    _d, _b, _h = collect_all("rapidocr_onnxruntime")
    datas += _d
    binaries += _b
    hidden += _h
    _d2, _b2, _h2 = collect_all("onnxruntime")
    datas += _d2
    binaries += _b2
    hidden += _h2
    hidden += collect_submodules("win32com")
    hidden += ["win32ui", "pywintypes", "pythoncom"]
except Exception as _e:                      # noqa: BLE001
    print(f"[spec] collect_all 失败（{_e}）—— 打包出来的 OCR 可能不可用")

a = Analysis(
    [os.path.join(ROOT, "packaging", "run_hud.py")],
    pathex=[ROOT],
    binaries=binaries,
    datas=datas + [(os.path.join(ROOT, "wechat_triage_hud", "assets", "ball.png"),
                    os.path.join("wechat_triage_hud", "assets"))],
    hiddenimports=hidden,
    hookspath=[],
    runtime_hooks=[],
    # 实测产物构成：llvmlite 115MB + scipy 47MB + scipy.libs 20MB 全是被间接拖进来的
    # （numba 链路），而**导入 OCR 全链后 sys.modules 里根本没有它们**（实测过）——
    # 排掉它们体积直接从 554MB 掉到三百多兆。cv2 不能排：版面检测用了联通域分析。
    excludes=["tkinter", "matplotlib", "pandas", "pytest", "setuptools", "pip",
              "numba", "llvmlite", "scipy", "sklearn", "IPython", "jupyter",
              "notebook", "docutils", "pydoc_data"],
    win_no_prefer_redirects=False,
    win_private_assemblies=False,
    cipher=block_cipher,
    noarchive=False,
)
pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="wechat-triage-hud",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,                     # UPX 压缩过 Qt DLL 容易被杀软误报，不好排查
    # 默认无控制台（HUD 不该弹黑框）；排查打包问题时用 HUD_BUILD_CONSOLE=1 出一版带控制台的，
    # 启动期的 ImportError 会直接打在终端上（否则 windowed 版只会弹个对话框、看不到细节）。
    console=bool(os.environ.get("HUD_BUILD_CONSOLE")),
    disable_windowed_traceback=False,
    icon=ICON,
)

coll = COLLECT(
    exe,
    a.binaries,
    a.zipfiles,
    a.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name="wechat-triage-hud",
)
