"""置位/读取/还原 Windows 屏幕阅读器标志（SPI_SETSCREENREADER）。

为什么需要：微信 4.x 是 Qt 自绘应用，其 mmui 无障碍树**仅当该标志在
微信进程启动时已置位才会构建**。实测（4.1.15.12）：未置位时真实聊天窗口
（hwnd=986286「幂喵」896x648）只暴露 1 个节点、零 mmui 内容。
且置位后必须**重启微信**才生效——事后广播/WM_GETOBJECT 均无效。

这个标志是全机范围的可访问性状态，会影响其他应用对"是否有屏幕阅读器"的判断。
它是**可还原**的：用 --off 关闭。

用法：
    python screenreader_flag.py           # 读取当前状态
    python screenreader_flag.py --on      # 置位
    python screenreader_flag.py --off     # 还原
"""
import ctypes
import sys

SPI_GETSCREENREADER = 0x0046
SPI_SETSCREENREADER = 0x0047
SPIF_UPDATEINIFILE = 0x01
SPIF_SENDCHANGE = 0x02

u32 = ctypes.windll.user32


def get() -> bool:
    v = ctypes.c_int()
    ok = u32.SystemParametersInfoW(SPI_GETSCREENREADER, 0, ctypes.byref(v), 0)
    if not ok:
        raise OSError("SystemParametersInfoW(SPI_GETSCREENREADER) 失败")
    return bool(v.value)


def set_(on: bool) -> bool:
    u32.SystemParametersInfoW(
        SPI_SETSCREENREADER, 1 if on else 0, None,
        SPIF_UPDATEINIFILE | SPIF_SENDCHANGE)
    return get()


def main():
    args = sys.argv[1:]
    before = get()
    print(f"当前 SPI_GETSCREENREADER = {before}")

    if "--on" in args:
        if before:
            print("已经是置位状态，无需操作。")
        else:
            print(f"置位 → {set_(True)}")
            print("注意：对**已运行**的微信无效，必须重启微信才会重建控件树。")
    elif "--off" in args:
        print(f"还原 → {set_(False)}")
        print("（还原后同样需要重启微信才会退回无控件树状态。）")
    else:
        print("用法: --on 置位 / --off 还原")


if __name__ == "__main__":
    main()
