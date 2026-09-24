# -*- coding: utf-8 -*-
"""生成文档用的**模拟图**（README / 文档里展示 UI 用）。

为什么要有这个脚本：
  · 真实截图里必然含聊天原文 —— 本项目明文规定**绝不入库**（见 SECURITY.md / CONTRIBUTING.md）；
  · 但"这玩意儿长什么样"必须给人看，所以用**虚构数据**离屏渲染（offscreen 平台 + 系统字体），
    版式、配色、交互文案与真机一模一样，内容全是编的。

跑法：python tools/render_mocks.py       → 输出到 screenshots/*.png
改 UI 之后重跑一遍，文档里的图就跟着更新（图里不含任何真实数据，可以放心提交）。
"""
from __future__ import annotations

import os
import sys
import tempfile
import time

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")     # 别弹到用户屏幕上
os.environ.setdefault("QT_QPA_FONTDIR", "C:/Windows/Fonts")  # offscreen 也要有字，不然全是方框

PROJECT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PROJECT_DIR)

from PySide6.QtCore import QObject, Qt, QTimer, Signal              # noqa: E402
from PySide6.QtGui import QColor, QFont, QImage, QPainter, QPixmap  # noqa: E402
from PySide6.QtWidgets import QApplication                          # noqa: E402

from wechat_triage_hud import hud as H                              # noqa: E402
from wechat_triage_hud import paths as P                            # noqa: E402

OUT = os.path.join(P.PROJECT_DIR, "screenshots")
TMP = tempfile.mkdtemp(prefix="mock_")


# --------------------------------------------------------------------------
# 桩：不截屏、不联网、不写真实文件（与 tests/dev_hud_buttons_verify.py 同款）
# --------------------------------------------------------------------------
class _FakeWin:
    hwnd, rect, minimized, visible = 1, (300, 100, 957, 956), False, True


class _FakeWW:
    def find_main(self, hwnd=None):
        return _FakeWin()

    def occlusion(self, *a, **k):
        return (False, "")

    def window_rect(self, h):
        return None


class _NoScanner(QObject):
    scanned = Signal(object)
    failed = Signal(str)
    blocked = Signal(str)
    tick = Signal(dict)

    def __init__(self, *a, **k):
        super().__init__()
        self.ignore_hwnd = None
        self.names = self.titles = None
        self._stop = False
        self.paused = False
        self.last_tick_at = time.time()

    def start(self):
        pass

    def stop(self):
        pass

    def wait(self, *a):
        return True

    def isRunning(self):
        return True           # 别让看门狗以为扫描线程死了（否则面板会写"已自动重启"的提示）


class _NoWorker(QObject):
    done = Signal(dict)           # 真实 TriageWorker 用的是 done（写错会被 HudBar.__init__ 立刻打脸）
    failed = Signal(str)
    stage = Signal(dict)
    msg_done = Signal(dict)

    def __init__(self, *a, **k):
        super().__init__()
        self.profile = H.SpeakerProfile()
        self.throttle = H.Throttle()
        self.cold_start = True
        self.jobs = []
        self.last_msg_job = None

    def start(self):
        pass

    def stop(self):
        pass

    def wait(self, *a):
        return True

    def isRunning(self):
        return False

    def submit(self, job):
        self.jobs.append(job)          # 只记账：模拟图不真的调 API

    def submit_msg(self, job):
        self.last_msg_job = job

    def drop_jobs(self):
        pass


H.ww = _FakeWW()
H.wc.grab = lambda *a, **k: (_ for _ in ()).throw(RuntimeError("生成模拟图：不截屏"))
H.Scanner = _NoScanner
H.TriageWorker = _NoWorker
H.AUDIT_PATH = os.path.join(TMP, "audit.jsonl")
H.HIST_PATH = os.path.join(TMP, "hist.json")
_real_prefs = H.UiPrefs
H.UiPrefs = lambda *a, **k: _real_prefs(os.path.join(TMP, "ui.json"))
H.ENV_PATH = os.path.join(TMP, ".env")
H.META_PATH = os.path.join(TMP, "meta.json")       # GroupStore 默认写这里
H.DEBUG_PATH = os.path.join(TMP, "dbg.log")
H.DEBUG = False                                    # 生成模拟图不需要调试日志
with open(H.ENV_PATH, "w", encoding="utf-8") as f:
    f.write("jevkey=apikey_" + "demo" * 32 + "\n")


