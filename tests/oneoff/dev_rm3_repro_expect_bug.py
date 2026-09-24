# -*- coding: utf-8 -*-
"""复现 dev_p0_verify.py [B] 段崩溃点（不改动被测脚本，只复用它前面部分的定义）。

结论预期：expect_hud_xy() 收到的 scr 是 **tuple**（screen_rect 的返回值），
但函数内部按 wintypes.RECT 调用了 scr.bottom()/scr.right()/scr.left()
-> AttributeError: 'tuple' object has no attribute 'bottom'
异常打印到 stdout（Windows 下 stderr 易被外层吞掉）。
"""
import os
import sys
import traceback

PROJ = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.chdir(PROJ)
sys.path.insert(0, PROJ)

src = open(os.path.join(PROJ, ".tests", "dev_p0_verify.py"), encoding="utf-8").read()
src = src.split("def main")[0]
g = {}
exec(compile(src, "dev_p0_verify.py(preamble)", "exec"), g)

scr = g["screen_rect"]
print("screen_rect 定义源码行：")
for i, ln in enumerate(src.splitlines()):
    if "def screen_rect" in ln:
        print("\n".join("    " + x for x in src.splitlines()[i:i + 6]))
        break
print("screen_rect() ->", scr())
print("type(screen_rect()) ->", type(scr()).__name__)
print("hasattr(scr,'bottom') ->", hasattr(scr(), "bottom"))
print()
print("现在按脚本实际调用方式调用 expect_hud_xy(微信rect, 707, 260, screen_rect) ：")
try:
    r = g["expect_hud_xy"]((1240, 21, 1897, 924), 707, 260, scr())
    print("  返回 ->", r)
except Exception:
    print("  抛异常：")
    print("".join("    " + x for x in traceback.format_exc().splitlines(keepends=True)))
    raise SystemExit(0)
print("  未抛异常（与预期不符）")
