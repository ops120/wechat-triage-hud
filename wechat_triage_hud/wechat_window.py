"""定位并跟踪 PC 微信主窗口。

实测要点（微信 4.1.15.12，E:\\wx2026）：
  * 主窗口 class = Qt51514QWindowIcon，title = 当前聊天对象名（会变）
  * 主窗口与托盘消息窗(WxTrayIconMessageWindowClass)、小窗 'Weixin' 并存，
    必须按 area + 非空 title + 排除白名单来挑
  * 最小化时 rect 变成 (-32000,-32000,...)，area 计算会失效
    → 用 IsIconic 判断，用 GetWindowPlacement.rcNormalPosition 取还原后的位置
  * Weixin.exe 有多个进程（主进程+辅助），窗口 PID 不固定，需按 exe 名筛
"""
from __future__ import annotations

import ctypes
import ctypes.wintypes  # 让 ctypes.wintypes 可用（HWND）
from dataclasses import dataclass

import psutil
import win32con
import win32gui

EXCLUDE_TITLES = {"Weixin", "WxTrayIconMessageWindow", "微信"}
MIN_AREA = 60_000


def wechat_pids() -> set[int]:
    pids = set()
    for p in psutil.process_iter(["pid", "name"]):
        try:
            n = (p.info["name"] or "").lower()
            if n in ("weixin.exe", "wechat.exe"):
                pids.add(p.info["pid"])
        except Exception:
            pass
    return pids


@dataclass
class WinInfo:
    hwnd: int
    title: str
    cls: str
    rect: tuple          # 视觉位置（最小化时是 -32000）
    normal_rect: tuple   # 还原后的位置（GetWindowPlacement）
    minimized: bool
    visible: bool

    @property
    def width(self) -> int:
        return self.normal_rect[2] - self.normal_rect[0]

    @property
    def height(self) -> int:
        return self.normal_rect[3] - self.normal_rect[1]


def _normal_rect(hwnd: int) -> tuple:
    try:
        pl = win32gui.GetWindowPlacement(hwnd)
        return tuple(pl[4])  # rcNormalPosition
    except Exception:
        return tuple(win32gui.GetWindowRect(hwnd))


def list_windows() -> list[WinInfo]:
    pids = wechat_pids()
    out: list[WinInfo] = []

    def cb(h, _):
        try:
            pid = ctypes.c_ulong()
            ctypes.windll.user32.GetWindowThreadProcessId(h, ctypes.byref(pid))
            if pid.value not in pids:
                return True
            cls = win32gui.GetClassName(h)
            title = win32gui.GetWindowText(h)
            if not cls.startswith("Qt") or "QWindowIcon" not in cls:
                return True
            if title in EXCLUDE_TITLES or not title.strip():
                return True
            if "TrayIcon" in cls:
                return True
            nr = _normal_rect(h)
            w, hh = nr[2] - nr[0], nr[3] - nr[1]
            if w * hh < MIN_AREA:
                return True
            out.append(WinInfo(
                hwnd=h, title=title, cls=cls,
                rect=tuple(win32gui.GetWindowRect(h)),
                normal_rect=nr,
                minimized=bool(win32gui.IsIconic(h)),
                visible=bool(win32gui.IsWindowVisible(h)),
            ))
        except Exception:
            pass
        return True

    win32gui.EnumWindows(cb, None)
    out.sort(key=lambda w: -(w.width * w.height))
    return out


def find_main(sticky_hwnd: int | None = None) -> WinInfo | None:
    """优先返回上次用过的 hwnd（会话切换时标题会变，但 hwnd 稳定）。"""
    wins = list_windows()
    if sticky_hwnd is not None:
        for w in wins:
            if w.hwnd == sticky_hwnd:
                return w
    return wins[0] if wins else None


def restore(hwnd: int) -> None:
    """把最小化的微信还原（不强制抢焦点）。"""
    if win32gui.IsIconic(hwnd):
        win32gui.ShowWindow(hwnd, win32con.SW_RESTORE)


# --------------------------------------------------------------------------
# 遮挡判定 —— 采集前的强制前置检查
# --------------------------------------------------------------------------
class _POINT(ctypes.Structure):
    _fields_ = [("x", ctypes.c_long), ("y", ctypes.c_long)]


_u32 = ctypes.windll.user32
_u32.WindowFromPoint.restype = ctypes.wintypes.HWND
_u32.WindowFromPoint.argtypes = [_POINT]


def _root_of(hwnd: int) -> int:
    """一路向上找顶层窗口。"""
    cur = hwnd
    for _ in range(32):
        parent = win32gui.GetParent(cur)
        if not parent:
            return cur
        cur = parent
    return cur


