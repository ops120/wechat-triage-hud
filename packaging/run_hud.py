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


def main() -> int:
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
