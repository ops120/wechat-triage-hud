"""判定：删掉「✕ 退出」之后，标题栏剩下的每个按钮是不是都还对。

用户要求「取消退出按钮，并让我验证其它按钮是否正确」——「正确」不该靠眼睛猜，
所以这里用**真实点击**（QTest 打在真的 QPushButton 上，走 clicked → 槽 那条路）
把每个控件走一遍，把观察到的效果打出来。

自检期间**不联网、不截屏、不动真数据**：
  · 微信窗口用桩替代 —— 吸附/隐藏两条分支靠改桩的状态确定性地触发
  · wc.grab 被换成"直接抛错" —— 万一扫描线程漏跑一轮也不会 OCR、更不会调 API
  · 审计日志与群身份文件都写到临时目录（「日志」按钮那条是真清空，不能拿真日志试）
"""
import inspect
import io
import json
import os
import sys
import tempfile
import time

# 默认跑在 Qt 的 offscreen 平台上：自检会真的建面板、真的弹右键菜单/对话框，
# 它们全是真窗口，会实打实闪在用户屏幕正中（2026-09-24 用户截图连问两次
# "怎么一直出现"、"弹出来是干啥用"）。offscreen 下事件、几何、isVisible()
# 语义都不变，只是不投到屏幕上。想亲眼看一遍时：HUD_SELFCHECK_VISIBLE=1 再跑。
if not os.environ.get("HUD_SELFCHECK_VISIBLE"):
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

PROJECT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PROJECT_DIR)

from PySide6.QtCore import (QEvent, QObject, QPoint, QPointF, Qt, Signal,
                             QThread, QTimer)   # noqa: E402
from PySide6.QtGui import QContextMenuEvent, QMouseEvent         # noqa: E402
from PySide6.QtTest import QTest                         # noqa: E402
from PySide6.QtWidgets import (                          # noqa: E402
    QApplication, QDialog, QLabel, QMenu, QPushButton, QWidget,
)

from wechat_triage_hud import hud as H                   # noqa: E402
from wechat_triage_hud import person_engine as PE        # noqa: E402
from wechat_triage_hud import qset                       # noqa: E402

TMP = tempfile.mkdtemp(prefix="hud_btn_")
_REAL_SCANNER = None      # unit_stubs() 里保存的真实 Scanner（第 18 节用）
_REAL_WORKER = None
RESULTS: list[tuple[bool, str, str]] = []


def check(name: str, ok: bool, detail: str = "") -> bool:
    RESULTS.append((bool(ok), name, detail))
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}"
          + (f" —— {detail}" if detail else ""))
    return bool(ok)


def info(text: str) -> None:
    print(f"  [INFO] {text}")


def teardown(b) -> None:
    """收掉一个实例：停跟随定时器、停线程，**并等线程真的退出**。

    只 stop 不 wait 会留下正在跑的 QThread：扫描线程可能还在跑一轮 OCR（~4s），
    等 Python 把对象回收掉，线程就在已释放的内存上继续跑 —— 整个测试进程随机段错误
    （实测 3 次里崩 1 次，而且是在所有断言跑完之后才崩，看着像全绿）。
    另外不停定时器的话，前面几个实例会一直在后台跑 follow()（每 600ms），
    和后面小节抢同一个存档文件 —— 实测会让"没有存档时默认面板"这类断言偶发失败。
    """
    try:
        b.t_pos.stop()
    except Exception:
        pass
    b.scanner.stop()
    b.worker.stop()
    try:
        b.scanner.wait(5000)
        b.worker.wait(5000)
    except Exception:
        pass
    b.hide()


def menu_probe(widget, trigger_last: bool = False,
               trigger: str | None = None) -> list[str]:
    """抄下右键菜单项（trigger_last=True 顺手点最后一项；trigger="文案" 点指定那项）。

    直接问产品要菜单对象（`_build_context_menu()`），**不弹出来**。前一版是真发
    QContextMenuEvent 去弹真菜单，代价一大把：
      · 菜单一次次实打实闪在用户屏幕正中 —— 用户连问两次"怎么一直出现""弹出来是干啥用"
      · offscreen 平台（自检默认跑的）下 `QMenu.exec()` 的嵌套循环永不返回 → 整脚本吊死
      · 断言还得跟菜单弹出抢时间：菜单关闭那一瞬 Qt 会把面板隐藏再显示，
        于是"右键收成小球"这条 3 次里偶发红 1 次（尺寸/ball_mode 都对，就 isVisible() 还是 False）
    现在菜单项仍来自产品的同一段构建代码，trigger 走的也是真实 QAction 通路，
    唯一没覆盖的是"exec 能把菜单显示出来"（那一步由第 12 节末尾的真弹一次兜底）。
    """
    menu = widget._build_context_menu()
    acts = [a.text() for a in menu.actions() if not a.isSeparator()]
    if trigger is not None:
        hit = [a for a in menu.actions() if a.text() == trigger]
        if hit:                              # 按标题点某一项（菜单项文案会被断言同时验证）
            hit[0].trigger()
    elif trigger_last:
        menu.actions()[-1].trigger()
    menu.deleteLater()
    return acts


# --------------------------------------------------------------------------
# 桩：微信窗口 + 不截屏
# --------------------------------------------------------------------------
class FakeWin:
    def __init__(self):
        self.hwnd = 424242
        self.rect = (300, 100, 1000, 800)     # 宽 700 的"微信"
        self.minimized = False
        self.visible = True


class FakeWW:
    """替掉 wechat_window：位置与可见性要可控，且完全不碰真实桌面。"""

    def __init__(self):
        self.win = FakeWin()

    def find_main(self, hwnd=None):
        return self.win

    def occlusion(self, *a, **k):
        return (False, "")

    def window_rect(self, hwnd):
        # 默认"拿不到面板矩形"；第 18 节（自遮挡）会临时替换成具体矩形
        return None


def no_grab(*a, **k):
    raise RuntimeError("按钮自检：不截屏")


def build_bar(fake: FakeWW) -> H.HudBar:
    bar = H.HudBar()
    bar.scanner.stop()                       # 构造时就启动了，立刻停
    bar.store = H.GroupStore(os.path.join(TMP, "meta.json"))
    bar.group, bar.topic = "自检群", "自检主题"
    bar.viewer = {}
    bar.refresh_me_button()
    bar.show()
    # 自检面板是真的无边框置顶窗口，会实打实弹在用户屏幕上（2026-09-24 用户看到
    # 右下角冒出面板、点了一下还以为是自己的窗口）。**无头模式下**调成 0.02 透明度：
    # 几何、isVisible()、QTest 事件全照旧，屏幕上却什么都没有。
    # 想亲眼看（HUD_SELFCHECK_VISIBLE=1）时保持正常透明度 —— 那时就是给人看的。
    if os.environ.get("QT_QPA_PLATFORM") == "offscreen":
        bar.setWindowOpacity(0.02)
    QApplication.processEvents()
    fake.win.minimized = False
    fake.win.visible = True
    return bar


def fake_detail(nick: str) -> dict:
    return {
        "nickname": nick, "msgs": ["我感觉，过年前这个PCB设计软件能搓出来"],
        "state": "alert", "attention": 8, "attention_mass": 1.0,
        "state_reasons": ["点我名"],
        "role": next(iter(qset.LABELS)), "intent": next(iter(qset.LABELS)),
        "addressed_to_me": 0.7, "worth": "should_reply", "worth_conf": 0.8,
        "sufficiency": 2.4,
        "risks": {"ad": 0.02, "fraud": 0.01, "conflict": 0.0, "illegal": 0.0},
        "model": "自检", "usage": {"input_tokens": 1}, "cached": True,
        "scene": "group", "pending_detail": False,
    }


def right_widgets(bar) -> list:
    """右栏当前摆着的所有控件（子布局、行容器里的都算）。

    三个坑都踩过：
      · `bar.r_body` 是 **QLayout 不是 QWidget** —— 在它上面 findChildren() 永远是空的
        （明明"正在判这条…"和返回按钮都在，却返回 []）；
      · 只取 `itemAt(i).widget()` 会漏掉嵌在**子布局**里的东西；
      · 「真实意图/对方需要」这些"键+值"是套在**行容器 QWidget** 里的（`_kv_pairs`
        每行一个容器装两格），只认顶层项会只剩下状态行和"风险"标题。
    """
    out: list = []

    def walk(lay) -> None:
        for i in range(lay.count()):
            it = lay.itemAt(i)
            w = it.widget()
            if w is not None:
                out.append(w)
                out.extend(w.findChildren(QWidget))     # 行容器里的 QLabel
            sub = it.layout()
            if sub is not None:
                walk(sub)

    walk(bar.r_body)
    return out


def ghost_menus() -> None:
    """让自检弹的**菜单**也近乎透明。

    QMenu 是独立的顶层窗口，不吃面板那份透明度 —— 面板看不见了，菜单还是一次次
    实打实闪在用户屏幕正中间（2026-09-24 用户截图问"怎么一直出现"）。这里在 show
    之前改透明度：菜单项、事件、断言全照旧，只是肉眼看不见。
    **必须在 exec/popup 内部、show 之前设**：菜单建好后再设会先闪一帧。
    **只在无头模式打这个补丁**：HUD_SELFCHECK_VISIBLE=1 时用户就是要看这些菜单。
    """
    if os.environ.get("QT_QPA_PLATFORM") != "offscreen":
        return                  # 可视模式：菜单/对话框就该让人看见，不调透明度
    real_exec, real_popup = QMenu.exec, QMenu.popup
    real_dlg_exec = QDialog.exec

    def _fade(menu):
        menu.setWindowOpacity(0.02)
        return menu

    def exec_(self, *a, **k):
        _fade(self)
        return real_exec(self, *a, **k)

    def popup(self, *a, **k):
        _fade(self)
        return real_popup(self, *a, **k)

    def dlg_exec(self, *a, **k):
        _fade(self)          # 「填写我在本群的昵称」那个真对话框也是窗口，同样别让人看见
        return real_dlg_exec(self, *a, **k)

    QMenu.exec = exec_
    QMenu.popup = popup
    QDialog.exec = dlg_exec


def unit_stubs() -> None:
    """把 Scanner / TriageWorker 换成**没有线程**的惰性桩。

    为什么必须这么做：真实实现会起两个 QThread。测试收尾时线程可能还在跑一轮 OCR，
    之后对象被回收 → 线程在已释放内存上继续跑 → 整个进程随机段错误（实测 4 次崩 1 次，
    而且是在所有断言跑完之后才崩 —— 输出看着 62/62 全绿，退出码却是 139）。
    这个脚本验的是按钮/布局/数据流，本来就不验真实扫描（`wc.grab` 也早就被打桩了），
    所以把线程去掉既不影响覆盖面，又能让"退出码"这个门槛变确定。
    """

    class _NoScanner(QObject):                 # 与 Scanner 同名的信号，行为全空
        scanned = Signal(object)
        failed = Signal(str)
        blocked = Signal(str)
        tick = Signal(dict)

        def __init__(self, *a, **k):
            super().__init__()
            self.ignore_hwnd = None
            self.names = None
            self.titles = None
            self._stop = False          # 看门狗会读它（暂停期间不该被当成卡死）
            self.paused = False
            self.last_tick_at = time.time()

        def start(self):
            pass

        def stop(self):
            pass

        def wait(self, *a):
            return True        # 产品的 _shutdown() 会调 QThread.wait()，桩得接住

        def isRunning(self):
            return True        # 看门狗会问这个；桩没有线程，报"在跑"免得被反复重启

    class _NoWorker(QObject):
        done = Signal(dict)
        failed = Signal(str)
        stage = Signal(dict)
        msg_done = Signal(dict)

        def __init__(self, *a, **k):
            super().__init__()
            self._stop = False
            self._job = None
            self._busy = False
            self.client = None
            self.last_msg_job = None
            self.jobs = []
            self.profile = H.SpeakerProfile()
            self.throttle = H.Throttle()
            self.cold_start = True

        def start(self):
            pass

        def stop(self):
            pass

        def submit(self, job):
            self.jobs.append(job)      # 记下每一次提交：断言"该提交的提交了、该拦的拦住了"

        def drop_jobs(self):
            pass

        def submit_msg(self, job):
            self.last_msg_job = job          # 桩：记下来，自检断言"只送了这一条"

        def wait(self, *a):
            return True

    global _REAL_SCANNER, _REAL_WORKER
    _REAL_SCANNER, _REAL_WORKER = H.Scanner, H.TriageWorker   # 第 18 节要验真实实现
    H.Scanner = _NoScanner
    H.TriageWorker = _NoWorker