def occluders(hwnd: int, rect: tuple | None = None,
              ignore: set[int] | None = None) -> list[int]:
    """返回盖住该窗口采样点的顶层窗口句柄（去重）。诊断与测试用。"""
    if not hwnd or not win32gui.IsWindow(hwnd):
        return []
    rect = rect or win32gui.GetWindowRect(hwnd)
    l, t, r, b = rect
    if r - l < 80 or b - t < 80:
        return []
    ignore = ignore or set()
    w, h = r - l, b - t
    found = []
    for x, y in [(l + w // 2, t + 12), (l + w // 2, t + h // 2),
                 (l + int(w * 0.15), t + h // 2),
                 (l + int(w * 0.75), t + int(h * 0.35))]:
        top = _u32.WindowFromPoint(_POINT(x, y))
        if not top:
            continue
        root = _root_of(top)
        if root != hwnd and root not in ignore and root not in found:
            found.append(root)
    return found


def window_rect(hwnd: int) -> tuple[int, int, int, int] | None:
    """任意窗口的屏幕矩形 (l, t, r, b)。

    用途：把**面板自己的矩形**拿出来做自遮挡判定 —— 面板是置顶的，它压住微信
    消息区时，截屏会把面板上的字读成群消息（实测真发生过：群里出现了叫
    "、、品群里的人：点一个人着判断，，判新注府2" 的会话）。
    """
    try:
        import ctypes.wintypes as wt
        r = wt.RECT()
        if not ctypes.windll.user32.GetWindowRect(wt.HWND(hwnd), ctypes.byref(r)):
            return None
        return (r.left, r.top, r.right, r.bottom)
    except Exception:
        return None


def occlusion(hwnd: int, rect: tuple | None = None,
              ignore: set[int] | None = None) -> tuple[bool, str]:
    """检查微信窗口是否被别的窗口盖住。返回 (是否被遮挡, 说明)。

    为什么必须有：截屏抓的是**屏幕上该矩形里的像素**，不是"微信窗口的内容"。
    微信被盖住时，OCR 读到的是覆盖层的东西（实测读到过编辑器里本项目报告的文字），
    然后会被当成群消息发给模型 —— 不报错、看着还很确定，且已经写进审计日志。

    ignore: 需要忽略的窗口句柄（通常是我们自己的悬浮面板，它是置顶的，
            用户把它拖到微信上时不应算作"遮挡"）。
    """
    if not hwnd or not win32gui.IsWindow(hwnd):
        return True, "微信窗口句柄无效"
    if rect is None:
        rect = win32gui.GetWindowRect(hwnd)
    l, t, r, b = rect
    if r - l < 80 or b - t < 80:
        return True, f"窗口尺寸异常 {r-l}x{b-t}"

    ignore = ignore or set()
    w, h = r - l, b - t
    samples = [
        (l + w // 2, t + 12),                 # 标题栏
        (l + w // 2, t + h // 2),             # 消息区中心
        (l + int(w * 0.15), t + h // 2),      # 左列（会话列表）
        (l + int(w * 0.75), t + int(h * 0.35)),  # 右列上方
    ]
    covered = []
    for x, y in samples:
        top = _u32.WindowFromPoint(_POINT(x, y))
        if not top:
            continue
        root = _root_of(top)
        if root == hwnd or root in ignore:
            continue
        covered.append((x, y, root))

    if not covered:
        return False, ""
    x, y, root = covered[0]
    try:
        cls = win32gui.GetClassName(root)
        title = win32gui.GetWindowText(root)[:24]
    except Exception:
        cls, title = "?", "?"
    return True, (f"{len(covered)}/{len(samples)} 个采样点被别的窗口覆盖"
                  f"（最顶层 hwnd={root} class={cls!r} title={title!r}）")


def client_rect_of(hwnd: int) -> tuple:
    """窗口客户区在屏幕上的坐标（去掉标题栏与边框）。"""
    r = win32gui.GetClientRect(hwnd)
    l, t = win32gui.ClientToScreen(hwnd, (r[0], r[1]))
    rr, bb = win32gui.ClientToScreen(hwnd, (r[2], r[3]))
    return (l, t, rr, bb)


def visible_rect_of(hwnd: int) -> tuple:
    """窗口的 **DWM 可见边界**（屏幕坐标）。

    为什么单独要这个：`GetWindowRect` 比可见边界大 7px（左/右/下各 7px 不可见边框，
    实测 2026-09-23），而抓帧抓的正是 GetWindowRect 那个矩形。两个坐标混用就是
    7px 系统性偏移 —— 采集侧（wechat_capture）统一用**帧坐标**，帧右边 − 7px 才是
    可见右边界；这个函数用来现取可见边界做对照/复测，不要拿它去和帧内坐标相加。
    """
    try:
        r = ctypes.wintypes.RECT()
        # DWMWA_EXTENDED_FRAME_BOUNDS = 9
        ctypes.windll.dwmapi.DwmGetWindowAttribute(
            ctypes.wintypes.HWND(hwnd), 9, ctypes.byref(r), ctypes.sizeof(r))
        return (r.left, r.top, r.right, r.bottom)
    except Exception:
        return tuple(win32gui.GetWindowRect(hwnd))


if __name__ == "__main__":
    ws = list_windows()
    if not ws:
        print("未找到微信主窗口（可能未登录或未运行）")
    for w in ws:
        print(f"hwnd={w.hwnd} title={w.title!r} {w.width}x{w.height} "
              f"normal={w.normal_rect} minimized={w.minimized} vis={w.visible}")
    if ws:
        print("\n客户区:", client_rect_of(ws[0].hwnd))
        r, v = ws[0].rect, visible_rect_of(ws[0].hwnd)
        print(f"帧矩形(GetWindowRect): {r}\nDWM 可见边界        : {v}\n"
              f"差值(帧-可见)       : "
              f"{tuple(a - b for a, b in zip(r, v))}")