# --------------------------------------------------------------------------
# 虚构数据（**每一个名字和每一句话都是编的**，别换成真实内容）
# --------------------------------------------------------------------------
def person(nick, state, worth, attention, role, msgs, risks=None,
           suff=1.2, reasons=None, danger_word="", danger=0.0,
           true_intent="", she_needs="", literal=None, attention_mass=1.0):
    d = {"nickname": nick, "msgs": msgs, "earlier": [], "hist_total": 0,
         "state": state, "attention": attention, "attention_mass": attention_mass,
         "state_reasons": reasons or [], "role": role, "intent": "chat",
         "addressed_to_me": 0.4, "worth": worth, "worth_conf": 0.62,
         "sufficiency": suff, "risks": risks or {k: 0.0 for k in
                                                 ("risk_ad", "risk_fraud", "risk_conflict", "risk_illegal")},
         "model": "jev-1.13.0", "usage": {"input_tokens": 3672}, "cached": False,
         "scene": "group", "pending_detail": False,
         "danger_word": danger_word, "danger": danger,
         "true_intent": true_intent, "she_needs": she_needs, "literal": literal}
    return d


GROUP = "青柠设计组(12)"
DM = "小满"
PEOPLE = [
    # 一个明显的广告号：广告拉人 0.91（高）、诈骗嫌疑 0.44（中）
    person("福利君", "alert", "insufficient_info", 6, "ad_bot",
           ["加我微信 free888 领全套设计规范", "限时三天，群里前 20 名免费"],
           risks={"risk_ad": 0.91, "risk_fraud": 0.44, "risk_conflict": 0.05, "risk_illegal": 0.12},
           suff=2.4, reasons=["点我名", "风险:risk_ad"],
           attention_mass=0.72),
    person("小满", "todo", "may_reply", 4, "asker",
           ["周三前能出图吗？我这边要排期", "先给一版粗的也行"],
           risks={"risk_ad": 0.02, "risk_fraud": 0.01, "risk_conflict": 0.0, "risk_illegal": 0.0},
           suff=1.2, reasons=["可回可不回:may_reply"]),
    person("周工", "silent", "note_only", 0, "sharer",
           ["我这边没问题，按你们节奏来"],
           suff=0.9),
    person("林可", "silent", "insufficient_info", 0, "regular",
           ["嗯", "好的"],
           suff=2.7, reasons=["结论不清晰且非必回，保持沉默"]),
]

DM_DETAIL = person(DM, "todo", "should_reply", 5, "",
                   ["这个报价能不能再降一点？预算卡得比较死"],
                   suff=1.4, reasons=["该回:should_reply"],
                   danger_word="安全", danger=2.0,
                   true_intent="seek_explanation", she_needs="explanation", literal=0.62)
DM_DETAIL["scene"] = "dm"        # 私聊走私聊那套标签（真实意图/对方需要/危险等级…）


def new_bar(group: str, topic: str, viewer: dict, size=(747, 430)):
    b = H.HudBar()
    b.scanner.stop()
    b.t_pos.stop()
    b.set_ball(False, save=False)
    b.group, b.topic, b.viewer = group, topic, viewer
    b.prefs.set("panel_size", list(size))
    # 抹掉"测试环境假象"（启动占位文案、扫描线程已退出的提示），换成真机上该有的状态行
    b.hint.setVisible(False)
    b.dot.setStyleSheet("color:#22a06b")
    b.b_state.setText("需我 18% · 风险 13%")
    b.b_state.setStyleSheet("font-size:10px;color:#fff;border-radius:3px;padding:1px 6px;"
                            "background:#9a9a9a")
    b.b_cost.setText("今日 9 次/时 · $0.0012")
    b.set_progress("完成", 100, "完成 · 细判 4 人 · 需我 18%")
    return b


def settle(app, n=10):
    for _ in range(n):
        app.processEvents()
        time.sleep(0.04)


def save(widget, name: str):
    if hasattr(widget, "hint"):        # 提示行在真机上常驻态是隐藏的，演示图里也藏掉
        widget.hint.setVisible(False)
    os.makedirs(OUT, exist_ok=True)
    path = os.path.join(OUT, name)
    widget.grab().save(path)
    print(f"  {os.path.relpath(path, PROJECT_DIR)}  ({widget.width()}x{widget.height()})")
    return path


