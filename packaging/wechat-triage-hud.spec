# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller 规格文件（onedir + 无控制台）。

用法：`pyinstaller --noconfirm packaging/wechat-triage-hud.spec`（或直接跑 build.bat）

为什么这么配（都是踩过/查过的）：
- **onedir 而不是 onefile**：onefile 每次启动要把 ~200MB 解包到临时目录，面板启动要等十几秒，
  而且解包目录会被当成"程序目录"（`paths.resolve_project_dir` 已处理，但没必要受这个罪）。
- **windowed（无控制台）**：这是常驻 HUD，弹个黑框很难看；无控制台时 `sys.stdout` 是 None，
  所以入口用 `packaging/run_hud.py`（它先把 stdout/stderr 换成桩）。
- **`--collect-data rapidocr_onnxruntime`**：它自带字典/config 与 onnx 模型，不收就 OCR 直接失败。
- `win32gui/win32con/win32api` 是 pywin32 的子模块，PyInstaller 有时抓不全，显式列上。
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

# conda 的 Python：DLL 放在 <prefix>\Libraryin，PyInstaller 不会自动收 —— 后果是
# 打包好的 exe 一启动就 `ImportError: DLL load failed while importing _ctypes`（真踩过）。
# 这里把运行时真的需要的几个按名字收进来（`ffi-8.dll` 就是 _ctypes 依赖的那个；
# conda 管它叫 ffi-8.dll，不叫 libffi*.dll，所以按 "ffi*.dll" 匹配）。
import glob
import sys as _sys
_CONDA_BIN = os.path.join(_sys.prefix, "Library", "bin")
if os.path.isdir(_CONDA_BIN):
    for _pat in ("ffi*.dll", "liblzma.dll", "libbz2.dll", "libexpat.dll",
                 "zlib.dll", "libzstd*.dll", "libjpeg.dll", "libpng16.dll",
                 "libtiff.dll", "libwebp*.dll", "concrt140.dll",
                 "msvcp140*.dll", "vcruntime140*.dll",
                 # conda 的 PySide6 把 Qt/shiboken 的 DLL 也放在这里（名字是
                 # shiboken6.cp313-win_amd64.dll，pip 轮子里才叫 shiboken6.abi3.dll）——
                 # 不收它们的后果是打包好的 exe 启动时报
                 # `ImportError: DLL load failed while importing Shiboken`。
                 # 只放"入口"三个 Qt DLL，其余依赖 PyInstaller 自己分析出来。
                 "shiboken6*.dll", "pyside6*.dll",
                 "Qt6Core.dll", "Qt6Gui.dll", "Qt6Widgets.dll",
                 "icudt*.dll", "icuin*.dll", "icuuc*.dll"):
        for _f in glob.glob(os.path.join(_CONDA_BIN, _pat)):
            if not _f.endswith(".dll"):
                continue
            binaries.append((_f, "."))

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
    excludes=["tkinter", "matplotlib", "pandas", "pytest", "setuptools"],
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
    console=False,                 # 无控制台（HUD）
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