def main() -> int:
    # 先打桩再构造：TriageWorker.run 一上来就会读 AUDIT_PATH / ENV_PATH
    fake = FakeWW()
    H.wc.grab = no_grab
    H.ww = fake
    H.AUDIT_PATH = os.path.join(TMP, "audit.jsonl")
    with open(H.AUDIT_PATH, "wb") as f:
        f.write(b"x" * 1234)
    # UiPrefs 的默认参数在 def 那一刻就绑定了真实 UI_PATH，改模块常量没用 ——
    # 必须换掉类本身。不换的话，用户真实存档里的 ball_mode/ball_xy 会渗进测试
    # （存档若恰好是"小球"，后面每一条断言都会莫名其妙地失败）。
    real_prefs = H.UiPrefs
    prefs_path = os.path.join(TMP, "ui_prefs.json")
    H.UiPrefs = lambda *a, **k: real_prefs(prefs_path)
    unit_stubs()          # 去线程化：退出码必须确定（见 unit_stubs 的注释）
    ghost_menus()         # 菜单也别让人看见（见 ghost_menus 的注释）
    # 累积历史也写到临时目录：否则测试里的 remember()/flush 会污染真机 out/speaker_history.json
    H.HIST_PATH = os.path.join(TMP, "speaker_history.json")

    app = QApplication.instance() or QApplication(sys.argv)
    print("=== HUD 按钮自检（真实点击） ===")

    bar = build_bar(fake)

    # ---- 1. 按钮清单：✕ 必须已经不在 ----
    btns = [b.text() for b in bar.findChildren(QPushButton)]
    info(f"标题栏+面板上的按钮：{btns}")
    check("「✕ 退出」按钮已移除", "✕" not in btns, f"实际：{btns}")
    check("标题栏 = 填群信息 + 日志 + 📌 + 关闭（▴ 与 ◍ 都已移除）",
          all(t in btns for t in ("填群信息", "日志", "📌", "关闭"))
          and "▴" not in btns and "▾" not in btns and "◍" not in btns,
          f"实际：{btns}")

    # ---- 2. 吸附（未固定时面板跟微信走） ----
    bar.btn_pin.setChecked(False)
    bar.move(10, 10)
    bar.follow()
    moved = (bar.x(), bar.y()) != (10, 10)
    check("未固定时 follow() 把面板吸回微信旁", moved,
          f"从 (10,10) → ({bar.x()},{bar.y()})，宽 {bar.width()}")
    # 宽度契约：不小于微信宽，且**不小于内容真正需要的宽度**（真机踩过：被钉在微信宽度上
    # → 右栏的值全被裁到面板外，只剩标签）
    need = bar.body_host.minimumSizeHint().width()
    check("宽度 ≥ 微信宽，且足够放下两列（不被裁）",
          bar.width() >= 700 and bar.width() - 34 >= need,
          f"面板宽 {bar.width()} / 微信 700 / 内容需要 {need}")

    # ---- 3. 微信不可见 → 面板自己收起来 ----
    fake.win.minimized = True
    bar.follow()
    check("微信最小化时面板自动隐藏", not bar.isVisible())
    fake.win.minimized = False
    bar.follow()
    check("微信回来后自动显示", bar.isVisible())

    # ---- 4. 📌 固定 / 再点取消 ----
    QTest.mouseClick(bar.btn_pin, Qt.LeftButton)
    check("📌 单击进入固定", bar.pinned is True and "已固定" in bar.btn_pin.toolTip(),
          f"pinned={bar.pinned} tip={bar.btn_pin.toolTip()!r}")
    bar.move(10, 10)
    bar.follow()
    check("固定后 follow() 不再挪动面板（位置是我给的）",
          (bar.x(), bar.y()) == (10, 10), f"仍在 ({bar.x()},{bar.y()})")

    # ---- 5. 双击面板 = 解除固定（回到吸附位） ----
    bar.move(10, 10)
    QTest.mouseDClick(bar, Qt.LeftButton,
                      pos=QPoint(bar.width() - 5, bar.height() - 5))
    bar.follow()
    check("双击面板任意处解除固定", bar.pinned is False,
          f"pinned={bar.pinned}，位置 ({bar.x()},{bar.y()})")

    # ---- 6. 折叠详情（标题栏的 ▴ 已按用户要求去掉，2026-09-24）----
    # 入口只剩右键菜单：「收起详情 / 展开详情」—— 与删 ✕、删 ◍ 的处理一致：
    # 按钮可以去掉，能力得留一条路（否则"折叠"就真没了）。
    before = (bar.scroll.isVisible(), bar.foot.isVisible(), bar._expanded)
    acts_fold = menu_probe(bar, trigger="收起详情")
    mid = (bar.scroll.isVisible(), bar.foot.isVisible(), bar._expanded)
    menu_probe(bar, trigger="展开详情")
    after = (bar.scroll.isVisible(), bar.foot.isVisible(), bar._expanded)
    info(f"折叠前 详情/底部/标志={before} → 菜单收起 {mid} → 菜单展开 {after}")
    check("菜单「收起详情」真的收起（标题栏不再有 ▴ 按钮）",
          mid[0] != before[0] and not mid[1] and mid[2] is False,
          f"详情 {before[0]}→{mid[0]}，标志={mid[2]}，菜单项={acts_fold[:4]}")
    check("菜单「展开详情」回到原状", after == before, f"{after}（应等于 {before}）")
    check("标题栏里已经没有 ▴ 折叠按钮",
          "▴" not in [b.text() for b in bar.findChildren(QPushButton)],
          f"实际按钮：{[b.text() for b in bar.findChildren(QPushButton)]}")

    # ---- 7. 「日志」双击清空（防误触） ----
    bar.refresh_log_button()
    check("「日志」显示占用大小", "1.2 KB" in bar.btn_log.text(),
          f"按钮文字 {bar.btn_log.text()!r}")
    QTest.mouseClick(bar.btn_log, Qt.LeftButton)
    check("第 1 次点击只提示、不清空",
          os.path.getsize(H.AUDIT_PATH) == 1234 and "再点一次" in bar.hint.text(),
          f"文件仍 {os.path.getsize(H.AUDIT_PATH)} B；提示 {bar.hint.text()[:24]!r}…")
    QTest.mouseClick(bar.btn_log, Qt.LeftButton)
    check("4 秒内第 2 次点击真的清空",
          os.path.getsize(H.AUDIT_PATH) == 0 and "已清空" in bar.hint.text(),
          f"文件 {os.path.getsize(H.AUDIT_PATH)} B；文字 {bar.btn_log.text()!r}")
    with open(H.AUDIT_PATH, "wb") as f:
        f.write(b"y" * 999)
    bar._log_clear_armed = time.time() - 10          # 假装第一次点击已过去很久
    QTest.mouseClick(bar.btn_log, Qt.LeftButton)
    check("超时后重新只提示（防误触没被破坏）",
          os.path.getsize(H.AUDIT_PATH) == 999 and "再点一次" in bar.hint.text(),
          f"文件仍 {os.path.getsize(H.AUDIT_PATH)} B")

    # ---- 8. 「填群昵称」 —— 真填一次，看写没写进去 ----
    check("未填身份时按钮可见", bar.btn_me.isVisible(), bar.btn_me.toolTip())
    typed: dict = {}

    def accept_dlg():
        dlg = next((w for w in QApplication.topLevelWidgets()
                    if isinstance(w, H.NicknameDialog) and w.isVisible()), None)
        if dlg is None:
            typed["err"] = "对话框没弹出来"
            return
        QTest.keyClicks(dlg.edit, "YeYu")
        QTest.keyClicks(dlg.duty, "QA")
        dlg.kind_box.setCurrentIndex(dlg.kind_box.findText("客户群"))   # 新增的群性质
        typed["title"] = dlg.windowTitle()
        typed["input"] = dlg.edit.text()
        dlg.accept()

    QTimer.singleShot(500, accept_dlg)
    QTest.mouseClick(bar.btn_me, Qt.LeftButton)       # exec() 阻塞到对话框被接受
    rec = bar.store.get("自检群")
    check("点按钮弹框、输入（含群性质）能落库",
          rec.get("me") == "YeYu" and rec.get("duty") == "QA" and rec.get("kind") == "客户群",
          f"标题 {typed.get('title')!r}；输入 {typed.get('input')!r}；"
          f"落库 {rec.get('me')!r}/{rec.get('duty')!r}/{rec.get('kind')!r}")
    check("落库后面板立刻用上新身份、按钮自动隐藏",
          bar.viewer.get("群昵称") == "YeYu" and not bar.btn_me.isVisible()
          and bar.last_sig is None,
          f"viewer={bar.viewer} / 按钮可见={bar.btn_me.isVisible()} / "
          f"last_sig={bar.last_sig}（None=强制重新分诊）")

    # ---- 9. 点左列某个人 → 右栏出他的判断（群/私聊两套标签） ----
    bar.btn_pin.setChecked(True)                      # 免得右栏跟着窗口跳
    d = fake_detail("小满")
    bar.results = {"小满": d}
    bar._rebuild_rows([d], 1)
    app.processEvents()
    row = bar.rows.get("小满")
    QTest.mouseClick(row, Qt.LeftButton)
    # 右栏内容是加在 QVBoxLayout(self.r_body) 里的，QLabel 挂在 r_head 的父控件下；
    # 从 layout 上 findChildren 永远是空（layout 不是 widget）。
    texts = [x.text() for x in bar.r_head.parentWidget().findChildren(QLabel)]
    check("点左列某人 → 右栏显示他的判断",
          bar.selected == "小满" and "小满" in bar.r_head.text()
          and bar.r_body.count() > 0,
          f"标题 {bar.r_head.text()!r}，右栏 {bar.r_body.count()} 个字段")
    check("群聊标签正确（角色/意图/是否在点我）",
          any(t == "角色" for t in texts) and any(t == "是否在点我" for t in texts),
          f"含角色={'角色' in texts} 含是否在点我={'是否在点我' in texts}")

    dd = dict(d, nickname="云海车险顾问", scene="dm", role="", intent="",
              she_needs="explanation", true_intent="seek_explanation",
              danger=1.0, danger_word="安全", literal=0.55)
    # 真机上这一步是 on_scan 看到私聊标题时做的（这里显式走一遍同样的路径）
    bar.topic = "单聊"
    bar.selected = None
    bar._apply_scene_labels()
    bar.results["云海车险顾问"] = dd
    bar._rebuild_rows([d, dd], 2)
    app.processEvents()
    QTest.mouseClick(bar.rows["云海车险顾问"], Qt.LeftButton)
    # _clear_right 用的是 deleteLater：不等它落地，findChildren 还会看到上一份的旧标签
    QApplication.sendPostedEvents(None, QEvent.DeferredDelete)
    app.processEvents()
    texts = [x.text() for x in bar.r_head.parentWidget().findChildren(QLabel)]
    check("私聊换成私聊标签（真实意图/危险等级）",
          any(t == "真实意图" for t in texts)
          and any(t.startswith("危险等级") for t in texts)
          and not any(t == "是否在点我" for t in texts),
          f"真实意图={'真实意图' in texts} 危险等级={[t for t in texts if t.startswith('危险等级')]}")
    # 真机踩到过的显示 bug：面板宽度被钉在微信宽度上，右栏的值整列被裁到面板外，
    # 用户只看得到"真实意图/危险等级"这些标签、看不到值。这里做回归。
    vp = bar.scroll.viewport().width()
    need = bar.body_host.minimumSizeHint().width()
    clipped = [x.text()[:16] for x in bar.body_host.findChildren(QLabel)
               if x.text() and x.isVisible()
               and x.mapTo(bar.scroll.viewport(), QPoint(x.width(), 0)).x() > vp]
    # 还要逐标签比**它自己的容器**：真机出现过"视口够宽、但值被格子容器切掉"的情况
    # （`在试探你在不`、`要个具体安排` 贴着边断掉）—— 只比视口抓不到这种裁剪。
    clipped_cell = [x.text()[:16] for x in bar.right_panel.findChildren(QLabel)
                    if x.text() and x.isVisible() and x.parentWidget() is not None
                    and x.x() + x.width() > x.parentWidget().width() + 1]
    check("右栏的值不会被裁掉（视口 ≥ 内容需要，且不超出各自容器）",
          vp >= need and not clipped and not clipped_cell,
          f"视口 {vp} / 内容需要 {need} / 超出视口 {clipped} / 超出容器 {clipped_cell}")
    check("私聊措辞换掉了「群里的人」",
          "对方" in bar.l_head.text() and "群里" not in bar.l_head.text(),
          f"{bar.l_head.text()!r}")

    # ---- 10. 右键菜单：退出还在、能真关掉 ----
    # 真机上"退出"之后进程就结束了，不会再有轮询把面板显示回来；
    # 测试进程里必须手动停掉这个 600ms 跟随定时器，否则它会在 close() 之后
    # 把窗口重新显示出来 —— 实测这条断言偶发失败就是这个原因。
    bar.t_pos.stop()
    acts = menu_probe(bar, trigger_last=True)
    check("右键菜单弹出且项数齐全", len(acts) == 7, f"{acts}")   # 含「设置…」（F-22）
    check("展开态菜单里有「收成小球」", "收成小球" in acts, f"{acts}")
    check("菜单里退出仍是最后一项、没有多余的「关闭」",
          bool(acts) and acts[-1] == "退出" and "关闭" not in acts,
          f"最后一项 {(acts or ['?'])[-1]!r}")
    ok_close = False
    for _ in range(25):               # 关闭是在菜单的嵌套循环里完成的，给它一点时间落地
        QApplication.processEvents()
        time.sleep(0.04)
        if not bar.isVisible():
            ok_close = True
            break
    check("菜单「退出」真的能关掉面板", ok_close, f"isVisible={bar.isVisible()}")

    # ---- 11. Esc（代码层通路）+ 焦点事实 ----
    bar2 = build_bar(fake)
    QTest.keyClick(bar2, Qt.Key_Escape)
    check("Esc 在代码层仍能关掉面板", not bar2.isVisible(),
          f"isVisible={bar2.isVisible()}")
    no_focus = bool(bar2.windowFlags() & Qt.WindowDoesNotAcceptFocus)
    info(f"面板 windowFlags 含 WindowDoesNotAcceptFocus={no_focus}"
         f"（=面板不抢焦点，真实键盘的 Esc 落不到面板上；"
         f"退出实际靠右键菜单 → 退出）")
    info(f"应用当前激活窗口：{QApplication.activeWindow()!r}")

    # ---- 12. 气泡两态（小球） ----
    # 小球是 QLabel：它不接收鼠标事件，真实点击由系统冒泡给顶层窗口（HudBar）。
    # 合成事件不会冒泡，所以这里把点击/拖动直接打在窗口上、坐标取球的中心 ——
    # 走的仍然是 HudBar.mousePress/Move/Release 那条产品路径。
    teardown(bar)                     # 前两节用完就收，别再让它俩的定时器掺和进来
    teardown(bar2)
    if os.path.exists(prefs_path):    # "没有存档"这个前提要真的成立，不能靠运气
        os.remove(prefs_path)
    bar3 = build_bar(fake)
    info(f"小节 12 开工前的存档内容："
         f"{open(prefs_path, encoding='utf-8').read() if os.path.exists(prefs_path) else '(文件还不存在)'}")
    check("没有存档时默认是整块面板",
          not bar3.ball_mode and bar3.card.isVisible() and not bar3.ball.isVisible(),
          f"ball_mode={bar3.ball_mode}")

    bar3.results = {f"人{i}": {"state": "alert" if i < 2 else "silent"}
                    for i in range(3)}
    bar3._ball_bad = None        # 扫描线程的打桩 grab 会抛错 → 可能已把球标红；
    bar3._refresh_ball()         # 这里只验"结论 → 颜色"这一段映射，先把它清掉
    qss_alert, tip_alert = bar3.ball.styleSheet(), bar3.ball.toolTip()
    ring_alert = bar3._ball_ring
    bar3.results = {"人A": {"state": "silent"}}
    bar3._ball_bad = None
    bar3._refresh_ball()
    qss_silent, tip_silent = bar3.ball.styleSheet(), bar3.ball.toolTip()
    ring_silent = bar3._ball_ring
    check("2 条 alert → 外环橙红、提示写明条数",
          ring_alert == "#c2410c" and "2 条需要你" in tip_alert,
          f"环色={ring_alert} {tip_alert!r}")
    check("没有需要你 → 外环灰",
          ring_silent == "#b0b0b0" and "没有需要你" in tip_silent,
          f"环色={ring_silent} {tip_silent!r}")
    check("球上不出现任何数字（用户要求：只用颜色）",
          not any(ch.isdigit() for ch in bar3.ball.text()),
          f"球的文字={bar3.ball.text()!r}")
    bar3._ball_bad = "扫描不到微信：被遮挡"
    bar3._refresh_ball()
    ring_bad, tip_bad = bar3._ball_ring, bar3.ball.toolTip()
    check("扫描出问题 → 外环变红且说明原因",
          ring_bad == "#dc2626" and "扫描不到微信" in tip_bad,
          f"环色={ring_bad} {tip_bad!r}")
    bar3._ball_bad = None
    bar3._refresh_ball()

    # 标题栏的「◍」已按用户要求删掉（2026-09-24）→ 改走右键菜单，顺带把菜单入口也测了
    menu_probe(bar3, trigger="收成小球")
    for _ in range(4):                       # 让 singleShot / 布局重算跑完
        app.processEvents()
        time.sleep(0.05)
    check("右键菜单「收成小球」（窗口只剩 52x52）",
          bar3.ball_mode and not bar3.card.isVisible() and bar3.ball.isVisible()
          and (bar3.width(), bar3.height()) == (H.BALL, H.BALL),
          f"{bar3.width()}x{bar3.height()} 卡片可见={bar3.card.isVisible()} "
          f"球可见={bar3.ball.isVisible()} 窗口可见={bar3.isVisible()} "
          f"最小化={bar3.isMinimized()} 球位=({bar3.x()},{bar3.y()})")

    # 「exec 真能把菜单显示出来」这一步 _build_context_menu() 覆盖不到：发一个真的
    # 右键事件，等菜单出现（3 秒期限）再关掉。offscreen 平台下 QMenu.exec() 的嵌套
    # 循环不返回 → 跳过（自检默认跑 offscreen，这条路留给真机 / HUD_SELFCHECK_VISIBLE=1）
    if os.environ.get("QT_QPA_PLATFORM") == "offscreen":
        info("跳过「真弹一次菜单」：offscreen 下 QMenu.exec() 不返回，由真机覆盖")
    else:
        shown: list = []

        def look():
            # **必须用定时器**：菜单一弹出来，`exec()` 就把当前线程带进嵌套循环，
            # 我自己的 while 循环根本没机会跑（第一版就是这么写的，结果 3 秒里
            # 一次都没看到菜单 → 假失败）。定时器在嵌套循环里照样触发。
            if shown:
                return
            pop = QApplication.activePopupWidget()
            if not isinstance(pop, QMenu):
                pop = next((w for w in QApplication.topLevelWidgets()
                            if isinstance(w, QMenu) and w.isVisible()), None)
            if pop is not None:
                shown.append([a.text() for a in pop.actions() if not a.isSeparator()])
                pop.close()

        t_menu = QTimer()
        t_menu.setInterval(80)
        t_menu.timeout.connect(look)
        t_menu.start()
        QApplication.postEvent(bar3, QContextMenuEvent(
            QContextMenuEvent.Mouse, QPoint(20, 20),
            bar3.mapToGlobal(QPoint(20, 20))))
        t0 = time.time()
        while time.time() - t0 < 4 and not shown:
            app.processEvents()
            time.sleep(0.02)
            if time.time() - t0 > 3:          # 兜底：把可能还开着的菜单关掉，别吊死
                for w in QApplication.topLevelWidgets():
                    if isinstance(w, QMenu) and w.isVisible():
                        w.close()
        t_menu.stop()
        check("右键菜单真的能弹出来（exec 通路，菜单项也抄了一份）",
              bool(shown) and shown[0][-1:] == ["退出"],
              f"3 秒内看到的菜单：{shown[0] if shown else '一个都没看到'}")

    c = bar3.ball.geometry().center()
    QTest.mouseClick(bar3, Qt.LeftButton, pos=c)
    check("点小球 → 展开回面板",
          not bar3.ball_mode and bar3.card.isVisible() and not bar3.ball.isVisible(),
          f"ball_mode={bar3.ball_mode}")
    check("展开后宽度重新按（微信宽 ∩ 内容需要）算", bar3.width() >= 700,
          f"宽 {bar3.width()}")

    # 拖动：按下 → 移动 30px → 松手。位移 >4px 算拖动，只挪位置、不展开
    bar3.set_ball(True)
    before = (bar3.x(), bar3.y())
    c = bar3.ball.geometry().center()
    QTest.mousePress(bar3, Qt.LeftButton, pos=c)
    QApplication.sendEvent(bar3, QMouseEvent(
        QEvent.Type.MouseMove, QPointF(c + QPoint(30, 16)),
        QPointF(bar3.mapToGlobal(c + QPoint(30, 16))),
        Qt.MouseButton.NoButton, Qt.MouseButton.LeftButton,
        Qt.KeyboardModifier.NoModifier))
    QTest.mouseRelease(bar3, Qt.LeftButton, pos=c + QPoint(30, 16))
    check("拖小球只挪位置、不展开", bar3.ball_mode and (bar3.x(), bar3.y()) != before,
          f"{before} → ({bar3.x()},{bar3.y()})")
    saved = json.load(open(prefs_path, encoding="utf-8"))
    check("小球位置已落进存档（重启后还在）",
          saved.get("ball_xy") == [bar3.x(), bar3.y()],
          f"存档 ball_xy={saved.get('ball_xy')}")

    # 收起态的右键菜单：不该出现"固定位置/收起详情"，退出仍在最后
    a2 = menu_probe(bar3, trigger_last=False)
    check("收起态菜单=展开面板/填昵称/清空缓存/退出",
          a2[:1] == ["展开面板"] and a2[-1:] == ["退出"]
          and not any(t in ("固定位置", "收起详情", "收成小球") for t in a2), f"{a2}")

    # ---- 13. 形态与"叫醒"：重启后保持、另一个实例启动能展开小球 ----
    bar3.set_ball(True)
    bar4 = build_bar(fake)
    check("重启后保持上次形态（直接是小球）",
          bar4.ball_mode and (bar4.width(), bar4.height()) == (H.BALL, H.BALL),
          f"ball_mode={bar4.ball_mode} {bar4.width()}x{bar4.height()}")

    real_prefs(prefs_path).set("expand_at", time.time())   # 假装又启动了一次面板
    bar4._expand_checked = 0.0
    bar4.follow()
    check("重复启动 → 小球自动展开（不再没反应）",
          not bar4.ball_mode and bar4.card.isVisible(), f"ball_mode={bar4.ball_mode}")

    # ---- 14. 回归：私聊那一轮的 skim 是 {"needs_me": None, ...} ----
    # 真机踩到过：`sk.get("needs_me", 0)` 在"键存在但值为 null"时返回 None，
    # `round(None*100)` 抛 TypeError → on_triage 中途崩，整轮结果不渲染
    # （面板看着像"提交了分诊却什么都没变"）。这里按真机形状复现一次。
    bar5 = build_bar(fake)
    bar5.group, bar5.topic = "云海车险顾问", "单聊"
    dm_detail = fake_detail("云海车险顾问")
    dm_detail.update({"scene": "dm", "role": "", "intent": "",
                      "she_needs": "explanation", "true_intent": "seek_explanation",
                      "danger": 1.0, "danger_word": "安全", "literal": 0.55})
    payload = {"skim": {"needs_me": None, "risk_any": None, "ranked": ["云海车险顾问"]},
               "details": [dm_detail], "skipped": [], "all_speakers": ["云海车险顾问"],
               "gate_reason": "私聊：直接细判（粗筛无意义）",
               "calls_hour": 1, "cost_today": 0.0001, "total_records": 1}
    err = None
    try:
        bar5.on_triage(payload)
    except Exception as e:                       # noqa: BLE001 — 要的就是"别抛"
        err = f"{type(e).__name__}: {e}"
    check("私聊一轮（skim 为 null）不再崩", err is None, err or "无异常")
    check("私聊一轮照样把人和进度渲染出来",
          "云海车险顾问" in bar5.results and len(bar5.rows) == 1
          and "完成" in bar5.status.text() and "需我" not in bar5.status.text(),
          f"rows={list(bar5.rows)} 进度={bar5.status.text()!r}")
    check("私聊不摆「需我 0%」的假徽标",
          bar5.b_state.text() == "私聊 · 直接细判", f"{bar5.b_state.text()!r}")

    for b in (bar3, bar4, bar5):
        teardown(b)

    # ---- 15. 场景属性：关系 / 群性质 / 缓存失效 / 旧存档兼容 ----
    bar6 = build_bar(fake)
    bar6.group, bar6.topic = "云海车险顾问", "单聊"
    bar6.viewer = bar6.build_viewer()
    bar6.refresh_me_button()          # on_scan 里就是这两句连着的
    check("私聊未填关系时：提示在、按钮高亮、按钮文案是「填关系」",
          bar6.missing_input() == "关系" and bar6.nudge.isVisible()
          and bar6.btn_me.isVisible() and bar6.btn_me.text() == "填关系"
          and bar6.nudge.text().startswith("· 点「填关系」"),
          f"缺={bar6.missing_input()!r} 提示={bar6.nudge.text()!r} 按钮={bar6.btn_me.text()!r}")

    dm_rec: dict = {}

    def fill_dm():
        dlg = next((w for w in QApplication.topLevelWidgets()
                    if isinstance(w, H.NicknameDialog) and w.isVisible()), None)
        if dlg is None:
            dm_rec["err"] = "对话框没弹出来"
            return
        dm_rec["has_nick_field"] = dlg.edit is not None       # 私聊不该有"群昵称"输入框
        dlg.rel_box.setCurrentIndex(dlg.rel_box.findText("客户"))   # 下拉选「客户」
        QTest.keyClicks(dlg.identity, "presales")
        dlg.accept()

    QTimer.singleShot(500, fill_dm)
    QTest.mouseClick(bar6.btn_me, Qt.LeftButton)
    key_dm = "云海车险顾问" + H.DM_KEY_SUFFIX
    rec_dm = bar6.store.get(key_dm)
    check("私聊对话框只有 关系/身份，落库到「｜单聊」这条键",
          dm_rec.get("has_nick_field") is False and rec_dm.get("rel") == "客户"
          and rec_dm.get("identity") == "presales",
          f"无群昵称输入框={dm_rec.get('has_nick_field') is False}；落库键 {key_dm!r} → "
          f"rel={rec_dm.get('rel')!r} identity={rec_dm.get('identity')!r}")
    check("私聊 viewer 用 关系/身份（不是 群昵称/职责），未填提示换成了判据",
          bar6.viewer.get("关系") == "客户" and bar6.viewer.get("身份") == "presales"
          and "群昵称" not in bar6.viewer and bar6.missing_input() == ""
          and "填关系" not in bar6.nudge.text() and "判据" in bar6.nudge.text(),
          f"viewer={bar6.viewer} 缺={bar6.missing_input()!r} 状态行={bar6.nudge.text()!r}")

    # 「其他…」手填
    def fill_other():
        dlg = next((w for w in QApplication.topLevelWidgets()
                    if isinstance(w, H.NicknameDialog) and w.isVisible()), None)
        if dlg is None:
            return
        dlg.rel_box.setCurrentIndex(dlg.rel_box.findText("其他…"))
        QTest.keyClicks(dlg.other, "roommate")
        dlg.accept()

    QTimer.singleShot(500, fill_other)
    QTest.mouseClick(bar6.btn_me, Qt.LeftButton)
    check("下拉选「其他…」能手填并落库",
          bar6.store.get(key_dm).get("rel") == "roommate"
          and bar6.viewer.get("关系") == "roommate",
          f"rel={bar6.store.get(key_dm).get('rel')!r}")

    # 群性质要真的进 state（不是只落盘）
    st = PE._state("自检群", "测试主题", {"群昵称": "YeYu", "职责": "QA", "群性质": "客户群"},
                   "张三", ["在吗"], [])
    check("群性质进 state（群.性质）", st["群"]["性质"] == "客户群", f"{st['群']}")
    st_dm = PE._state_dm("云海车险顾问", {"关系": "客户", "身份": "presales"},
                         "云海车险顾问", ["再降一点"], [])
    check("私聊 state 用的是 对话.关系 / 我.身份",
          st_dm["对话"]["关系"] == "客户" and st_dm["我"]["身份"] == "presales",
          f"{st_dm['对话']} {st_dm['我']}")

    # 缓存键必须含身份 —— 否则"填了关系"仍会拿回填之前的结论（真机踩过）
    prof = PE.SpeakerProfile()
    prof.seen[prof.key_of("张三", ["在吗"], {"关系": "客户"})] = {"worth": "should_reply"}
    hit_same = prof.get("张三", ["在吗"], {"关系": "客户"}) is not None
    hit_changed = prof.get("张三", ["在吗"], {"关系": "陌生"}) is None
    check("改身份后不再命中旧缓存（填了立刻生效）", hit_same and hit_changed,
          f"同身份命中={hit_same} 改身份后命中={not hit_changed}")
    # 缓存键还必须含 场景 / 群 / 题集版本（§6.6 复核）：跨群同昵称同消息
    # （两边都没填身份时指纹相同）会互相串结论；群聊与私聊共用一只 profile，
    # 不隔开会把群口径的结论安到私聊头上；题集改版后旧结论也必须作废。
    hit_other_group = prof.get("张三", ["在吗"], {"关系": "客户"}, group="别的群") is None
    hit_other_scene = prof.get("张三", ["在吗"], {"关系": "客户"}, scene="dm") is None
    check("换群 / 换场景不再串结论（键含 群+场景+题集版本）",
          hit_other_group and hit_other_scene,
          f"同群同场景命中={hit_same} 换群命中={not hit_other_group} "
          f"换场景命中={not hit_other_scene}")
    src = inspect.getsource(PE.analyse_person)
    check("analyse_person 真的把 viewer/场景/群/版本传进了缓存键",
          "profile.get(nickname, msgs, viewer" in src
          and "key_of(nickname, msgs, viewer" in src
          and "scene=" in src and "group=" in src and "qver=" in src,
          "源码里取/存两处都带 viewer + 场景维度")

    # 全局默认关系（沿用同类实现的做法）：没填的私聊也能有判据
    bar6.store.set_default_relationship("同事")
    bar6.group, bar6.topic = "某个没填过的联系人", "单聊"
    bar6.viewer = bar6.build_viewer()
    bar6.refresh_me_button()
    check("全局默认关系兜底（没填的私聊也用得上）",
          bar6.viewer.get("关系") == "同事" and bar6.missing_input() == "",
          f"viewer={bar6.viewer}")
    bar6.store.set_default_relationship(None)

    # 旧存档（只有 me/duty/known）要照读，不能因为多了新键就炸
    legacy = os.path.join(TMP, "legacy_meta.json")
    with open(legacy, "w", encoding="utf-8") as f:
        json.dump({"林可": {"me": "老王", "duty": "答疑", "known": True}}, f,
                  ensure_ascii=False)
    old_store = H.GroupStore(legacy)
    lrec = old_store.get("林可")
    check("旧存档向后兼容（老群昵称照读、新键补空）",
          lrec.get("me") == "老王" and lrec.get("duty") == "答疑"
          and lrec.get("kind") is None and lrec.get("rel") is None,
          f"me={lrec.get('me')!r} kind={lrec.get('kind')!r} rel={lrec.get('rel')!r}")
    teardown(bar6)

    # ---- 16. 私聊结果构造（离线，不打网络）----
    # 为什么非要有这一节：今天改缓存键时把 viewer 漏传给了 _dm_result，真机上私聊直接
    # NameError（API 明明 200）—— 而当时 53 项自检全绿，因为"私聊结果构造"那条路
    # 一直没被测过。用假客户端把整条路走一遍，不再依赖真机才暴露。
    def _dm_answers():
        return {
            "worth_my_reply": {"choice": "should_reply", "confidence": 0.8,
                               "probabilities": {"should_reply": 0.8,
                                                 "may_reply": 0.15,
                                                 "insufficient_info": 0.05}},
            "sufficiency": {"score": 2.3, "legend": {"2": "一般"}},
            "true_intent": {"choice": "seek_explanation", "confidence": 0.7},
            "she_needs": {"choice": "explanation", "confidence": 0.7},
            "danger_level": {"score": 2.0},
            "literal_question": {"noul": 0.5},
            "tension_resolved": {"noul": 0.6},
            "risk_ad": {"noul": 0.05}, "risk_fraud": {"noul": 0.05},
            "risk_conflict": {"noul": 0.05}, "risk_illegal": {"noul": 0.05},
        }

    class FakeClient:
        total_input_tokens = 0

        def __init__(self):
            self.seen_state = None
            self.answers = _dm_answers()

        def system_one(self, state, questions, meta=None):
            self.seen_state = state
            return {"answers": self.answers, "model": "stub",
                    "usage": {"input_tokens": 1, "output_tokens": 1}}

    dm_viewer = {"关系": "客户", "身份": "presales"}
    c1, prof = FakeClient(), PE.SpeakerProfile()
    r_dm = PE.analyse_person(c1, group="云海车险顾问", topic="单聊", viewer=dm_viewer,
                             nickname="云海车险顾问", msgs=["再降一点"],
                             context=[("对方", "再降一点")], profile=prof, scene="dm")
    check("私聊结果构造不炸（离线走完整条路）",
          r_dm.get("scene") == "dm" and r_dm.get("true_intent") == "seek_explanation"
          and r_dm.get("danger") == 2.0,
          f"scene={r_dm.get('scene')} 意图={r_dm.get('true_intent')} "
          f"危险={r_dm.get('danger')} worth={r_dm.get('worth')}")
    check("私聊 state 里带上了关系（判据真的到了模型手上）",
          c1.seen_state.get("对话", {}).get("关系") == "客户"
          and c1.seen_state.get("我", {}).get("身份") == "presales",
          f"{c1.seen_state.get('对话')} {c1.seen_state.get('我')}")

    c2 = FakeClient()
    r2 = PE.analyse_person(c2, group="云海车险顾问", topic="单聊", viewer=dm_viewer,
                           nickname="云海车险顾问", msgs=["再降一点"],
                           context=[("对方", "再降一点")], profile=prof, scene="dm")
    check("同身份同消息命中缓存（不重复计费）",
          r2.get("cached") is True and c2.seen_state is None,
          f"cached={r2.get('cached')} 是否又调了 API={c2.seen_state is not None}")

    c3 = FakeClient()
    r3 = PE.analyse_person(c3, group="云海车险顾问", topic="单聊",
                           viewer={"关系": "陌生", "身份": "presales"},
                           nickname="云海车险顾问", msgs=["再降一点"],
                           context=[("对方", "再降一点")], profile=prof, scene="dm")
    check("换了关系后重新判（这就是「填了立刻生效」）",
          r3.get("cached") is False and c3.seen_state is not None,
          f"cached={r3.get('cached')} 是否重新调了 API={c3.seen_state is not None}")

    # 群聊那条路也走一遍（群.性质 必须进 state）
    c4 = FakeClient()
    c4.answers = {
        "role": {"choice": "peer", "confidence": 0.6,
                 "probabilities": {"peer": 0.6, "asker": 0.3, "seller": 0.1}},
        "intent_now": {"choice": "chat", "confidence": 0.6,
                       "probabilities": {"chat": 0.6, "selling": 0.2, "asking": 0.2}},
        "addressed_to_me": {"noul": 0.4},
        "worth_my_reply": {"choice": "may_reply", "confidence": 0.5,
                           "probabilities": {"may_reply": 0.5, "should_reply": 0.3,
                                             "insufficient_info": 0.2}},
        "sufficiency": {"score": 2.0, "legend": {}},
        "risk_ad": {"noul": 0.05}, "risk_fraud": {"noul": 0.05},
        "risk_conflict": {"noul": 0.05}, "risk_illegal": {"noul": 0.05},
    }
    r_g = PE.analyse_person(c4, group="林可", topic="群聊",
                            viewer={"群昵称": "老王", "职责": "答疑", "群性质": "兴趣群"},
                            nickname="张三", msgs=["在吗"], context=[],
                            profile=PE.SpeakerProfile(), scene="group")
    check("群聊结果构造也走通，且 群.性质 进了 state",
          c4.seen_state.get("群", {}).get("性质") == "兴趣群"
          and r_g.get("state") in ("alert", "todo", "silent"),
          f"群={c4.seen_state.get('群')} state={r_g.get('state')}")
    teardown(bar6)

    # ---- 17. UI 批 1：面板高度跟内容 / 消息行数 / 媒体残片 ----
    # 用户原话"看都看不全"：面板高度卡在开面板那一刻（QScrollArea 的 sizeHint 不随内容长），
    # 右栏被塞进滚动条后面。这里断言"视口 ≥ 内容需要的高度"。
    bar7 = build_bar(fake)
    bar7.group, bar7.topic = "云海车险顾问", "单聊"
    bar7.viewer = {"关系": "客户", "身份": "presales"}
    bar7.refresh_me_button()
    d7 = fake_detail("云海车险顾问")
    d7.update({"scene": "dm", "role": "", "intent": "", "she_needs": "explanation",
               "true_intent": "seek_explanation", "danger": 1.0, "danger_word": "安全",
               "literal": 0.6,
               "msgs": ["」13″」", "再降一点", "没有经销商返利", "前提是新车对吧",
                        "第五条不该显示", "第六条也不该显示"]})
    bar7.results = {"云海车险顾问": d7}
    bar7._rebuild_rows([d7], 1)
    bar7.on_person_click("云海车险顾问")
    for _ in range(6):
        app.processEvents()
        time.sleep(0.05)
    bar7.follow()
    app.processEvents()
    need_h = bar7.body_host.minimumSizeHint().height()
    vp_h = bar7.scroll.viewport().height()
    check("面板高度容得下详情（视口 ≥ 内容需要 → 一屏看全）",
          vp_h >= need_h - 2, f"面板高 {bar7.height()}：视口 {vp_h} / 内容需要 {need_h}")
    check("标题里的媒体残片不再显示（」13″」 这类）",
          "13″" not in bar7.r_head.text() and "」" not in bar7.r_head.text(),
          f"{bar7.r_head.text()!r}")
    check("「他最近的消息」最多渲染 MSG_ROWS 行",
          getattr(bar7, "_last_msg_rows", 99) <= H.MSG_ROWS,
          f"渲染 {getattr(bar7, '_last_msg_rows', '?')} 行（上限 {H.MSG_ROWS}）")
    # 用户要求：最近的消息**显示全文**（原来省略到 150px、全文只在 tooltip 里）
    msgs_shown = [x.text() for x in bar7.right_panel.findChildren(QLabel)
                  if x.text().startswith("\u3000") and len(x.text()) > 3]
    src_msgs = [m for m in d7["msgs"][:H.MSG_ROWS]]
    check("最近的消息显示全文（不省略、与源文本逐条一致）",
          bool(msgs_shown) and all("…" not in t for t in msgs_shown)
          and [t.lstrip("\u3000") for t in msgs_shown] == src_msgs,
          f"渲染={[t[:26] for t in msgs_shown]} 源={[m[:26] for m in src_msgs]}")
    teardown(bar7)

    # ---- 18. 自遮挡：面板压住微信消息区时不许扫（真机踩过：存档里出现垃圾会话名）----
    # 真机上 group_meta.json 出现过叫 `、、品群里的人：点一个人着判断，，判新注府2` 的会话 ——
    # 那是 OCR 把**面板自己的字**读成了会话标题（面板置顶，截屏读到了自己）。
    from wechat_triage_hud import wechat_capture as WC
    from wechat_triage_hud import wechat_window as WW
    bar8 = build_bar(fake)
    bar8.t_pos.stop()
    sc = _REAL_SCANNER(bar8)         # 真实实现（不 start，不起线程）
    sc.ignore_hwnd = 999999          # 假句柄；面板矩形由下面的桩决定
    wx = (1240, 21, 1897, 924)       # 真机微信尺寸
    saved_rect = fake.win.rect
    fake.win.rect = wx                    # 让桩微信报真机尺寸
    cases = [((400, 21, 1240, 600), False, "贴微信左侧不重叠"),
             ((1300, 200, 1800, 600), True, "压在消息区上"),
             ((1300, 780, 1800, 920), False, "只压底部输入区"),
             # 小球（52px）压在消息区上约 1%：它是一张圆图、没有文字，不该拦住整个扫描
             # （真机：用户把微信拖到右边，球正好落在消息区里 → 面板一直报"压住了消息区"）
             ((1700, 300, 1752, 352), False, "小球压住一点（52px，约 1%）")]
    got = []
    for rect, expect, tag in cases:
        # 注意：真实 Scanner 调的是 hud 模块里的 ww（测试里已被换成桩），
        # 所以要打在桩上，不能打在真模块 wechat_window 上（打错了方法根本不会被执行）
        H.ww.window_rect = lambda h, _r=rect: _r
        got.append((bool(sc._self_occluded(fake.win)), expect, tag))
    H.ww.window_rect = lambda h: None     # 还原默认行为
    fake.win.rect = saved_rect
    check("自遮挡判定正确（压消息区才挡，压输入区不挡）",
          all(g == e for g, e, _ in got),
          "；".join(f"{t}→{'挡' if g else '不挡'}{'✅' if g == e else '❌'}"
                    for g, e, t in got))
    teardown(bar8)

    # ---- 19. UI 批 3：面板可调整大小（用户尺寸 / 紧凑模式 / 恢复自适应）----
    bar9 = build_bar(fake)
    bar9.group, bar9.topic = "云海车险顾问", "单聊"
    bar9.viewer = {"关系": "客户", "身份": "presales"}
    bar9.refresh_me_button()
    d9 = fake_detail("x")
    d9.update({"scene": "dm", "role": "", "intent": "", "she_needs": "explanation",
               "true_intent": "seek_explanation", "danger": 1.0, "danger_word": "安全",
               "literal": 0.5, "msgs": ["再降一点"]})
    bar9.results = {"x": d9}
    bar9._rebuild_rows([d9], 1)
    bar9.on_person_click("x")
    for _ in range(4):
        app.processEvents()
        time.sleep(0.05)
    bar9.follow()
    app.processEvents()
    right9 = bar9.r_head.parentWidget()
    auto_w, auto_h = bar9.width(), bar9.height()

    bar9.prefs.set("panel_size", [900, 500])
    bar9.follow()
    app.processEvents()
    time.sleep(0.15)
    vp9 = bar9.scroll.viewport()
    need9 = bar9.body_host.minimumSizeHint()
    # 期望宽度按**实际能给的**算：产品会把用户尺寸夹进屏幕（真机 1920 屏上是 900，
    # 自检默认跑 offscreen，虚拟屏只有 800 宽 → 夹到 776）。断言"要么给到 900、
    # 要么给到屏幕允许的最大值"，既守住了"手动尺寸生效"，也不会因为屏小而假红。
    allow_w = min(900, QApplication.primaryScreen().availableGeometry().width() - 24)
    check("手动尺寸生效，且仍不裁内容（视口 ≥ 内容需要）",
          (bar9.width(), bar9.height()) == (allow_w, 500)
          and vp9.width() >= need9.width() and vp9.height() >= need9.height(),
          f"{bar9.width()}x{bar9.height()}（应为 {allow_w}x500，屏幕允许 {allow_w}）；"
          f"视口 {vp9.width()}x{vp9.height()} vs 内容 {need9.width()}x{need9.height()}")

    # 拖窄 → 紧凑模式：**只在群聊 + 没有选中的人时生效**
    # （有选中的人还隐右栏就会"点人看不到详情"；私聊更不该隐，见下一条）
    bar9.topic = "群聊"
    bar9.selected = None
    bar9._clear_right()
    bar9._apply_scene_labels()
    bar9.prefs.set("panel_size", [430, 300])
    bar9.follow()
    app.processEvents()
    time.sleep(0.15)
    left_min = bar9.left_panel.minimumSizeHint().width() + 46
    check("群聊拖窄 → 进紧凑模式（隐右栏），宽度被左列下限兜住",
          not right9.isVisible() and bar9.width() >= left_min,
          f"右栏可见={right9.isVisible()} 宽 {bar9.width()}（左列下限 {left_min}）")

    # 私聊拖同样窄：**不许**进紧凑（右栏就是全部内容，隐了就一片空白）
    bar9.topic = "单聊"
    bar9._apply_scene_labels()
    bar9.follow()
    app.processEvents()
    time.sleep(0.15)
    check("私聊拖窄不进紧凑模式（右栏必须留着）",
          right9.isVisible() and bar9.width() >= H.PANEL_MIN_W,
          f"右栏可见={right9.isVisible()} 宽 {bar9.width()}")

    # 比较要同场景：auto_w 是群聊（左栏在）时量的，先切回群聊再比，
    # 否则会拿"私聊无左栏"的宽度去比"群聊有左栏"的宽度，报假失败
    bar9.topic = "群聊"
    bar9._apply_scene_labels()
    bar9.follow()
    app.processEvents()
    time.sleep(0.15)
    bar9.reset_size()
    bar9.follow()
    app.processEvents()
    time.sleep(0.15)
    check("恢复自适应：尺寸回到内容驱动、右栏放回来、存档清掉 panel_size",
          right9.isVisible() and bar9.width() == auto_w
          and "panel_size" not in json.load(open(prefs_path, encoding="utf-8")),
          f"{bar9.width()}x{bar9.height()}（自适应基准 {auto_w}x{auto_h}，同为左栏可见形态）"
          f"右栏={right9.isVisible()}")
    teardown(bar9)

    # ---- 20. 私聊去掉左栏 / 群聊保留 / 窄栏回退一行一对 ----
    # 用户原话："私聊不需要左边栏吧" —— 私聊左栏只为放一行"某某"，却占 3/5 宽度，
    # 把右栏挤到 248px，值被格子容器切掉（截图里 `在试探你在不` 就是这样断的）。
    def _clip_in_container(b) -> list:
        out = []
        for x in b.right_panel.findChildren(QLabel):
            if not x.text() or not x.isVisible() or x.parentWidget() is None:
                continue
            if x.x() + x.width() > x.parentWidget().width() + 1:
                out.append(x.text()[:14])
        return out

    def _row_of_pairs_len(fake_b) -> int:
        """右栏里"键值行"的最小宽度（判断是不是一行两格）。"""
        ws = [w.width() for w in fake_b.right_panel.findChildren(QWidget)
              if w.isVisible() and w.layout() is not None and w.parentWidget() is fake_b.right_panel]
        return max(ws) if ws else 0

    bar10 = build_bar(fake)
    d10 = fake_detail("文件传输助手")
    d10.update({"scene": "dm", "role": "", "intent": "", "she_needs": "want_arrangement",
                "true_intent": "confirm_you_care", "danger": 2.0, "danger_word": "安全",
                "literal": 0.3, "state": "todo", "worth": "insufficient_info",
                "sufficiency": 2.44, "risks": {"fraud": 0.18, "illegal": 0.16},
                "msgs": ["嗯嗯哼哼", "我想要能做这个效果的源文件", "从头到尾", "我们用来学习"]})
    bar10.group, bar10.topic = "林可", "单聊"
    bar10.viewer = {"关系": "客户", "身份": None}
    bar10.refresh_me_button()
    bar10._apply_scene_labels()
    bar10.results = {"文件传输助手": d10}
    bar10._rebuild_rows([d10], 1)
    bar10.on_triage({"skim": {"needs_me": None, "risk_any": None,
                              "ranked": ["文件传输助手"]},
                     "details": [d10], "skipped": [], "all_speakers": ["文件传输助手"],
                     "gate_reason": "私聊：直接细判", "calls_hour": 2,
                     "cost_today": 0.0003, "total_records": 20})
    for _ in range(5):
        app.processEvents()
        time.sleep(0.05)
    bar10.follow()
    app.processEvents()
    time.sleep(0.15)
    app.processEvents()
    dm_w = bar10.width()
    right_w = bar10.right_panel.width()
    check("私聊：左栏隐藏、右栏拿到全部宽度",
          not bar10.left_panel.isVisible() and right_w > 400,
          f"左栏可见={bar10.left_panel.isVisible()} 右栏宽={right_w}")
    check("私聊：无需点击就自动选中对方并出详情",
          bar10.selected == "文件传输助手" and bar10.r_body.count() > 0,
          f"selected={bar10.selected!r} 右栏字段数={bar10.r_body.count()}")
    check("私聊：值不被格子容器裁掉",
          dm_w >= 640 and not _clip_in_container(bar10),
          f"面板宽 {dm_w} / 超出容器 {_clip_in_container(bar10)}")

    # 群聊：左栏要回来；右栏窄 → 自动一行一对（值完整）
    bar10.group, bar10.topic = "林可", "群聊"
    bar10.selected = None
    bar10._apply_scene_labels()
    d11 = fake_detail("张三")
    d11.update({"scene": "group", "msgs": ["这个多少钱？", "有链接吗"]})
    bar10.results = {"张三": d11}
    bar10._rebuild_rows([d11], 1)
    bar10.on_person_click("张三")
    for _ in range(4):
        app.processEvents()
        time.sleep(0.05)
    bar10.follow()
    app.processEvents()
    time.sleep(0.15)
    app.processEvents()
    # 测量前把 deleteLater 的旧控件真正销毁：_clear_right 用的是 deleteLater，
    # findChildren 会连"已标记删除、尚未销毁"的上一轮标签一起返回，
    # 它们的几何还是旧宽度（实测误报过 640 宽标签"超出 248 的容器"）。
    QApplication.sendPostedEvents(None, QEvent.DeferredDelete)
    for _ in range(6):
        app.processEvents()
        time.sleep(0.05)
    pairs_two = bar10._pairs_two_columns()
    row_w = _row_of_pairs_len(bar10)
    check("群聊：左栏回来，且面板比私聊宽",
          bar10.left_panel.isVisible() and bar10.width() > dm_w,
          f"左栏可见={bar10.left_panel.isVisible()} 宽 {bar10.width()}（私聊时 {dm_w}）")
    over = _clip_in_container(bar10)
    worst = ""
    if over:
        cand = [(x.text()[:12], x.x(), x.width(), x.parentWidget().width())
                for x in bar10.right_panel.findChildren(QLabel)
                if x.text() and x.isVisible() and x.parentWidget() is not None
                and x.x() + x.width() > x.parentWidget().width() + 1]
        worst = f" | 详情：右栏 {bar10.right_panel.width()} 宽，越界标签 {cand[:3]}"
    check("右栏窄时自动改一行一对（值不再被容器裁掉）",
          (pairs_two or row_w <= 0 or row_w >= 240) and not over,
          f"两列={pairs_two} 行宽={row_w} 超出容器={over}{worst}")
    teardown(bar10)

    # ---- 21. 私聊：右栏要跟着每一轮重画（否则"判了但界面不动"）----
    # 真机踩过：给私聊加"自动选中"时，把"每轮重画右栏"写成了 elif → 选中的人没变时
    # 永远不重画，新结果进了 results、界面还挂在旧消息上。用户的原话是
    # "卡住了 / 内容变了但没识别 / 最近的消息根本没识别"。
    bar11 = build_bar(fake)
    bar11.group, bar11.topic = "某私聊", "单聊"
    bar11.viewer = {"关系": "客户", "身份": None}
    bar11.refresh_me_button()
    bar11._apply_scene_labels()

    def _dm_detail(msgs, sheet):
        d = fake_detail("对方")
        d.update({"scene": "dm", "role": "", "intent": "", "she_needs": sheet[1],
                  "true_intent": sheet[0], "danger": 1.0, "danger_word": "安全",
                  "literal": 0.5, "msgs": list(msgs)})
        return d

    payload = lambda d: {                       # noqa: E731
        "skim": {"needs_me": None, "risk_any": None, "ranked": [d["nickname"]]},
        "details": [d], "skipped": [], "all_speakers": [d["nickname"]],
        "gate_reason": "私聊：直接细判", "calls_hour": 1, "cost_today": 0.0,
        "total_records": 1}

    d_a = _dm_detail(["第一条", "第二条"], ("seek_explanation", "explanation"))
    bar11.results = {"对方": d_a}
    bar11._rebuild_rows([d_a], 1)
    bar11.on_triage(payload(d_a))
    for _ in range(4):
        app.processEvents()
        time.sleep(0.05)
    rows_a = [x.text() for x in bar11.right_panel.findChildren(QLabel)
              if x.text().startswith("　")]

    d_b = _dm_detail(["第三条", "第四条"], ("seek_explanation", "explanation"))
    bar11.results["对方"] = d_b                # 同一人、新消息（走 re-judge 那条路）
    bar11.on_triage(payload(d_b))
    for _ in range(4):
        app.processEvents()
        time.sleep(0.05)
    QApplication.sendPostedEvents(None, QEvent.DeferredDelete)
    rows_b = [x.text() for x in bar11.right_panel.findChildren(QLabel)
              if x.text().startswith("　")]
    check("私聊：同一人的新一轮结果会重画右栏（不再停在旧快照）",
          rows_a != rows_b and any("第三条" in t for t in rows_b),
          f"上一轮消息行 {rows_a} → 这一轮 {rows_b}（应含\"第三条\"）")
    check("右栏写清判断范围（最近 N 条 + 累积 M 条 + 整段对话 K 条，且不逐句）",
          any(t.startswith("依据：最近") and "按人整体判断" in t
              for t in [x.text() for x in bar11.right_panel.findChildren(QLabel)]),
          f"{[x.text() for x in bar11.right_panel.findChildren(QLabel) if x.text().startswith('依据')]}")
    teardown(bar11)

    # ---- 22. F-21 暂停 / 免打扰 ----
    # 要求："立即停捕获与网络"。所以暂停后：扫描器 paused=True（不截屏/不 OCR）、
    # 排队中的分诊被丢掉、状态行写明怎么恢复；再切回来要恢复采集。
    bar12 = build_bar(fake)
    bar12.t_pos.stop()
    bar12.scanner.paused = False
    check("默认不是暂停态", not bar12.paused and not bar12.scanner.paused,
          f"面板 paused={bar12.paused} 扫描器 paused={bar12.scanner.paused}")

    bar12.toggle_pause(True)                      # 等价于按 Ctrl+Alt+H / 点托盘菜单
    check("暂停后：扫描器停采集、状态行写明恢复方式、小球外环变灰",
          bar12.paused and bar12.scanner.paused
          and H.HOTKEY_TEXT in bar12.status.text()
          and bar12._ball_ring == "#8a8a8a",
          f"paused={bar12.paused} 扫描器={bar12.scanner.paused} "
          f"状态={bar12.status.text()!r}")

    bar12.toggle_pause(False)
    check("再切回来：恢复采集、状态行回到扫描中",
          (not bar12.paused) and (not bar12.scanner.paused)
          and "扫描" in bar12.status.text(),
          f"paused={bar12.paused} 状态={bar12.status.text()!r}")

    # 暂停期间扫描循环**不能**被看门狗当成"卡死"重启（它仍在发 tick）
    bar12.scanner.last_tick_at = time.time() - 999
    bar12.toggle_pause(True)
    before_stop = bar12.scanner._stop
    bar12._check_scanner()
    check("暂停期间看门狗不重启扫描器（暂停 ≠ 卡死）",
          bar12.scanner._stop == before_stop,
          f"scanner._stop={bar12.scanner._stop}（True 说明被误重启过）")
    bar12.toggle_pause(False)
    teardown(bar12)

    # ---- 23. F-22 设置：频率 / 成本 / 阈值 / API Key ----
    # 全程用**临时** settings.json 与临时 .env，绝不碰你本机那两份真文件。
    from wechat_triage_hud import settings as SET
    saved_path, saved_data = SET.SETTINGS.path, dict(SET.SETTINGS.data)
    saved_env = H.ENV_PATH
    SET.SETTINGS.path = os.path.join(TMP, "settings.json")
    SET.SETTINGS.reset()
    H.ENV_PATH = os.path.join(TMP, ".env")
    with open(H.ENV_PATH, "w", encoding="utf-8") as f:
        f.write("# 测试用\njevkey=apikey_" + "x" * 96 + "\n")
    try:
        check("设置默认值齐全且合法",
              all(SET.SETTINGS.validate(k, SET.SETTINGS.get(k))[0] for k in SET.RANGES),
              f"{SET.SETTINGS.data}")
        ok1, why1 = SET.SETTINGS.set("per_hour", 99999)
        ok2, why2 = SET.SETTINGS.set("daily_usd", "abc")
        check("越界与非数字都被拒绝，且给出原因（不静默钳值）",
              (not ok1 and "1000" in why1) and (not ok2 and "不是数字" in why2),
              f"{why1} / {why2}")

        SET.SETTINGS.set("per_hour", 7)
        SET.SETTINGS.set("thr_noul_hit", 0.9)
        ok3, why3 = SET.SETTINGS.save()
        reloaded = SET.Settings(SET.SETTINGS.path)
        check("保存后重新读盘还在（改完不必重启）",
              ok3 and reloaded.get("per_hour") == 7 and abs(reloaded.get("thr_noul_hit") - 0.9) < 1e-9,
              f"per_hour={reloaded.get('per_hour')} thr_noul_hit={reloaded.get('thr_noul_hit')}")

        # 阈值真的影响门控结论（不是只写进文件）
        from wechat_triage_hud import qset as Q
        ans = {"worth_my_reply": {"choice": "may_reply", "confidence": 0.5,
                                  "probabilities": {"may_reply": 0.5, "should_reply": 0.3,
                                                    "insufficient_info": 0.2}},
               "role": {"choice": "peer", "confidence": 0.6,
                        "probabilities": {"peer": 0.6, "asker": 0.3, "seller": 0.1}},
               "intent_now": {"choice": "chat", "confidence": 0.6,
                              "probabilities": {"chat": 0.6, "selling": 0.2, "asking": 0.2}},
               "addressed_to_me": {"noul": 0.4}, "sufficiency": {"score": 2.0, "legend": {}},
               "risk_ad": {"noul": 0.5}, "risk_fraud": {"noul": 0.05},
               "risk_conflict": {"noul": 0.05}, "risk_illegal": {"noul": 0.05}}
        SET.SETTINGS.set("thr_noul_hit", 0.35)
        SET.sync_thresholds()
        base = Q.gate(ans)
        SET.SETTINGS.set("thr_noul_hit", 0.9)
        SET.sync_thresholds()
        high = Q.gate(ans)
        check("阈值改完门控结论真的变（Noul 门槛 0.35→0.9，风险信号不再计入）",
              base[1] != high[1] and any("风险" in r for r in base[1])
              and not any("风险" in r for r in high[1]),
              f"0.35 → {base[1]}；0.9 → {high[1]}")

        # 节流上限跟着设置走
        from wechat_triage_hud.triage import Throttle
        SET.SETTINGS.set("per_hour", 1)
        SET.SETTINGS.set("daily_usd", 100.0)
        th = Throttle()
        a1 = th.allow()
        th.record(0.0)
        a2 = th.allow()
        SET.SETTINGS.set("per_hour", 60)
        a3 = th.allow()
        check("每小时上限跟着设置生效（设 1 时第 2 次就被拒，改回 60 就放行）",
              a1[0] and (not a2[0]) and a3[0],
              f"第1次={a1[0]} 第2次={a2[0]}({a2[1]}) 改回后={a3[0]}")

        # 对话框：保存走一遍（Key 格式不对 → 设置照存、Key 不动）
        dlg = H.SettingsDialog()
        # 每个设置项都要有「推荐值 + 作用」的小字，且推荐值 == 默认值
        # （用户 2026-09-24："这些值的解释和推荐值要在合适的地方进行说明"）
        miss = [k for k, lb in dlg.hints.items() if not lb.text().strip()]
        rec_bad = [k for k in dlg.hints
                   if f"推荐 {SET.RANGES[k][2]}" not in dlg.hints[k].text()]
        check("设置项每条都有「推荐 X · 作用」小字，且推荐值 == 默认值",
              not miss and not rec_bad,
              f"缺说明={miss} 推荐值不对={rec_bad}；"
              f"示例 {dlg.hints.get('min_interval_s').text()[:60]!r}")
        tips_ok = []
        for k in dlg.hints:
            tip = dlg.boxes[k].toolTip()
            lo, hi, _d = SET.RANGES[k]
            tips_ok.append(("实测" in tip or "推荐" in tip) and f"{lo} ~ {hi}" in tip)
        check("悬停详解里有为什么与取值范围（可填区间不再是猜的）",
              all(tips_ok), f"不满足的项数={tips_ok.count(False)}/{len(tips_ok)}")
        dlg.boxes["per_hour"].setValue(7)
        check("改了值 → 小字变成「已改为 7（推荐 60）」并标黄",
              "已改为 7" in dlg.hints["per_hour"].text()
              and "推荐 60" in dlg.hints["per_hour"].text()
              and "#c2410c" in dlg.hints["per_hour"].styleSheet(),
              f"{dlg.hints['per_hour'].text()[:70]!r}")
        dlg._on_reset()
        check("「恢复默认」把小字也复原成「推荐 …」",
              dlg.hints["per_hour"].text().startswith("推荐 60")
              and "#c2410c" not in dlg.hints["per_hour"].styleSheet(),
              f"{dlg.hints['per_hour'].text()[:50]!r}")
        dlg.boxes["per_hour"].setValue(12)
        dlg.boxes["thr_strong"].setValue(0.8)
        dlg.key_edit.setText("junk-not-a-key")
        dlg._on_ok()
        env_txt = io.open(H.ENV_PATH, encoding="utf-8").read()
        after = SET.Settings(SET.SETTINGS.path)
        check("对话框保存：设置写盘生效，格式不对的 Key 不改 .env",
              after.get("per_hour") == 12 and abs(after.get("thr_strong") - 0.8) < 1e-9
              and "junk-not-a-key" not in env_txt and "apikey_" in env_txt,
              f"per_hour={after.get('per_hour')} thr_strong={after.get('thr_strong')} "
              f".env 未被动={'junk-not-a-key' not in env_txt}")
        dlg.deleteLater()
    finally:
        SET.SETTINGS.path, SET.SETTINGS.data = saved_path, saved_data
        SET.sync_thresholds()
        H.ENV_PATH = saved_env

    # ---- 24. 紧凑模式下点人：必须看得到详情 ----
    # 用户报"点人进去看不到详情"：面板被拖到 492 宽 → 群聊下自动进紧凑（隐右栏），
    # 点人时 _show_detail 照常执行、但右栏是隐藏的 → 看着像"点了没反应"。
    bar13 = build_bar(fake)
    bar13.t_pos.stop()
    bar13.group, bar13.topic = "某群(188)", "群聊"
    bar13.viewer = {"群昵称": "我", "职责": "答疑", "群性质": "兴趣群"}
    bar13.refresh_me_button()
    bar13._apply_scene_labels()
    d13a, d13b = fake_detail("阿泽"), fake_detail("老周")
    for d13 in (d13a, d13b):
        d13.update({"scene": "group", "msgs": ["这个多少钱？", "有链接吗"]})
    bar13.results = {"阿泽": d13a, "老周": d13b}
    bar13._rebuild_rows([d13a, d13b], 2)
    for _ in range(4):
        app.processEvents()
        time.sleep(0.05)
    bar13.prefs.set("panel_size", [492, 471])      # 用户拖窄的尺寸
    bar13.follow()
    app.processEvents()
    time.sleep(0.15)
    app.processEvents()
    check("窄面板（群聊）自动进紧凑：右栏隐藏，且提示写明怎么展开",
          not bar13.right_panel.isVisible() and "点一个人会自动展开" in bar13.hint.text(),
          f"右栏可见={bar13.right_panel.isVisible()} 提示={bar13.hint.text()!r}")

    bar13.on_person_click("阿泽")
    for _ in range(6):
        app.processEvents()
        time.sleep(0.05)
    bar13.follow()
    app.processEvents()
    time.sleep(0.15)
    app.processEvents()
    check("紧凑下点人：右栏自动放出来、面板变宽、详情有内容",
          bar13.right_panel.isVisible() and bar13.r_body.count() > 0
          and "阿泽" in bar13.r_head.text() and bar13.width() > 505,
          f"右栏可见={bar13.right_panel.isVisible()} 宽={bar13.width()} "
          f"标题={bar13.r_head.text()!r} 字段数={bar13.r_body.count()}")

    bar13.selected = None
    bar13._clear_right()
    bar13.follow()
    app.processEvents()
    time.sleep(0.15)
    app.processEvents()
    check("清空选择后又回到紧凑（用户拖窄的尺寸没被丢掉）",
          not bar13.right_panel.isVisible()
          and bar13.prefs.get("panel_size") == [492, 471],
          f"右栏可见={bar13.right_panel.isVisible()} 存档={bar13.prefs.get('panel_size')}")
    teardown(bar13)

    # ---- 25. 按人累积历史（用户："个人消息是否会累积，这样是不是可以更好的判断"）----
    # 以前只看"屏幕上还看得见的"：消息一滚就没了 → "这人前后说过什么"模型永远看不到。
    from wechat_triage_hud import settings as SET2
    saved2 = dict(SET2.SETTINGS.data)
    from wechat_triage_hud import person_engine as PE2
    bar14 = build_bar(fake)
    bar14.t_pos.stop()
    SET2.SETTINGS.set("history_msgs", 12)

    bar14.remember([("阿泽", ["第一句", "第二句"])])
    bar14.remember([("阿泽", ["第二句", "第三句"])])          # 第二句重复上屏，不该存两遍
    bar14.remember([("阿泽", ["第四句"])])                    # 第一句已滚出屏幕，但要在历史里
    hist = bar14.history_of("阿泽")
    check("跨屏累积：滚出屏幕的消息仍留在历史里，且不重复",
          hist == ["第一句", "第二句", "第三句", "第四句"],
          f"历史={hist}")

    class _C:
        total_input_tokens = 0

        def __init__(self):
            self.seen_state = None

        def system_one(self, state, questions, meta=None):
            self.seen_state = state
            return {"answers": {
                "role": {"choice": "peer", "confidence": 0.6,
                         "probabilities": {"peer": 0.6, "asker": 0.3, "seller": 0.1}},
                "intent_now": {"choice": "chat", "confidence": 0.6,
                               "probabilities": {"chat": 0.6, "selling": 0.2, "asking": 0.2}},
                "addressed_to_me": {"noul": 0.4},
                "worth_my_reply": {"choice": "may_reply", "confidence": 0.5,
                                   "probabilities": {"may_reply": 0.5, "should_reply": 0.3,
                                                     "insufficient_info": 0.2}},
                "sufficiency": {"score": 2.0, "legend": {}},
                "risk_ad": {"noul": 0.05}, "risk_fraud": {"noul": 0.05},
                "risk_conflict": {"noul": 0.05}, "risk_illegal": {"noul": 0.05}},
                "model": "stub", "usage": {"input_tokens": 1}}

    c = _C()
    prof = PE2.SpeakerProfile()
    PE2.analyse_person(c, group="某群", topic="群聊", viewer={"群昵称": "我"},
                       nickname="阿泽", msgs=["第三句", "第四句"], context=[],
                       profile=prof, scene="group", history=bar14.history_of("阿泽"))
    earlier = ((c.seen_state.get("这个人") or {})
               .get("更早的消息（本机累积，供参考）") or [])
    got = [x.get("内容") for x in earlier]
    check("累积历史进了 state，且去掉屏幕上已有的（不重复送）",
          got == ["第一句", "第二句"], f"更早的消息={got}")

    c2 = _C()
    PE2.analyse_person(c2, group="某群", topic="群聊", viewer={"群昵称": "我"},
                       nickname="阿泽", msgs=["第三句", "第四句"], context=[],
                       profile=prof, scene="group",
                       history=bar14.history_of("阿泽") + ["第五句"])
    check("历史变多 → 缓存键跟着变（重新判，不吃旧结论）",
          c2.seen_state is not None, f"是否重新调了 API={c2.seen_state is not None}")

    # 面板必须**看得见**累积（用户："我看了阿泽的消息并没有累积"——其实在送，只是没显示）
    d14 = fake_detail("阿泽")
    d14.update({"scene": "group", "msgs": ["第三句", "第四句"],
                "earlier": ["第一句", "第二句"]})
    bar14.results = {"阿泽": d14}
    bar14._rebuild_rows([d14], 1)
    bar14.on_person_click("阿泽")
    for _ in range(4):
        app.processEvents()
        time.sleep(0.05)
    labels = [x.text() for x in bar14.right_panel.findChildren(QLabel)]
    check("右栏显示「更早的消息（本机累积）」及其内容",
          any(t.startswith("更早的消息（本机累积") for t in labels)
          and any(t.strip("　") == "第一句" for t in labels),
          f"含累积段={any('更早的消息' in t for t in labels)}；示例={[t for t in labels if '第一句' in t][:1]}")

    # 跨重启保留（落盘 → 新实例载入）：内存-only 会让每次重启都白攒
    import json as _json
    hist_path = os.path.join(TMP, "speaker_history.json")
    saved_hist_path = H.HIST_PATH
    H.HIST_PATH = hist_path
    try:
        bar14.hist.clear()
        bar14.hist_all.clear()
        bar14.group, bar14.topic = "某群(188)", "群聊"
        bar14._use_conv_hist()
        bar14.remember([("阿泽", ["跨重启一", "跨重启二"])])
        bar14._flush_hist()
        bar15 = build_bar(fake)
        bar15.t_pos.stop()
        bar15.group, bar15.topic = "某群(188)", "群聊"
        bar15._use_conv_hist()
        check("累积历史跨重启保留（落盘 → 新实例载入）",
              os.path.exists(hist_path)
              and bar15.history_of("阿泽") == ["跨重启一", "跨重启二"],
              f"文件在={os.path.exists(hist_path)} 新实例读到={bar15.history_of('阿泽')}")
        teardown(bar15)
    finally:
        H.HIST_PATH = saved_hist_path

    SET2.SETTINGS.set("history_msgs", 0)
    bar14.hist.clear()
    bar14.remember([("阿泽", ["不该被记住"])])
    check("设置成 0 就不再累积（可关）",
          bar14.history_of("阿泽") == [], f"历史={bar14.history_of('阿泽')}")
    for k, v in saved2.items():
        SET2.SETTINGS.set(k, v)
    teardown(bar14)

    # ---- 26. 小球用图片（用户提供 assets/ball.png）+ 状态色外环 ----
    from wechat_triage_hud import paths as P26
    bar16 = build_bar(fake)
    bar16.t_pos.stop()
    check("球体资源在仓库里（wechat_triage_hud/assets/ball.png）",
          os.path.exists(P26.BALL_IMG) and os.path.getsize(P26.BALL_IMG) > 1000,
          f"{P26.BALL_IMG}（{os.path.getsize(P26.BALL_IMG) if os.path.exists(P26.BALL_IMG) else 0} 字节）")
    bar16.results = {"A": {"state": "alert"}}
    bar16._ball_bad = None
    bar16._refresh_ball()
    pm_alert, ring_alert = bar16.ball.pixmap(), bar16._ball_ring
    bar16.results = {"A": {"state": "silent"}}
    bar16._refresh_ball()
    pm_silent, ring_silent = bar16.ball.pixmap(), bar16._ball_ring
    check("小球贴上图片（52×52 圆形裁切），球上不再写字",
          (not pm_alert.isNull()) and pm_alert.width() == H.BALL
          and bar16.ball.text() == "",
          f"贴图 {'有' if not pm_alert.isNull() else '无'} {pm_alert.width()}x{pm_alert.height()}"
          f" 文字={bar16.ball.text()!r}")
    check("状态语义没丢：外环颜色随状态变（alert 橙红 / 静默灰）",
          ring_alert != ring_silent and ring_alert.startswith("#") and ring_silent.startswith("#"),
          f"alert={ring_alert} silent={ring_silent}")
    # 图片缺失要回落成纯色球，别因为少个资源就没了外观
    saved_img = H.BALL_IMG
    H.BALL_IMG = os.path.join(TMP, "没有这张图.png")
    bar16._ball_qss = None
    bar16._refresh_ball()
    check("图片缺失时回落纯色球（不因缺资源破相）",
          "background" in bar16.ball.styleSheet() and bar16.ball.pixmap().isNull(),
          f"样式含 background={'background' in bar16.ball.styleSheet()}")
    H.BALL_IMG = saved_img
    teardown(bar16)

    # ---- 27. 点一条消息 → 只判这一条（复用私聊口径）----
    # 用户要求："点击每条信息也能进行判断更好"。这里验四件事：
    #   ① 消息行可点、显示全文（不是省略号）② 送出去的 job 里**只有这一条**
    #   ③ 结果回来后右栏切成"消息判断" ④ 「← 返回按人判断」能回去
    # 另加一条离线走位：_state_msg + 结果构造不炸（真机上这条路的炸点踩过一次了）
    bar17 = build_bar(fake)
    bar17.group, bar17.topic = "自检群", "自检主题"
    bar17.viewer = {"群昵称": "YeYu", "职责": "QA", "关系": "客户", "群性质": "客户群"}
    d17 = fake_detail("阿泽")
    d17["msgs"] = ["帮我看看这个排版怎么调", "周三能出吗"]
    d17["earlier"] = ["上周那份稿子"]
    d17["hist_total"] = 1
    bar17.results = {"阿泽": d17}
    bar17._rebuild_rows([d17], 1)
    bar17.on_person_click("阿泽")
    for _ in range(4):
        app.processEvents()
        time.sleep(0.05)

    rows = [w for w in bar17.findChildren(H.MsgRow)]
    texts = [w.text for w in rows]
    check("右栏每条消息都是一行、都能点（最近 + 更早）",
          len(rows) == 3 and texts[:2] == d17["msgs"] and texts[2] == d17["earlier"][0],
          f"{len(rows)} 行：{texts}")
    check("消息行显示全文（换行折行，不省略）",
          all(not w.lab.text().endswith("…") for w in rows)
          and rows[0].lab.text().strip() == d17["msgs"][0],
          f"首行 {rows[0].lab.text()!r}" if rows else "没有消息行")

    target = d17["msgs"][1]
    QTest.mouseClick(rows[1], Qt.LeftButton)
    for _ in range(3):
        app.processEvents()
        time.sleep(0.05)
    job = getattr(bar17.worker, "last_msg_job", None)
    job_desc = "None（没送出去）" if job is None else f"{(job[0], job[2], job[3])!r}"
    check("点第 2 条 → 只把这一条送出去（昵称/群/正文都对得上）",
          isinstance(job, tuple) and len(job) == 5 and job[2] == "阿泽" and job[3] == target
          and job[0] == "自检群",
          f"job={job_desc}")
    check("右栏立刻切成「消息判断 · 阿泽」，并给出「← 返回按人判断」",
          bar17.r_head.text().startswith("消息判断 · 阿泽")
          and any(isinstance(w, QPushButton) and w.text() == "← 返回按人判断"
                  for w in right_widgets(bar17)),
          f"标题={bar17.r_head.text()!r} 右栏={[type(w).__name__ for w in right_widgets(bar17)]}")

    # 结果回来：字段与私聊那套一致 → 标签该照常出现
    r17 = dict(d17)
    r17.update({"msgs": [target], "scene": "msg",
                "true_intent": "seek_explanation", "she_needs": "explanation",
                "danger": 2.0, "danger_word": "安全", "literal": 0.2,
                "cached": False, "state": "alert"})
    bar17.on_msg_done(r17)
    for _ in range(3):
        app.processEvents()
        time.sleep(0.05)
    body17 = " ".join(w.text() for w in right_widgets(bar17) if isinstance(w, QLabel))
    check("单条结论按私聊标签渲染（真实意图/对方需要/危险等级/有潜台词/该不该回）",
          all(k in body17 for k in ("真实意图", "对方需要", "危险等级", "有潜台词", "该不该回"))
          and "explanation" not in body17,      # 该显示中文标签，不是枚举名
          f"右栏标签：{body17[:120]}")
    check("结论页写明只发这一条（隐私口径不糊）",
          "只发这一条" in bar17.foot.text(), f"footer={bar17.foot.text()!r}")

    # 晚到的结果：已经点去别的消息 → 必须丢掉（别把 A 的结论画在 B 上）
    bar17.msg_result = None
    QTest.mouseClick(rows[0], Qt.LeftButton)
    app.processEvents()
    bar17.on_msg_done(r17)                        # r17 的 msgs 还是第 2 条
    check("点走之后晚到的结论被丢弃（不画到别条上）",
          bar17.msg_result is None and bar17.msg_sel[1] == d17["msgs"][0],
          f"msg_result={bar17.msg_result is not None} msg_sel={bar17.msg_sel}")

    back = [w for w in right_widgets(bar17)
            if isinstance(w, QPushButton) and w.text() == "← 返回按人判断"]
    if back:
        QTest.mouseClick(back[0], Qt.LeftButton)
    for _ in range(3):
        app.processEvents()
        time.sleep(0.05)
    check("「← 返回按人判断」回到按人详情",
          bar17.msg_sel is None and bar17.r_head.text().startswith("判断 · 阿泽"),
          f"msg_sel={bar17.msg_sel} 标题={bar17.r_head.text()!r} 返回按钮={len(back)} 个")

    # 没人被选中时点消息：不该发请求（右栏本来也不会显示消息行，这里防的是极端时序）
    bar17.selected = None
    before_job = bar17.worker.last_msg_job
    bar17.on_msg_click("野消息")
    check("没有选中的人 → 点消息不发请求（不烧钱）",
          bar17.worker.last_msg_job is before_job and bar17.msg_sel is None,
          f"msg_sel={bar17.msg_sel} 又发了一次={bar17.worker.last_msg_job is not before_job}")
    teardown(bar17)

    # 离线走一遍 _state_msg + 结果构造（scene="msg" 单独一套缓存键）
    class FakeClientMsg(FakeClient):
        def system_one(self, state, questions, meta=None):
            self.seen_state = state
            self.seen_meta = meta
            return {"answers": _dm_answers(), "model": "stub",
                    "usage": {"input_tokens": 1, "output_tokens": 1}}

    c27, prof27 = FakeClientMsg(), PE.SpeakerProfile()
    ctx27 = [("阿泽", "帮我看看这个排版怎么调"), ("我", "我看看")]
    r27 = PE.analyse_msg(c27, group="自检群", viewer=bar17.viewer, speaker="阿泽",
                         msg="周三能出吗", context=ctx27, profile=prof27)
    st27 = c27.seen_state or {}
    check("单条消息 state：判断对象写明、群身份映射进「我.身份/对话.关系」",
          st27.get("这条消息（要判断的就是它）", {}).get("内容") == "周三能出吗"
          and st27.get("对方", {}).get("最近消息") == [{"序号": 1, "内容": "周三能出吗"}]
          and "YeYu" in st27.get("我", {}).get("身份", "")
          and st27.get("对话", {}).get("关系") == "客户",
          f"{list(st27.keys())[:6]} / 我={st27.get('我')}")
    check("单条判断走的是私聊题面（meta.scene=msg，question 里有潜台词那套）",
          (c27.seen_meta or {}).get("scene") == "msg"
          and r27.get("scene") == "msg" and r27.get("true_intent") == "seek_explanation",
          f"meta={c27.seen_meta} scene={r27.get('scene')}")
    c27b = FakeClientMsg()
    r27b = PE.analyse_msg(c27b, group="自检群", viewer=bar17.viewer, speaker="阿泽",
                          msg="周三能出吗", context=ctx27, profile=prof27)
    check("同一条消息 + 同上下文 → 命中缓存（不重复花钱）",
          r27b.get("cached") is True and c27b.seen_state is None,
          f"cached={r27b.get('cached')} 是否又调了 API={c27b.seen_state is not None}")
    c27c = FakeClientMsg()
    r27c = PE.analyse_msg(c27c, group="自检群", viewer=bar17.viewer, speaker="阿泽",
                          msg="周三能出吗", context=[("阿泽", "另一个场景")],
                          profile=prof27)
    check("同一句话换了上下文 → 重新判（结论不互串）",
          r27c.get("cached") is False and c27c.seen_state is not None,
          f"cached={r27c.get('cached')}")

    # ---- 28. 用户三连报的修复：键值别离太远 / 左列要有抬头 / 切群别卡住 ----
    # 起因（2026-09-24，用户两张真机截图）：
    #   ① "值太远了"      —— 键后用 addStretch(1) 把值顶到格子最右，两列时每格 ~260px
    #   ② "不知道这值干什么用的" —— 左列五列宽度写死却没有任何列头
    #   ③ "切换群或个人的时候太慢了" —— 节流是**全局**的，切群沿用上一个群的 20s 窗口
    class FakeView:
        """够 on_scan 用的最小视图（真 OCR 那条路在别的节里验）。"""

        def __init__(self, title, people, is_group=True, marker="x"):
            self.title = title
            self.is_group = is_group
            self.error = None
            self.messages = [marker]           # 只要非空即可（on_scan 只看布尔）
            self.layout = None
            self._people = list(people)
            self._marker = marker

        def speakers(self):
            return list(self._people)

        def recent_messages(self, n=8):
            return [("我", f"上下文 {self._marker}")]

    def kv_gaps(b) -> list:
        """每个「键 + 值」单元格：值离键右边界多少像素。"""
        out = []
        for w in right_widgets(b):
            labs = w.findChildren(QLabel)
            ks = [c for c in labs if c.objectName() == "k"]
            vs = [c for c in labs if c.objectName() in ("v", "vh")]
            if len(ks) == 1 and len(vs) == 1:
                out.append((ks[0].text(), vs[0].text(),
                            vs[0].x() - (ks[0].x() + ks[0].width()), ks[0].width()))
        return out

    bar18 = build_bar(fake)
    bar18.t_pos.stop()
    bar18.group, bar18.topic = "青柠设计组项目群(39)", "群聊"
    bar18.viewer = {"群昵称": "YeYu"}
    d18 = fake_detail("小满")
    d18["msgs"] = ["这版图周三能出吗"]
    d18["scene"] = "group"
    bar18.results = {"小满": d18}
    bar18._rebuild_rows([d18], 1)
    bar18.on_person_click("小满")
    for _ in range(4):
        app.processEvents()
        time.sleep(0.05)

    gaps = kv_gaps(bar18)
    worst = max((g[2] for g in gaps), default=999)
    check("键值排版：值紧贴键（最大间隔 ≤16px），不再被顶到格子最右",
          bool(gaps) and worst <= 16,
          f"共 {len(gaps)} 格，最大间隔 {worst}px；示例 {gaps[:3]}")
    key_widths = {g[3] for g in gaps}          # 只看"键+值"单元格里的键（右栏还有个同名
    check("键值排版：键是定宽的（各格的值因此天然对齐）",   # 的"风险"标题，它不是单元格）
          key_widths == {H.KV_LABEL_W},
          f"键宽={key_widths}，应为 {{{H.KV_LABEL_W}}}")
    lab_over = []
    for w in right_widgets(bar18):
        for c in w.findChildren(QLabel):
            if c.objectName() in ("k", "v", "vh") and w.width() and                     c.x() + c.width() > w.width() + 2:
                lab_over.append((c.objectName(), c.text()[:12], c.x(), c.width(), w.width()))
    check("值仍不会被自己的容器裁掉（原有的回归断言保留）",
          not lab_over, f"越界的标签：{lab_over[:3]}")

    hdr = bar18._l_col_labels
    check("左列表头：四项文案 + 列宽与 PersonRow 各列一一对应",
          [lb.text() for lb in hdr] == ["昵称", "在群里的角色", "要不要回", "关注度"]
          and [lb.width() for lb in hdr] == [H.ROW_MARK_W + H.ROW_SPACING + H.ROW_NICK_W,
                                             H.ROW_ROLE_W, H.ROW_WORTH_W, H.ROW_ATTN_W]
          and bar18.l_risk_head.text() == "风险信号",
          f"列头={[(lb.text(), lb.width()) for lb in hdr]} 末列={bar18.l_risk_head.text()!r}")
    row_labs = {c.objectName(): c.width()
                for c in bar18.findChildren(H.PersonRow)[0].findChildren(QLabel)}
    check("表头列宽与真实行完全一致（这是对齐的前提）",
          row_labs.get("role") == hdr[1].width()
          and row_labs.get("worth") == hdr[2].width()
          and row_labs.get("attn") == hdr[3].width(),
          f"行={row_labs} 表头={[lb.width() for lb in hdr]}")
    # 用户 2026-09-24 截图："这个看不清" —— 窄面板里两枚风险徽标被挤出行外、半个背景被裁。
    # 现在：每行**最多一枚**徽标（最高的风险；没有风险才显示"信息不足"），文案完整不裁。
    rows18 = [
        dict(d18, nickname="小满", risks={"risk_ad": 0.9, "risk_fraud": 0.7},
             sufficiency=2.0),
        dict(d18, nickname="被挤出去的那种特别长的昵称测试", risks={"risk_fraud": 0.6},
             sufficiency=2.0),
        dict(d18, nickname="周工", risks={"risk_ad": 0.0}, sufficiency=0.8),
    ]
    bar18.results = {r["nickname"]: r for r in rows18}
    bar18._rebuild_rows(rows18, 3)
    for _ in range(4):
        app.processEvents()
        time.sleep(0.05)
    over, multi, zh, nick_tip = [], [], [], []
    for row in bar18.findChildren(H.PersonRow):
        chips = [c for c in row.findChildren(QLabel) if c.objectName() in ("risk", "lowsuff")]
        if len(chips) > 1:
            multi.append((row.key, len(chips)))
        for c in row.findChildren(QLabel):
            if c.width() and c.x() + c.width() > row.width():
                over.append((row.key, c.objectName(), c.text()[:8]))
        for c in chips:
            if c.objectName() == "risk" and not any("一" <= ch <= "鿿" for ch in c.text()):
                zh.append((row.key, c.text()))
            if c.toolTip() == "":
                zh.append((row.key, "徽标没有 tooltip"))
        nk = row.nick
        if nk.text().endswith("…"):
            nick_tip.append((row.key, nk.toolTip() != ""))
    check("人员行里没有任何控件越出行宽（徽标不再被挤出画面）",
          not over, f"越界：{over[:3]}")
    check("风险/信息徽标每行最多一枚，且是中文标签", not multi and not zh,
          f"多于一枚={multi} 非中文/无提示={zh}")
    check("超长昵称是省略号 + tooltip（不再硬截断到像缺字）",
          len(nick_tip) == 1 and nick_tip[0][1] is True,
          f"被省略的行={len(nick_tip)} 带 tooltip={nick_tip}")
    # 选中底色不能污染子控件（用户截图"红框里看不清"的根因：行的无选择器样式表
    # 把徽标的橙红底覆盖成选中蓝，白字压在浅蓝上）。这里取**像素**判定，别只看代码。
    rows_now = bar18.findChildren(H.PersonRow)
    if rows_now:
        rows_now[0].set_selected(True)
        for _ in range(4):
            app.processEvents()
            time.sleep(0.05)
    want_px = {"risk": "#c2410c", "lowsuff": "#fff1cc"}
    got_px = {}
    for row in rows_now:
        for c in row.findChildren(QLabel):
            if c.objectName() in want_px:
                px = c.grab().toImage().pixelColor(2, 2)
                got_px[c.objectName()] = (f"#{px.red():02x}{px.green():02x}{px.blue():02x}",
                                          "选中行" if row.selected else "未选中行")
    check("徽标底色不被选中高亮污染（取像素：橙红 #c2410c / 淡黄 #fff1cc）",
          bool(got_px) and all(got_px[k][0] == v for k, v in want_px.items() if k in got_px),
          f"实测 {got_px}；期望 {want_px}")

    bar18.results = {"小满": d18}
    bar18._rebuild_rows([d18], 1)

    # 信息充分度：分数越高越**不足**（0=完全够用…3=严重不足，图例见审计里模型回传的 legend）。
    # 只显示 "2.40 / 3" 会被读成"挺充分" —— 必须带等级文字（用户看不懂的一半原因在这）。
    from wechat_triage_hud import qset as Q18
    check("信息充分度显示成「等级（分数/3）」，方向不再反直觉",
          Q18.suff_level(0.4) == "完全够用" and Q18.suff_level(1.2) == "大致够"
          and Q18.suff_level(2.4) == "不太够" and Q18.suff_level(2.9) == "严重不足",
          f"0.4→{Q18.suff_level(0.4)} 1.2→{Q18.suff_level(1.2)} "
          f"2.4→{Q18.suff_level(2.4)} 2.9→{Q18.suff_level(2.9)}")
    bar18.results = {r["nickname"]: dict(r, sufficiency=2.4) for r in rows18}
    d24 = dict(d18, sufficiency=2.4, risks={"risk_ad": 0.0})
    bar18.results = {"小满": d24}
    bar18._rebuild_rows([d24], 1)
    bar18.on_person_click("小满")
    for _ in range(4):
        app.processEvents()
        time.sleep(0.05)
    body24 = " ".join(w.text() for w in right_widgets(bar18) if isinstance(w, QLabel))
    check("右栏那行确实写出了等级（不是光一个数字）",
          "不太够" in body24 and "2.40" in body24,
          f"右栏里含「不太够」={'不太够' in body24} 含「2.40」={'2.40' in body24}")

    # 风险默认显示 高/中/低（用户 2026-09-24："换成高中低，要看详细几率就点它变成数据"）。
    # 档位**锚在门控阈值上**：高=≥STRONG(0.75，单类够印证)、中=≥NOUL_HIT(0.35，门控算命中)、低=其余。
    d_risk = dict(d18, nickname="小满", sufficiency=2.4,
                  risks={"risk_ad": 0.9, "risk_fraud": 0.4, "risk_conflict": 0.15,
                         "risk_illegal": 0.02})
    bar18.results = {"小满": d_risk}
    bar18._rebuild_rows([d_risk], 1)
    bar18.msg_sel = None
    bar18.on_person_click("小满")
    for _ in range(4):
        app.processEvents()
        time.sleep(0.05)

    def risk_vals() -> list:
        out = []
        for w in right_widgets(bar18):
            for c in w.findChildren(QLabel):
                if c.toolTip().startswith(("广告拉人：", "诈骗嫌疑：", "引战攻击：", "违规内容：")):
                    out.append(c.text())
        return out

    check("风险默认显示 高/中/低（而不是把百分比怼给用户）",
          risk_vals() == ["高", "中", "低"] and bar18._risk_pct is False,
          f"显示={risk_vals()}（0.9→高 0.4→中 0.15→低；0.02 <8% 不列）")
    jobs_before = len(bar18.worker.jobs)
    titles = [w for w in right_widgets(bar18)
              if isinstance(w, H.ClickLabel) and w.text().startswith("风险")]
    check("「风险」标题是可点的（提示写着能切换）",
          bool(titles) and "点一下看具体概率" in titles[0].text(),
          f"标题={[t.text() for t in titles]}")
    if titles:
        QTest.mouseClick(titles[0], Qt.LeftButton)
    for _ in range(3):
        app.processEvents(); time.sleep(0.05)
    check("点标题 → 变成具体概率（且 8% 以下的仍然不列）",
          risk_vals() == ["90%", "40%", "15%"] and bar18._risk_pct is True,
          f"显示={risk_vals()}")
    vals = [w for w in right_widgets(bar18)
            if isinstance(w, H.ClickLabel) and w.text().endswith("%")]
    if vals:
        QTest.mouseClick(vals[0], Qt.LeftButton)          # 点"数值"那一格（用户说的“点它”）
    for _ in range(3):
        app.processEvents(); time.sleep(0.05)
    check("点数值那一格也能切回等级（两个入口都行）",
          risk_vals() == ["高", "中", "低"] and bar18._risk_pct is False,
          f"显示={risk_vals()}")
    check("切换显示**不重新判断、不花钱**（提交次数没变）",
          len(bar18.worker.jobs) == jobs_before,
          f"提交 {jobs_before} → {len(bar18.worker.jobs)} 次")
    # 档位跟着设置走（阈值是可调的，显示不能写死 0.5/0.75）
    saved_hit = qset.NOUL_HIT
    try:
        qset.NOUL_HIT = 0.95
        check("把「Noul 门槛」调高 → 档位跟着降，且高低档不会交叉",
              bar18._risk_level(0.4)[0] == "低" and bar18._risk_level(0.9)[0] == "高"
              and bar18._risk_level(0.8)[0] == "高",
              f"0.4→{bar18._risk_level(0.4)[0]} 0.9→{bar18._risk_level(0.9)[0]}"
              f"（门槛 0.95 / 最高打扰 0.75）")
    finally:
        qset.NOUL_HIT = saved_hit
    bar18._risk_pct = False
    bar18.results = {"小满": d18}
    bar18._rebuild_rows([d18], 1)

    # 打包（PyInstaller）后数据目录必须落在 **exe 旁边**，不能落在临时解包目录 ——
    # 否则 out/（审计日志/设置/累积历史）重启就没，用户放在 exe 旁边的 .env 也读不到。
    # 这个坑只有打包后才暴露，所以把路径逻辑抽成纯函数在这里钉死。
    from wechat_triage_hud.paths import resolve_project_dir as _rpd
    _exe_dir = _rpd("/tmp/_MEI123/wechat_triage_hud", True, "D:/dist/app.exe")
    _dev_dir = _rpd("G:/repo/wechat_triage_hud", False, None)
    check("打包后数据目录 = exe 所在目录（不是临时解包目录）",
          os.path.normpath(_exe_dir) == os.path.normpath("D:/dist")
          and os.path.normpath(_dev_dir) == os.path.normpath("G:/repo"),
          f"冻结时={_exe_dir!r} 开发时={_dev_dir!r}")

    # 用了但没导入的名字 = "只在异常分支才炸"的那类 bug。用户真机踩到过：
    # wechat_capture 里 sys.stderr 没导入 → OCR 一报错，**处理异常的那段自己又抛 NameError**，
    # 面板上写着"扫描失败：NameError: name 'sys' is not defined"，小球也跟着看不见。
    # 静态检查能直接抓出来，装了就每次都跑；没装（INFO）不挡验收。
    try:
        import io as _io
        import pyflakes.api as _pfa
        import pyflakes.reporter as _pfr
        from wechat_triage_hud import paths as _paths
        _buf = _io.StringIO()
        for _m in ("hud.py", "person_engine.py", "qset.py", "triage.py", "wechat_capture.py",
                   "wechat_window.py", "jev_engine.py", "jev_log.py", "settings.py", "paths.py"):
            _pfa.checkPath(os.path.join(_paths.PROJECT_DIR, "wechat_triage_hud", _m),
                           _pfr.Reporter(_buf, _buf))
        _bad = [ln for ln in _buf.getvalue().splitlines() if "undefined name" in ln]
        check("产品代码里没有「用了但没导入」的名字（pyflakes）", not _bad, f"{_bad[:3]}")
    except ImportError:
        info("跳过 pyflakes 静态检查（未安装 pyflakes；装了就会跑）")

    # 缺 API Key 不许"无声等待"（用户报"怎么卡住了"：面板停在"提交判断 · 1 人待筛"、
    # 日志 0 B，而原因是打包版旁边没有 .env → 判断线程初始化就失败、任务永远没人处理）
    _env_saved = H.ENV_PATH
    H.ENV_PATH = os.path.join(TMP, "no_such_env")
    os.environ.pop("JEVKEY", None)
    try:
        bar19 = build_bar(fake)
        for _ in range(6):
            app.processEvents()
            time.sleep(0.05)
        check("没配 Key 时面板立刻说明原因（不再无声地干等）",
              "API Key" in bar19._worker_dead and "API Key" in bar19.hint.text(),
              f"原因={bar19._worker_dead[:60]!r} 提示={bar19.hint.text()[:60]!r}")
        # 看门狗：线程已结束 + 有排队任务 = 必须说出来（这里把计时器拨到过去，确定性触发）
        bar19.worker._jobs = [(1,)]
        bar19.worker.isRunning = lambda: False     # 桩里没有这两个方法，看门狗会 try/except 跳过
        bar19.worker.isFinished = lambda: True
        bar19._worker_dead = ""
        bar19._worker_stuck_since = time.time() - 10
        bar19._check_worker()
        check("判断线程已死 + 有任务排队 → 看门狗把原因写到页脚（不再看着像在忙）",
              "判断不可用" in bar19.foot.text() and "API Key" in bar19.foot.text(),
              f"页脚={bar19.foot.text()[:70]!r}")
        teardown(bar19)
    finally:
        H.ENV_PATH = _env_saved

    bar18.topic = "单聊"                       # 私聊：第二列放的是危险等级文字
    bar18._apply_scene_labels()
    check("私聊的列头文案跟着换（危险等级 / 信息量）",
          hdr[1].text() == "危险等级" and bar18.l_risk_head.text() == "信息量",
          f"{[lb.text() for lb in hdr]} + {bar18.l_risk_head.text()!r}")
    check("私聊整列隐藏时，列头跟着一起隐",
          not bar18.left_panel.isVisible() and not bar18.l_cols.isVisible(),
          f"左列可见={bar18.left_panel.isVisible()} 列头可见={bar18.l_cols.isVisible()}")

    # —— 「关闭」= 收成小球（面板收起、球留着；不是"彻底隐藏"）——
    # 真机踩过：早先做成"彻底隐藏"→ 用户点完屏幕上什么都没有，反馈"小球看不见"。
    bar18.topic = "群聊"
    bar18._apply_scene_labels()
    bar18.set_ball(False, save=False)
    bar18.show()
    QTest.mouseClick(bar18.btn_hide, Qt.LeftButton)
    for _ in range(4):
        app.processEvents()
        time.sleep(0.05)
    check("「关闭」= 收成小球（52x52、球可见、窗口还在）",
          bar18.ball_mode and bar18.ball.isVisible() and bar18.isVisible()
          and (bar18.width(), bar18.height()) == (H.BALL, H.BALL),
          f"球形态={bar18.ball_mode} 球可见={bar18.ball.isVisible()} "
          f"窗口可见={bar18.isVisible()} {bar18.width()}x{bar18.height()}")
    bar18.follow()
    check("收成小球后 follow() 不会把它藏起来（球该在屏幕上）",
          bar18.isVisible() and bar18.ball.isVisible(),
          f"窗口可见={bar18.isVisible()} 球可见={bar18.ball.isVisible()}")
    bar18.set_ball(False, save=False)
    check("展开回来：面板可见、球收起",
          bar18.isVisible() and bar18.card.isVisible() and not bar18.ball.isVisible(),
          f"卡片可见={bar18.card.isVisible()} 球可见={bar18.ball.isVisible()}")

    # —— 切群快不快：节流按会话 + 免去抖 + 切回显示上一轮 ——
    outs_before = set(os.listdir(H.OUT_DIR)) if os.path.isdir(H.OUT_DIR) else set()
    bar18.worker.jobs.clear()
    bar18.group, bar18.topic = "", ""          # 从"没有会话"开始，第一次扫描就算换会话
    bar18.last_sig = None
    bar18._pending_sig = None
    bar18.results.clear()
    bar18.on_scan(FakeView("A群(9)", [("甲", ["A 的第一句"])], marker="a1"))
    check("会话首帧直接提交（免去抖，不再白等一个扫描周期）",
          len(bar18.worker.jobs) == 1 and bar18._skip_debounce is False,
          f"提交 {len(bar18.worker.jobs)} 次，_skip_debounce={bar18._skip_debounce}")
    bar18.on_scan(FakeView("A群(9)", [("甲", ["A 的第一句", "A 的第二句"])], marker="a2"))
    check("同一会话内容变了 → 仍走去抖（不是每次抖动都花钱）",
          len(bar18.worker.jobs) == 1 and bar18._pending_sig is not None,
          f"提交 {len(bar18.worker.jobs)} 次，pending={bar18._pending_sig is not None}")
    # 假装 A 群这一轮判完了（真机上由 on_triage 调 _remember_conv_results）
    bar18.results = {"甲": dict(d18, nickname="甲", msgs=["A 的第一句"])}
    bar18._speaker_msgs = {"甲": ["A 的第一句"]}
    bar18._last_ctx = [("甲", "A 的第一句")]
    bar18._remember_conv_results()
    bar18.results.clear()
    bar18.on_scan(FakeView("B群(3)", [("乙", ["B 的第一句"])], marker="b1"))
    check("切到另一个群 → 立刻提交（不继承上一个群的节流窗口）",
          len(bar18.worker.jobs) == 2 and bar18.group == "B群(3)",
          f"提交 {len(bar18.worker.jobs)} 次，当前会话={bar18.group!r}")
    check("节流按会话记（两个会话各有各的时间戳）",
          set(bar18._last_triage_by_conv) == {"A群(9)" + chr(0) + "群聊", "B群(3)" + chr(0) + "群聊"},
          f"{list(bar18._last_triage_by_conv)}")
    bar18.hint.setText("")
    bar18.on_scan(FakeView("A群(9)", [("甲", ["A 的第一句"])], marker="a3"))
    check("切回看过的会话 → 立刻显示上一轮结果（不用干等一轮）",
          "甲" in bar18.results and "上一轮" in bar18.hint.text(),
          f"左列={list(bar18.results)} 提示={bar18.hint.text()!r}")
    check("上一轮结果标了时间与刷新状态（不冒充实时）",
          ("刚刚" in bar18.hint.text()) or ("分钟前" in bar18.hint.text()),
          f"{bar18.hint.text()!r}")
    check("切回 A 仍在它自己的 20s 窗口内 → 不重复花钱（但界面已有旧结论）",
          len(bar18.worker.jobs) == 2,
          f"提交 {len(bar18.worker.jobs)} 次（应仍是 2）")
    # —— OCR 标题抖动不能当成"换了会话" ——
    # 真机实测（2026-09-24 20:51 调试日志）：同一个群一会儿读成
    # `青柠设计组项目群(39)`、一会儿读成 `推  青柠设计组项目群(9、`；
    # 被当成换会话就会清空结果 + 立刻重新判一轮（多花钱、界面还闪一下）。
    check("严格版只认完全相同/包含（短名字必须精确相等）；宽松版靠公共子串认抖动",
          H._title_same_conv("青柠设计组项目群(39)", "青柠设计组项目群(39)")
          and not H._title_same_conv("青柠设计组项目群(39)", "推  青柠设计组项目群(9、")
          and not H._title_same_conv("群聊", "群聊2")
          and H._title_similar("青柠设计组项目群(39)", "推  青柠设计组项目群(9、")
          and H._title_similar("推  青柠设计组项目群(9、", "α 推  青柠设计组项目群(9")
          and not H._title_similar("青柠设计组项目群(39)", "另一个群(12)"),
          "真机三种读数两两都要判成同一个；完全不相关的群不能")
    # 按真机顺序复现：先进这个群 → 再来两次抖动读数
    bar18.worker.jobs.clear()
    bar18.on_scan(FakeView("青柠设计组项目群(39)", [("甲", ["第一句"])], marker="r1"))
    key0 = bar18._conv_key()
    jobs0 = len(bar18.worker.jobs)
    bar18.on_scan(FakeView("推  青柠设计组项目群(9、", [("甲", ["第一句"])], marker="r2"))
    bar18.on_scan(FakeView("α 推  青柠设计组项目群(9", [("甲", ["第一句"])], marker="r3"))
    check("标题抖动两次都不算换会话（不新提交、会话键不变、省下 2 次调用）",
          jobs0 == 1 and len(bar18.worker.jobs) == 1 and bar18._conv_key() == key0,
          f"首次提交 {jobs0} 次；抖动后共 {len(bar18.worker.jobs)} 次；"
          f"键 {key0!r} → {bar18._conv_key()!r}")
    bar18.on_scan(FakeView("青柠设计组项目群(39) 客服群", [("甲", ["第一句"])], marker="r4"))
    check("抖动里读到更全的标题时只更新显示名，会话键不动",
          bar18.group.endswith("客服群") and bar18._conv_key() == key0,
          f"显示名={bar18.group!r} 键={bar18._conv_key()!r}")
    outs_after = set(os.listdir(H.OUT_DIR)) if os.path.isdir(H.OUT_DIR) else set()
    check("会话缓存只在内存（out/ 里没有新增文件）",
          outs_after == outs_before, f"新增：{sorted(outs_after - outs_before)}")
    check("缓存有上限（不会无限记住所有会话）",
          len(bar18._conv_results) <= H.CONV_CACHE_MAX and H.CONV_CACHE_MAX == 8,
          f"{len(bar18._conv_results)} ≤ {H.CONV_CACHE_MAX}")
    teardown(bar18)

    bad = [r for r in RESULTS if not r[0]]
    verdict = f"VERDICT: {'PASS' if not bad else 'FAIL'} {len(RESULTS) - len(bad)}/{len(RESULTS)}"
    # 结论同时写文件：PySide6+QTest 在 Windows **退出期**会偶发段错误（SIGSEGV，退出码 139），
    # 而断言其实已经全部跑完 —— 所以"过没过"以这个文件为准，退出码只当参考。
    try:
        from wechat_triage_hud import paths as _p
        os.makedirs(_p.OUT_DIR, exist_ok=True)
        with io.open(os.path.join(_p.OUT_DIR, "selfcheck_result.txt"), "w",
                     encoding="utf-8") as f:
            f.write(verdict + "\n")
            for ok, name, detail in RESULTS:
                if not ok:
                    f.write(f"FAIL: {name} —— {detail}\n")
    except Exception as e:      # noqa: BLE001
        print(f"（结论文件写失败：{e}）")
    print(f"\n=== 结果：{len(RESULTS) - len(bad)}/{len(RESULTS)} 通过"
          f"{'，失败 ' + str(len(bad)) + ' 项：' + '；'.join(r[1] for r in bad) if bad else ''} ===")
    print(verdict)
    print(f"临时目录（可删）：{TMP}")
    sys.stdout.flush()
    return 0 if not bad else 1


if __name__ == "__main__":
    # 收尾只做一件事：硬退。**不要在退出路径上碰 Qt 对象** —— 实测在已销毁的 QThread 上
    # 调 isRunning()/wait() 会段错误（退出码 139，而且是在所有断言跑完之后才崩，
    # 看起来"全绿"但退出码是崩的）。每个实例的线程都在各自的 teardown() 里停过了。
    try:
        code = main()
    except Exception:
        import traceback
        traceback.print_exc()
        # 关键：异常中断也必须**重写**结论文件 —— 否则外面读到的还是上一次的 PASS
        # （这个坑今天真踩过：第 18 节一直在抛异常，而结论文件里躺着上一轮的 PASS）
        try:
            from wechat_triage_hud import paths as _p
            os.makedirs(_p.OUT_DIR, exist_ok=True)
            with io.open(os.path.join(_p.OUT_DIR, "selfcheck_result.txt"), "w",
                         encoding="utf-8") as f:
                f.write("VERDICT: FAIL 0/0 —— 脚本异常中断（见 traceback）\n")
        except Exception:
            pass
        sys.stdout.flush()
        code = 1
    os._exit(code)
