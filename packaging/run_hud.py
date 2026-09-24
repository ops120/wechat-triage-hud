# -*- coding: utf-8 -*-
"""打包成 exe 的入口（PyInstaller 用这个文件当入口，不要直接指 hud.py）。

为什么需要它：
- `hud.py` 是包内模块（用相对导入 `from .paths import ...`），直接当脚本跑会炸；
  这里从包外导入，路径与 `pyproject.toml` 里的控制台命令 `wechat-triage-hud` 一致。
- 打包成 windowed（无控制台）后 `sys.stdout/stderr` 是 None，代码里任何 `print()`
  都会抛 AttributeError。这里先把它们接到一个吞掉写入的桩上，再把信息写进调试日志。
"""
from __future__ import annotations

import os
import sys


_QAPP = None        # 自检里那个 QGuiApplication 的引用（必须活着）


class _Null:
    """替掉 None 的 stdout/stderr：只吞写入，不当异常源。"""

    def write(self, _s):
        return 0

    def flush(self):
        return None

    def isatty(self):
        return False


def _fix_streams() -> None:
    if sys.stdout is None:
        sys.stdout = _Null()          # type: ignore[assignment]
    if sys.stderr is None:
        sys.stderr = _Null()          # type: ignore[assignment]


def _selftest_ocr() -> int:
    """验「打包版里的 OCR 到底能不能跑」，结果写到 out/selftest_ocr.txt。

    为什么非要有它：我只测过"exe 能启动"，没测 OCR —— 用户一用就撞上
    `AttributeError: module 'ch_ppocr_v3_det' has no attribute 'TextDetector'`
    （PyInstaller 只收了 rapidocr 的数据文件，没收它的子模块）。
    现在这条自检会在**不依赖微信窗口**的情况下把 OCR 引擎真跑一遍。
    """
    import io as _io
    from wechat_triage_hud import paths as P

    ok = False
    lines = []
    try:
        import numpy as np
        from PySide6.QtGui import QColor, QGuiApplication, QFont, QImage, QPainter

        # 画字要用 QGuiApplication（不然 QFontDatabase 直接报错 —— 自检自己先踩了一次）。
        # 存成**模块级全局**：局部变量会被回收，QApplication 一生效对象就崩。
        global _QAPP
        _QAPP = QGuiApplication.instance() or QGuiApplication([])
        img = QImage(360, 90, QImage.Format_RGB888)
        img.fill(QColor("white"))
        pt = QPainter(img)
        pt.setPen(QColor("black"))
        f = QFont()
        f.setPointSize(30)
        pt.setFont(f)
        pt.drawText(10, 60, "你好 hello 123")
        pt.end()
        w, h = img.width(), img.height()
        buf = (np.frombuffer(img.constBits(), np.uint8)
               .reshape(h, img.bytesPerLine())[:, :w * 3].reshape(h, w, 3).copy())

        from wechat_triage_hud.wechat_capture import _get_ocr
        ocr = _get_ocr()
        res, elapse = ocr(buf)
        texts = [t[1] for t in (res or [])]
        ok = bool(texts)
        lines.append("OCR 引擎加载：OK")
        lines.append(f"识别到 {len(texts)} 段（{elapse}）：{texts}")
    except Exception as e:                       # noqa: BLE001
        lines.append(f"OCR 失败：{type(e).__name__}: {e}")
    try:
        os.makedirs(P.OUT_DIR, exist_ok=True)
        with _io.open(os.path.join(P.OUT_DIR, "selftest_ocr.txt"), "w",
                      encoding="utf-8") as f:
            f.write(os.linesep.join(lines) + os.linesep)   # 别用 "\n" 字面量：脚本被覆写过一次，转义踩过坑
    except Exception:
        pass
    return 0 if ok else 1


def main() -> int:
    if not getattr(sys, "frozen", False):
        # 开发态下直接跑这个文件（`python packaging/run_hud.py --selftest-ocr`）时，
        # sys.path 上是 packaging/ 而不是仓库根 —— 补一下才能 import 到包。
        _root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        if _root not in sys.path:
            sys.path.insert(0, _root)
    if "--selftest-ocr" in sys.argv:
        return _selftest_ocr()
    _fix_streams()
    if os.environ.get("JEV_HUD_DEBUG") == "1":
        # 打包后没有控制台，就把启动参数与工作目录写进调试日志，方便排查
        try:
            from wechat_triage_hud import paths as P
            from wechat_triage_hud.hud import dbg
            dbg(f"启动（打包版）：exe={sys.executable} 数据目录={P.PROJECT_DIR} argv={sys.argv[1:]}")
        except Exception:
            pass
    from wechat_triage_hud.hud import main as _main
    return _main()


if __name__ == "__main__":
    raise SystemExit(main())