def main() -> int:
    app = QApplication.instance() or QApplication(sys.argv)
    print("生成模拟图 →", os.path.relpath(OUT, PROJECT_DIR))

    # ① 群聊总览：左列（列头 + 三态 + 徽标）+ 右栏某人的判断（风险默认高/中/低）
    b = new_bar(GROUP, "群聊", {"群昵称": "小满", "职责": "设计负责人", "群性质": "客户群"})
    b.results = {p["nickname"]: p for p in PEOPLE}
    b._apply_scene_labels()
    b._rebuild_rows(PEOPLE, parsed_count=len(PEOPLE))
    b.on_person_click("福利君")
    settle(app)
    save(b, "01-group-overview.png")

    # ② 同一个人的判断：点「风险」标题 → 显示具体概率（同一屏的两种显示方式）
    titles = [w for w in b.right_panel.findChildren(H.ClickLabel)
              if w.text().startswith("风险")]
    if titles:
        from PySide6.QtTest import QTest
        QTest.mouseClick(titles[0], Qt.LeftButton)
        settle(app, 6)
        save(b, "02-group-risk-percent.png")
        QTest.mouseClick(titles[0], Qt.LeftButton)     # 切回等级
        settle(app, 6)

    # ③ 私聊：无左列、自动选中对方
    b2 = new_bar(DM, "单聊", {"关系": "客户", "身份": "乙方设计"}, size=(560, 430))
    b2.results = {DM: DM_DETAIL}
    b2._apply_scene_labels()
    b2._rebuild_rows([DM_DETAIL], parsed_count=1)
    b2.on_person_click(DM)
    settle(app)
    save(b2, "03-dm.png")

    # ④ 点一条消息 → 单独判这一条（带「← 返回按人判断」）
    b3 = new_bar(GROUP, "群聊", {"群昵称": "小满", "职责": "设计负责人", "群性质": "客户群"})
    b3.results = {p["nickname"]: p for p in PEOPLE}
    b3._apply_scene_labels()
    b3._rebuild_rows(PEOPLE, parsed_count=len(PEOPLE))
    b3.on_person_click("小满")
    settle(app)
    b3.on_msg_click("周三前能出图吗？我这边要排期")
    settle(app, 6)
    save(b3, "04-message-judging.png")
    msg_detail = dict(DM_DETAIL, nickname="小满", msgs=["周三前能出图吗？我这边要排期"],
                      scene="msg", worth="may_reply", worth_conf=0.54)
    b3.on_msg_done(msg_detail)
    settle(app, 6)
    save(b3, "05-message-result.png")

    # ⑤ 小球形态（52px，外环 = 状态色）
    b4 = new_bar(GROUP, "群聊", {}, size=(747, 430))
    b4.results = {p["nickname"]: p for p in PEOPLE}
    b4._ball_bad = None
    b4.set_ball(True, save=False)
    settle(app, 6)
    save(b4, "06-ball.png")

    # ⑥ 设置对话框（含「推荐 X · 作用」小字）
    dlg = H.SettingsDialog()
    dlg.show()
    settle(app, 6)
    save(dlg, "07-settings.png")
    dlg.close()

    # ⑦ 主图：把群聊面板放到"微信旁边"的示意背景上（背景是纯色块 + 文字占位，不画微信界面）
    panel = QPixmap(os.path.join(OUT, "01-group-overview.png"))
    canvas = QPixmap(1180, 520)
    canvas.fill(QColor("#f2f3f5"))
    p = QPainter(canvas)
    p.setRenderHint(QPainter.Antialiasing, True)
    p.setPen(Qt.NoPen)
    p.setBrush(QColor("#e6e7ea"))
    p.drawRoundedRect(700, 24, 456, 472, 8, 8)
    p.setPen(QColor("#9a9a9a"))
    f = QFont()
    f.setPointSize(11)
    p.setFont(f)
    p.drawText(700, 24, 456, 472, Qt.AlignCenter,
               "（这里是微信窗口\n示意位置：面板吸附在它旁边）")
    p.drawPixmap(24, 30, panel)
    p.end()
    os.makedirs(OUT, exist_ok=True)
    canvas.save(os.path.join(OUT, "00-overview-with-wechat.png"))
    print(f"  screenshots/00-overview-with-wechat.png  ({canvas.width()}x{canvas.height()})")

    print("完成。图里全是虚构数据（人名/消息都是编的），可以提交。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
