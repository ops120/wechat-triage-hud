"""群聊分诊工具栏 —— 按人分诊，下挂在微信窗口底部。

与上一版的根本区别：
  上一版：点一条消息 → 判断那一条（手动查询工具，不点就没有任何提示）
  本  版：**自动扫描 + 两段式分诊**（skim 全体 → 只细判前 K 名），
          按**人**给结论，并且**默认静默**——只有 alert 级才进入视线。

三条产品承诺在这里落地：
  1. 默认沉默：silent 的人只留一行灰字，不抢注意力；只有 alert 才高亮
  2. 只在明确需要我时才提示：门控规则在 qset.gate()，代码决定，不交给模型
  3. 风险告警不受"信息充分度"约束（漏报代价高），判断类告警受约束（宁可少打扰）

成本与打扰都由节流兜住：每群每小时 60 次、每日 $0.5 上限、同内容命中缓存不重复计费。
"""
from __future__ import annotations

import ctypes
import json
import os
import re
import sys
import time
import traceback
from collections import deque

from PySide6.QtCore import QEvent, QPoint, Qt, QThread, QTimer, Signal
from PySide6.QtGui import (QColor, QFontMetrics, QGuiApplication, QIcon,
                           QPainter, QPainterPath, QPen, QPixmap)
from PySide6.QtWidgets import (
    QApplication, QCheckBox, QComboBox, QDialog, QDialogButtonBox, QFrame,
    QHBoxLayout, QLabel, QLineEdit, QMenu, QProgressBar, QPushButton,
    QScrollArea, QSizeGrip, QSpinBox, QDoubleSpinBox, QSystemTrayIcon,
    QVBoxLayout, QWidget,
)


from . import wechat_capture as wc                       # noqa: E402
from . import wechat_window as ww                        # noqa: E402
from .jev_engine import JevClient, load_api_key    # noqa: E402
from .person_engine import SpeakerProfile, analyse_msg   # noqa: E402
from .qset import (                                 # noqa: E402
    MAX_CONTEXT_LINES, RISK_LABELS, STATE_LABELS, label, suff_level,  # noqa: E402
)
from .triage import Throttle, triage               # noqa: E402

from .paths import (                                     # noqa: E402
    AUDIT_PATH, BALL_IMG, DEBUG_PATH, ENV_PATH, PROJECT_DIR, HIST_PATH, META_PATH, OUT_DIR,
    UI_PATH,
)

OUT = OUT_DIR

# OCR 对"媒体占位"的读法五花八门：`〔语音 13″〕` 实测被读成 `」13″」` 这种残片。
# 判据不能只看开头几个固定串，要按"出现这些词、或以括号类字符起头"来认。
_MEDIA_JUNK = re.compile(
    r"〔|【|语音|图片|表情|视频|动画|文件|链接|红包|转账|位置|名片|^\s*[」』】\)\]】]")

MIN_W, GAP = 640, 6
BALL = 52              # 收起态小球直径
BALL_GAP = 8           # 小球与微信窗口之间留的缝
DRAG_SLOP = 4          # 位移小于它当"点击"，大于它当"拖动"（桌面版 4px）
ICON_BTN = 26          # 标题栏图标按钮的边长（用户："按钮不明显，要和图钉一样大"）
HIST_CHARS = 900       # 每人累积历史的字符预算（条数上限见设置 history_msgs）
MSG_ROWS = 4           # 右栏「他最近的消息」最多几行（每多一行，面板就要多长一截）
# 左列人员行的列宽（**唯一来源**：PersonRow 与列头 l_cols 都从这里取，必须一致才对得齐）。
# 原来合计 ~396px 且**没给风险徽标留预算** —— 面板 787 宽时左列只有 494，两枚徽标
# （广告拉人 60 + 信息不足 56）被挤出行外、半个背景被裁（用户 2026-09-24 截图："这个看不清"）。
ROW_MARK_W, ROW_NICK_W, ROW_ROLE_W = 14, 104, 80
ROW_WORTH_W, ROW_ATTN_W = 64, 38
ROW_CHIP_W = 132       # 风险/信息 徽标的预算（**最多一枚**；空间不够时压到 ROW_CHIP_MIN_W）
ROW_CHIP_MIN_W = 60
ROW_SPACING = 8        # 各列之间的间距（列头用同一个值才算对齐）
CONV_CACHE_MAX = 8     # 内存里最多记几个会话的「上一轮结果」（切回立刻显示，见 _remember_conv_results）
COMPACT_W = 560        # 用户把面板拖窄到这个宽度以下 → 进紧凑模式（隐右栏详情，只留人名单）
# 右栏「键 + 值」的排法（用户截图指出"值太远了"）：键定宽 → 值紧跟其后 → 尾巴留白。
# 原来是 键 → addStretch(1) → 值(右对齐)，两列模式下每格 ~260px，值被顶到格子最右边，
# 中间空出近 200px，扫读时眼睛得在两个词之间来回跳。
KV_LABEL_W = 58        # 键列宽（实测最长键「信息充分度」= 55px）
KV_GAP = 8             # 键与值之间的固定间隔
KV_BUDGET_2COL = 96    # 两列模式下值的省略预算（一格就这么点宽）
SAME_HASH_LOG = 10     # 同一帧连续多少轮就记一笔（排查"面板为什么不动"用）
SAME_HASH_FORCE = 10   # 同一帧连续多少轮就**不信哈希**、强制重新识别一次（3s×10 ≈ 30 秒）
SCAN_STALL_S = 45      # 扫描线程多久没有推进就算卡住 → 看门狗重启它
# F-21 暂停/免打扰：全局热键（Windows 的 RegisterHotKey）
HOTKEY_ID = 0xB1D0
HOTKEY_TEXT = "Ctrl+Alt+H"
MOD_ALT, MOD_CONTROL, WM_HOTKEY, VK_H = 0x0001, 0x0002, 0x0312, 0x48
PANEL_MIN_W, PANEL_MIN_H = 420, 120   # 手动调整时的硬下限（再小就没法用了）

# 存档键规则：群沿用会话标题**原名**（不动用户已填的群昵称记录），
# 私聊加后缀 —— 否则"某个群"和"某个同名联系人"会共用一条记录，身份串味。
DM_KEY_SUFFIX = "｜单聊"
DEFAULT_REL_KEY = "__默认关系__"       # 全局默认关系在 group_meta.json 里的保留键
# 下拉词表：固定词表对模型更稳（可重复、可统计、可回归），最后一项允许手填
REL_CHOICES = ["客户", "同事", "朋友", "家人", "陌生", "其他…"]
KIND_CHOICES = ["工作群", "客户群", "亲友群", "兴趣群", "陌生群", "其他…"]
IDENTITY_HINT = "例如：乙方的售前 / 甲方采购 / 普通成员"


def _install_error_trap() -> None:
    """把 Qt 槽里未捕获的异常记进调试日志。

    没这一步时，槽里抛的异常只会打到 stderr（面板是后台起的，没人看得见），
    表现是"面板就是不动"，追查只能靠猜。今天那条私聊 TypeError 就是这么隐身的。
    """
    def hook(t, v, tb):
        dbg("!! 未捕获异常: " + "".join(traceback.format_exception(t, v, tb))[-600:])
        sys.__excepthook__(t, v, tb)
    sys.excepthook = hook
# SCAN_MS 是**周期**而不是"干完活后睡多久"。
# 实测 OCR 单轮约 3.8s，若按"睡 2.2s"算，实际周期会变成 6s，去抖两轮就 >12s，
# 用户会明显觉得"没反应"。所以要按"周期 - 耗时"来睡。
SCAN_PERIOD_MS = 3000
MIN_TRIAGE_INTERVAL = 20      # 同一批内容的最小重复判断间隔（秒）
PROGRESS_STAGES = ("扫描", "粗筛", "细判", "完成")
POLL_MS = 600          # 窗口位置跟随的轮询周期
DBG_PATH = DEBUG_PATH
DEBUG = os.environ.get("JEV_HUD_DEBUG") == "1"

WINDOW_TITLE = "wechat-triage-hud"
MUTEX_NAME = "wechat-triage-hud-single-instance-v1"
ERROR_ALREADY_EXISTS = 183
STATE_COLOR = {"alert": "#c2410c", "todo": "#c99a2e", "silent": "#b0b0b0"}


def _num(v, default: float = 0.0) -> float:
    """把可能为 None / 字符串的数转成 float。

    踩到过：`d.get(k, 0)` 在"键存在但值是 null"时**不会**返回默认值。
    私聊路径的 skim 就是 `{"needs_me": None, ...}`，于是 `round(None*100)`
    抛 TypeError，异常从 Qt 槽里冒出去，那一轮的分诊结果整段不渲染。
    """
    try:
        return float(v)
    except (TypeError, ValueError):
        return default
STATE_MARK = {"alert": "⚠", "todo": "·", "silent": ""}


# OCR 抖动：同一屏内容的两次识别会在空格/标点/个别字上有微小差异。
# 去抖如果要求"签名逐字节相同"，这些抖动会让签名永远不一致 ——
# 后果是面板看着在跑，却永远不提交分诊（实测 14 秒 0 次提交）。
# 所以签名只保留 CJK 与字母数字，丢掉标点、空白、全半角差异。
_SIG_KEEP = None


def _norm_sig(text: str) -> str:
    global _SIG_KEEP
    if _SIG_KEEP is None:
        import re as _re
        _SIG_KEEP = _re.compile(r"[^0-9A-Za-z一-鿿]+")
    return _SIG_KEEP.sub("", text or "")


def _lcs_len(a: str, b: str) -> int:
    """最长公共子串长度（标题相似度用；标题都很短，O(n·m) 无所谓）。"""
    if not a or not b:
        return 0
    prev = [0] * (len(b) + 1)
    best = 0
    for ca in a:
        cur = [0] * (len(b) + 1)
        for j, cb in enumerate(b, 1):
            if ca == cb:
                cur[j] = prev[j - 1] + 1
                best = max(best, cur[j])
        prev = cur
    return best


def _title_similar(a: str, b: str) -> bool:
    """标题"像不像同一个"——**宽松版**，专门对付 OCR 噪声。

    真机实测（2026-09-24 20:52 调试日志）同一个群被读成三种：
      `青柠设计组项目群(39)` / `推  青柠设计组项目群(9、` / `α 推  青柠设计组项目群(9`
    —— 多个噪声字（推 / α）、尾巴还被截了。宽松规则：归一化后**最长公共子串
    ≥8 字且 ≥ 短者的 70%**。配合"发言人要有交集"一起用（见 on_scan），
    否则"青柠设计组项目群(39)"和"(42)"这种姐妹群会被并成一个。
    """
    x, y = _norm_sig(a), _norm_sig(b)
    if not x or not y:
        return False
    n = _lcs_len(x, y)
    return n >= 8 and n >= min(len(x), len(y)) * 0.7


def _title_same_conv(a: str, b: str) -> bool:
    """两次 OCR 读数是不是**同一个会话**。

    真机实测（2026-09-24 20:51 的调试日志）：同一个群，OCR 一会儿读成
    `青柠设计组项目群(39)`、一会儿读成 `推  青柠设计组项目群(9、`（多了个"推"、少了半个括号）。
    标题变一下就被当成"换了会话" → 清空结果、重新判一轮，**多花钱还闪一下**。
    所以判"是不是同一个会话"要归一化后再比：
      · 去掉空白与标点后完全相同 → 同一个
      · 一方包含另一方（短的 ≥6 字）→ 当成同一个（长标题被 OCR 截短的常见形态）
    只有 6 字以上的标题才允许"包含即同一个"：短名字（如「群聊」「群聊2」）必须精确相等，
    否则会把两个不同的群并成一个（这是这个启发式的已知代价，宁可多切一次也不能串群）。
    """
    import re as _re

    def n(t: str) -> str:
        return _re.sub(r"\s+", "", t or "")
    x, y = n(a), n(b)
    if not x or not y:
        return False
    if x == y:
        return True
    short, long_ = (x, y) if len(x) <= len(y) else (y, x)
    return len(short) >= 6 and short in long_


def dbg(msg: str) -> None:
    if not DEBUG:
        return
    try:
        os.makedirs(OUT, exist_ok=True)
        with open(DBG_PATH, "a", encoding="utf-8") as f:
            f.write(f"{time.strftime('%H:%M:%S')} {msg}{chr(10)}")
    except Exception:
        pass


def _already_running() -> bool:
    """命名互斥量做单实例保护。

    没有它时反复启动会堆积僵尸实例（实测曾留下 11 个进程 / 4 个窗口）。
    注意：必须在 CreateMutexW 之后**立刻**读 GetLastError，
    否则中间任何一次 API 调用都会把它覆盖掉。
    """
    try:
        k = ctypes.windll.kernel32
        k.SetLastError(0)
        k.CreateMutexW(None, False, MUTEX_NAME)
        return k.GetLastError() == ERROR_ALREADY_EXISTS
    except Exception:
        return False        # 取不到就放行，不因为保护机制本身挡住启动


def activate_existing() -> bool:
    """把已存在的面板调到前台。

    收起成小球时 ShowWindow 只是把一个 52px 的球摆在那里，用户看起来是"没反应"，
    所以顺带在存档里写一个 expand_at 时间戳 —— 运行中的实例在轮询里会认领并展开。
    """
    try:
        UiPrefs().set("expand_at", time.time())
    except Exception as e:
        dbg(f"写展开请求失败: {e}")
    try:
        h = ctypes.windll.user32.FindWindowW(None, WINDOW_TITLE)
        if h:
            ctypes.windll.user32.ShowWindow(h, 9)      # SW_RESTORE
            ctypes.windll.user32.SetForegroundWindow(h)
            return True
    except Exception:
        pass
    return False


# --------------------------------------------------------------------------
# 群元数据（群昵称、累计计数）—— 存本机，不上传
# --------------------------------------------------------------------------
class GroupStore:
    def __init__(self, path: str = META_PATH):
        self.path = path
        self.data: dict = {}
        try:
            with open(path, "r", encoding="utf-8") as f:
                self.data = json.load(f)
        except Exception:
            self.data = {}

    def get(self, group: str) -> dict:
        rec = self.data.setdefault(group, {})
        # 老文件里只有 me/duty/known —— 用 setdefault 补新键，旧记录照读照用
        for k, v in (("me", None), ("duty", None), ("kind", None),
                     ("rel", None), ("identity", None),
                     ("scene", None), ("known", False)):
            rec.setdefault(k, v)
        return rec

    def set_me(self, group: str, me: str, duty: str = "") -> None:
        """群场景的旧入口（保持不变，别把已填的数据写坏）。"""
        rec = self.get(group)
        rec["me"] = (me or "").strip() or None
        rec["duty"] = (duty or "").strip() or None
        rec["scene"] = rec.get("scene") or "group"
        rec["known"] = True
        self.save()

    def set_scene(self, key: str, scene: str, **fields) -> None:
        """按场景写一条记录。群聊写 me/duty/kind，私聊写 rel/identity。"""
        rec = self.get(key)
        for k in ("me", "duty", "kind", "rel", "identity"):
            if k in fields:
                v = fields[k]
                rec[k] = (str(v).strip() or None) if v is not None else None
        rec["scene"] = scene
        rec["known"] = True
        self.save()

    # ---- 全局默认关系：抄同类实现 jarvis的做法，但**默认留空** ----
    # 同类实现 jev-chat-windows 把它写死成 "romantic partners" —— 对群聊分诊这种场景
    # 用错默认比没有默认更糟（模型会照着"恋爱关系"去解释一句普通的询价）。
    # 留空时 state 里仍是「（未填写）」，面板会提示用户去填。
    def default_relationship(self) -> str | None:
        rec = self.data.get(DEFAULT_REL_KEY) or {}
        return (rec.get("rel") or "").strip() or None

    def set_default_relationship(self, rel: str | None) -> None:
        rec = self.data.setdefault(DEFAULT_REL_KEY, {})
        rec["rel"] = (rel or "").strip() or None
        rec["scene"] = "default"
        self.save()

    def save(self) -> None:
        try:
            os.makedirs(os.path.dirname(self.path), exist_ok=True)
            with open(self.path, "w", encoding="utf-8") as f:
                json.dump(self.data, f, ensure_ascii=False, indent=2)
        except Exception as e:
            dbg(f"群元数据保存失败: {e}")


# --------------------------------------------------------------------------
# 面板外观状态（小球形态 / 小球坐标）—— 存本机，不上传
# 和 GroupStore 分开存：那个文件按群名组织，是"判断用"的输入；
# 这个是纯界面状态，坏了不该影响判断。
# --------------------------------------------------------------------------
class UiPrefs:
    def __init__(self, path: str = UI_PATH):
        self.path = path
        self.data: dict = {}
        try:
            with open(path, "r", encoding="utf-8") as f:
                self.data = json.load(f)
            if not isinstance(self.data, dict):
                self.data = {}
        except Exception:
            self.data = {}

    def get(self, key: str, default=None):
        return self.data.get(key, default)

    def reload(self) -> dict:
        """重新读盘。另一个进程（重复启动时的 activate_existing）会往这个文件里
        写请求，只靠 __init__ 读一次是看不见的 —— 但内存里可能已经有本进程
        刚写下的值，所以按文件为主、内存里的键补回去（写都是立即落盘，两者一致）。"""
        try:
            with open(self.path, "r", encoding="utf-8") as f:
                disk = json.load(f)
            if isinstance(disk, dict):
                self.data = {**self.data, **disk}
        except Exception:
            pass
        return self.data

    def set(self, key: str, value) -> None:
        if self.data.get(key) == value:
            return
        self.data[key] = value
        self.save()

    def save(self) -> None:
        try:
            os.makedirs(os.path.dirname(self.path), exist_ok=True)
            with open(self.path, "w", encoding="utf-8") as f:
                json.dump(self.data, f, ensure_ascii=False)
        except Exception as e:
            dbg(f"界面状态保存失败: {e}")


# --------------------------------------------------------------------------
# 群昵称对话框
# 注意：主窗口是 WindowDoesNotAcceptFocus（避免抢焦点），
# 所以这个对话框必须用**自己的普通 flags**，否则输入不了字。
# --------------------------------------------------------------------------
class NicknameDialog(QDialog):
    """按场景问"决定性输入"（名字沿用，因为外部有引用；内容已按场景分叉）。

    - 群聊：① 我在本群的昵称 ② 职责 ③ **群性质**（下拉）
    - 私聊：① **我和这人什么关系**（下拉）② 我在这段对话里的身份

    为什么非问不可：`worth_my_reply`（该不该回）的判据**不在消息文本里**。
    独立校准实测：判据缺席时模型会**自信地错**（准确率 44.7% 却有 0.74 的平均置信）。
    所以：默认值留空 → state 里显式发「（未填写）」→ 面板同时提示用户去填，
    而不是让模型替我们猜一个"普通朋友"。
    """

    def __init__(self, contact: str, scene: str = "group", rec: dict | None = None,
                 default_rel: str = "", parent=None):
        super().__init__(parent)
        rec = rec or {}
        self.scene = scene
        dm = scene == "dm"
        self.setWindowTitle("填写关系与身份" if dm else "填写我在这个群里的身份")
        self.setWindowFlags(Qt.Dialog | Qt.WindowStaysOnTopHint)
        self.setMinimumWidth(470)
        v = QVBoxLayout(self)
        if dm:
            v.addWidget(QLabel(
                f"会话：{contact}\n\n"
                "模型需要知道「你和这个人什么关系」，才能判断这条该不该你回、\n"
                "以及该用什么分寸。只存在本机，不上传。"))
            v.addWidget(QLabel("① 我和这个人的关系"))
            self.rel_box = QComboBox()
            self.rel_box.addItems(REL_CHOICES)
            cur_rel = (rec.get("rel") or default_rel or "").strip()
            if cur_rel:
                i = self.rel_box.findText(cur_rel)
                if i >= 0:
                    self.rel_box.setCurrentIndex(i)
                else:
                    self.rel_box.setCurrentText("其他…")
            v.addWidget(self.rel_box)
            self.other = QLineEdit(cur_rel if cur_rel and self.rel_box.currentText() == "其他…"
                                   else "")
            self.other.setPlaceholderText("选「其他…」时在这里手填，例如：大学室友 / 前同事")
            self.other.setEnabled(self.rel_box.currentText() == "其他…")
            self.rel_box.currentTextChanged.connect(
                lambda t: self.other.setEnabled(t == "其他…"))
            v.addWidget(self.other)
            v.addWidget(QLabel("② 我在这段对话里的身份（可留空）"))
            self.identity = QLineEdit(rec.get("identity") or "")
            self.identity.setPlaceholderText(IDENTITY_HINT)
            v.addWidget(self.identity)
            self.default_chk = QCheckBox("以后没填过的会话都按这个关系算")
            self.default_chk.setChecked(bool(default_rel) and not rec.get("rel"))
            v.addWidget(self.default_chk)
            # 群场景的属性留着占位，免得外部按老代码取 .edit/.duty 时炸
            self.edit, self.duty, self.kind_box = None, None, None
        else:
            v.addWidget(QLabel(
                f"群：{contact}\n\n"
                "模型需要知道这两件事，才能判断「有没有人在点我名」\n"
                "以及「这条该不该我回」。只存在本机，不上传。"))
            v.addWidget(QLabel("① 我在这个群里的昵称"))
            self.edit = QLineEdit(rec.get("me") or "")
            self.edit.setPlaceholderText("例如：老王 / 张三 / 我的群备注名")
            v.addWidget(self.edit)
            v.addWidget(QLabel("② 我在这个群里的职责/身份（可留空）"))
            self.duty = QLineEdit(rec.get("duty") or "")
            self.duty.setPlaceholderText("例如：答疑的 / 潜水看热闹 / 负责对接客户")
            v.addWidget(self.duty)
            v.addWidget(QLabel("③ 这个群是什么性质的群"))
            self.kind_box = QComboBox()
            self.kind_box.addItems(KIND_CHOICES)
            cur_kind = (rec.get("kind") or "").strip()
            if cur_kind:
                i = self.kind_box.findText(cur_kind)
                if i >= 0:
                    self.kind_box.setCurrentIndex(i)
                else:
                    self.kind_box.setCurrentText("其他…")
            v.addWidget(self.kind_box)
            self.other = QLineEdit(cur_kind if cur_kind and self.kind_box.currentText() == "其他…"
                                   else "")
            self.other.setPlaceholderText("选「其他…」时在这里手填，例如：小区业主群")
            self.other.setEnabled(self.kind_box.currentText() == "其他…")
            self.kind_box.currentTextChanged.connect(
                lambda t: self.other.setEnabled(t == "其他…"))
            v.addWidget(self.other)
            self.rel_box, self.identity, self.default_chk = None, None, None
        bb = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        bb.accepted.connect(self.accept)
        bb.rejected.connect(self.reject)
        v.addWidget(bb)
        (self.edit or self.rel_box).setFocus()

    def _picked(self, box: QComboBox) -> str:
        t = box.currentText()
        if t == "其他…":
            return self.other.text().strip()
        return t.strip()

    def value(self) -> dict:
        """返回要落库的字段。群：{me, duty, kind}；私聊：{rel, identity, set_default}。"""
        if self.scene == "dm":
            return {"rel": self._picked(self.rel_box),
                    "identity": self.identity.text().strip(),
                    "set_default": bool(self.default_chk.isChecked()
                                        and self._picked(self.rel_box))}
        return {"me": self.edit.text().strip(),
                "duty": self.duty.text().strip(),
                "kind": self._picked(self.kind_box)}


# --------------------------------------------------------------------------
# F-22 设置对话框：频率上限 / 成本上限 / 判定阈值 / API Key
# --------------------------------------------------------------------------
class SettingsDialog(QDialog):
    """把写死在代码里的旋钮交出来可改。

    - 频率与成本：每群每小时上限、最小重判间隔、每日成本上限（节流用）
    - 判定阈值：Noul 门槛 / Choice 结论清晰门槛 / 最高打扰门槛（门控用，仍由代码判定）
    - API Key：只显示指纹与长度，**不回显明文**；要换就贴新的（写回 .env）

    校验在保存前做：越界/非数字一律拒绝并说明原因，不静默钳值 ——
    那会让用户以为改成功了（这类"看着生效其实没生效"是本项目最忌讳的）。
    """

    def __init__(self, parent=None):
        super().__init__(parent)
        from .settings import RANGES, LABELS, SETTINGS, help_tooltip
        self.setWindowTitle("设置")
        self.setWindowFlags(Qt.Dialog | Qt.WindowStaysOnTopHint)
        self.setMinimumWidth(470)
        self.boxes: dict = {}
        self.hints: dict = {}          # 每个设置项下面那行"推荐 X · 作用"
        v = QVBoxLayout(self)
        v.addWidget(QLabel("改动只存本机 out/settings.json，保存后立即生效（不必重启）。"))

        sub = QLabel("每一项下面的小字是**推荐值**（= 默认值），按这份模型的实测校准过；"
                     "把鼠标停在标题上看「为什么」。改了的小字会变黄，点「恢复默认」可一次复原。")
        sub.setObjectName("muted")
        sub.setWordWrap(True)
        v.addWidget(sub)

        def add_row(layout, key: str) -> None:
            """一行设置：标题 + 输入框 + 下面一行小字（推荐值 + 作用）。"""
            row = QHBoxLayout()
            lab = QLabel(LABELS[key])
            row.addWidget(lab)
            is_float = isinstance(RANGES[key][2], float)
            box = QDoubleSpinBox() if is_float else QSpinBox()
            lo, hi, _d = RANGES[key]
            box.setRange(lo, hi)
            if is_float:
                box.setDecimals(2)      # QSpinBox 没有 setDecimals（踩过一次 AttributeError）
                box.setSingleStep(0.05)
            else:
                box.setSingleStep(1)
            box.setValue(SETTINGS.get(key))
            box.setToolTip(help_tooltip(key))
            lab.setToolTip(help_tooltip(key))
            self.boxes[key] = box
            row.addStretch(1)
            row.addWidget(box)
            layout.addLayout(row)
            hint = QLabel("")
            hint.setObjectName("muted")
            hint.setWordWrap(True)
            hint.setToolTip(help_tooltip(key))
            self.hints[key] = hint
            layout.addWidget(hint)
            self._refresh_hint(key)
            box.valueChanged.connect(lambda _v, k=key: self._refresh_hint(k))

        v.addWidget(QLabel("① 频率与成本（节流）与上下文 —— 只管「多久判一次、花多少钱」，不改判断口径"))
        for key in ("per_hour", "min_interval_s", "daily_usd", "history_msgs"):
            add_row(v, key)

        v.addWidget(QLabel("② 判定阈值（门控规则不变，只是门槛可调）—— 决定「多大把握才提醒你」"))
        for key in ("thr_noul_hit", "thr_choice_clear", "thr_strong"):
            add_row(v, key)

        v.addWidget(QLabel("③ TypeSafe API Key（只显示指纹；要换就把新的整串贴进来）"))
        key_help = QLabel("推荐：不用动。只有换 Key 时才需要粘贴；面板从不回显明文，"
                          "上面只显示指纹与长度。新的 Key 会写回本机 .env。")
        key_help.setObjectName("muted")
        key_help.setWordWrap(True)
        v.addWidget(key_help)
        try:
            from .jev_engine import load_api_key
            from .jev_log import key_fingerprint
            cur = load_api_key(ENV_PATH)
            self.key_hint = QLabel(f"当前：{key_fingerprint(cur)}（长度 {len(cur)}）")
        except Exception as e:                    # noqa: BLE001
            self.key_hint = QLabel(f"当前：读不到 Key（{e}）")
        self.key_hint.setObjectName("muted")
        v.addWidget(self.key_hint)
        self.key_edit = QLineEdit("")
        self.key_edit.setEchoMode(QLineEdit.Password)
        self.key_edit.setPlaceholderText("粘贴新的 jevkey（留空 = 不改）")
        v.addWidget(self.key_edit)

        self.err = QLabel("")
        self.err.setWordWrap(True)
        v.addWidget(self.err)

        bb = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        bb.button(QDialogButtonBox.Ok).setText("保存")
        bb.button(QDialogButtonBox.Cancel).setText("取消")
        reset = bb.addButton("恢复默认", QDialogButtonBox.ResetRole)
        reset.clicked.connect(self._on_reset)
        bb.accepted.connect(self._on_ok)
        bb.rejected.connect(self.reject)
        v.addWidget(bb)

    def _refresh_hint(self, key: str) -> None:
        """刷新某一项下面的小字：默认值写「推荐 X · 作用」，改了就变黄并写出改动。

        推荐值不是拍脑袋的：`settings.HELP[key]["why"]` 里带着实测数字（模型校准、
        实测 token、实测重复提交次数），"改了会怎样"也写在那里（悬停可见）。
        """
        from .settings import RANGES, help_text
        hint = self.hints.get(key)
        if hint is None:
            return
        cur = self.boxes[key].value()
        default = RANGES[key][2]
        what = help_text(key).split("· ", 1)[-1]      # 去掉"推荐 X ·"前缀，只留作用说明
        if abs(float(cur) - float(default)) > 1e-9:
            hint.setText(f"已改为 {cur}（推荐 {default}）· {what}")
            hint.setStyleSheet("font-size:10.5px;color:#c2410c")
        else:
            hint.setText(help_text(key))
            hint.setStyleSheet("font-size:10.5px;color:#9a9a9a")

    def _on_reset(self) -> None:
        from .settings import RANGES
        for key, box in self.boxes.items():
            box.setValue(RANGES[key][2])
        self.err.setText("已填入默认值（仍要点「保存」才生效）")

    def _on_ok(self) -> None:
        from .settings import SETTINGS, sync_thresholds
        from .jev_log import key_fingerprint
        bad = []
        for key, box in self.boxes.items():
            ok, why = SETTINGS.validate(key, box.value())
            if not ok:
                bad.append(why)
        if bad:
            self.err.setText("；".join(bad))      # 有错就不关窗、不写入
            return
        for key, box in self.boxes.items():
            SETTINGS.set(key, box.value())
        ok, why = SETTINGS.save()
        if not ok:
            self.err.setText(why)
            return
        sync_thresholds()
        newkey = self.key_edit.text().strip()
        if newkey:
            if not newkey.startswith("apikey_") or len(newkey) < 20:
                self.err.setText("Key 看着不对（应以 apikey_ 开头且长度足够）—— 先存了设置，Key 未改")
                self.accept()
                self._notice = "设置已保存；Key 未改（格式不像 key）"
                return
            ok2, why2 = self._write_env_key(newkey)
            if not ok2:
                self.err.setText(why2)
                return
            self._notice = f"设置已保存；Key 已更新为 {key_fingerprint(newkey)}"
        else:
            self._notice = "设置已保存"
        self.accept()

    def _write_env_key(self, key: str) -> tuple[bool, str]:
        """写回 .env 的 jevkey= 行（其余行原样保留）。只在本机，.env 已 gitignore。"""
        try:
            lines = []
            found = False
            if os.path.exists(ENV_PATH):
                with open(ENV_PATH, "r", encoding="utf-8") as f:
                    for ln in f.read().splitlines():
                        if ln.strip().startswith("jevkey="):
                            lines.append(f"jevkey={key}")
                            found = True
                        else:
                            lines.append(ln)
            if not found:
                lines.append(f"jevkey={key}")
            with open(ENV_PATH, "w", encoding="utf-8") as f:
                f.write("\n".join(lines) + "\n")
            return True, ""
        except Exception as e:                    # noqa: BLE001
            return False, f"写入 .env 失败：{e}"


# --------------------------------------------------------------------------
# 可点击的人员行
# --------------------------------------------------------------------------
class PersonRow(QWidget):
    clicked = Signal(str)

    def __init__(self, key: str, nick: str, role: str, worth: str,
                 attention: int, state: str, risks: list[str],
                 low_suff: bool, attn_mass: float = 1.0):
        super().__init__()
        self.key = key
        self.selected = False
        self.setCursor(Qt.PointingHandCursor)
        h = QHBoxLayout(self)
        h.setContentsMargins(7, 3, 7, 3)
        h.setSpacing(ROW_SPACING)

        self.mark = QLabel(STATE_MARK.get(state, ""))
        self.mark.setObjectName("mark")
        self.mark.setFixedWidth(ROW_MARK_W)
        h.addWidget(self.mark)

        # 昵称/角色/要不要回：定宽 + **省略 + tooltip**（长名字以前是被硬截断的，看着像缺字）
        def cell(text: str, obj: str, width: int) -> QLabel:
            lb = QLabel()
            lb.setObjectName(obj)
            lb.setFixedWidth(width)
            fm = QFontMetrics(lb.font())
            lb.setText(fm.elidedText(text or "", Qt.ElideRight, width - 2))
            if lb.text() != (text or ""):
                lb.setToolTip(text)
            return lb

        self.nick = cell(nick or "?", "nick", ROW_NICK_W)
        h.addWidget(self.nick)
        h.addWidget(cell(role, "role", ROW_ROLE_W))
        h.addWidget(cell(worth, "worth", ROW_WORTH_W))

        # 有效质量太低（逃逸吃掉了大部分概率）时不给数字，避免伪精度
        al = QLabel(f"{attention}/10" if attn_mass >= 0.5 else "—")
        al.setObjectName("attn")
        al.setFixedWidth(ROW_ATTN_W)
        h.addWidget(al)

        h.addStretch(1)
        # 风险 / 信息徽标：**最多留一枚** —— 最高的那个风险；没有风险才显示"信息不足"。
        # 其余信息同一枚徽标的 tooltip 里说清。为什么只留一枚：这几列宽度是定死的，
        # 徽标没有自己的预算时（原来就是这样）两枚会一起被挤出行外、半个背景被裁，
        # 白字压在浅色上根本看不清（用户 2026-09-24 截图："这个看不清"）。
        chip_txt, chip_obj, chip_tip = "", "", ""
        if risks:
            top = risks[0]                     # 调用方已按概率从高到低排好
            chip_txt, chip_obj = RISK_LABELS.get(top, top), "risk"
            chip_tip = "风险：" + "、".join(RISK_LABELS.get(r, r) for r in risks)
        elif low_suff:
            chip_txt, chip_obj = "信息不足", "lowsuff"
            chip_tip = "信息充分度不足：模型对这几个人的判断缺依据"
        if chip_txt and low_suff and risks:
            chip_tip += "；另外信息充分度也不足"
        if chip_txt:
            chip = QLabel(chip_txt)
            chip.setObjectName(chip_obj)
            chip.setToolTip(chip_tip)
            chip.setAlignment(Qt.AlignCenter)
            chip.setMinimumWidth(ROW_CHIP_MIN_W)   # 空间不够时先压这一枚，绝不越界
            chip.setMaximumWidth(ROW_CHIP_W)
            h.addWidget(chip)

        self._state = state
        self._apply()

    def set_selected(self, on: bool) -> None:
        self.selected = on
        self._apply()

    def _apply(self) -> None:
        """更新状态与选中效果 —— **选中不靠样式表，自己在 paintEvent 里画**。

        踩过的坑（用户 2026-09-24 截图："红框里看不清"）：这里原来写
        `self.setStyleSheet("background:#e8f0fe;border-radius:5px;")`，而 Qt 会把父控件这条
        **没有选择器**的规则一并套到子控件上 —— 行里那两枚 `QLabel#risk`（橙红底白字）与
        `QLabel#lowsuff`（淡黄底深棕字）的背景被覆盖成选中蓝，白字压在浅蓝上基本看不见。
        """
        c = STATE_COLOR.get(self._state, "#b0b0b0")
        self.update()                      # 选中底色改由 paintEvent 画
        self.mark.setStyleSheet(f"color:{c};font-size:12px;font-weight:700")
        self.nick.setStyleSheet(
            f"color:{'#141414' if self._state != 'silent' else '#9a9a9a'};"
            f"font-size:12px;font-weight:{'600' if self._state=='alert' else '400'}")

    def paintEvent(self, e):
        """选中底色的唯一来源（圆角高亮）——只画自己，子控件的样式表说了算。"""
        if self.selected:
            p = QPainter(self)
            p.setRenderHint(QPainter.Antialiasing, True)
            p.setPen(Qt.NoPen)
            p.setBrush(QColor("#e8f0fe"))
            p.drawRoundedRect(self.rect().adjusted(0, 0, -1, -1), 5, 5)
        super().paintEvent(e)

    def mousePressEvent(self, e):
        if e.button() == Qt.LeftButton:
            e.accept()          # 必须 accept 并 return，避免冒泡到 HudBar 破坏吸附
            self.clicked.emit(self.key)
            return
        super().mousePressEvent(e)


# --------------------------------------------------------------------------
# 可点击的"消息行"：点一条消息就能判这一条（用户要求）
# --------------------------------------------------------------------------
class ClickLabel(QLabel):
    """可点的标签（左键按下即 emit，与 PersonRow/MsgRow 同款行为）。

    用途：右栏「风险」那一行 —— 默认显示 高/中/低（用户 2026-09-24：*"换成高中低，
    要看详细几率就点它变成数据"*），点一下在"等级 ⇄ 百分比"之间切换。
    """

    clicked = Signal()

    def __init__(self, *a, **k):
        super().__init__(*a, **k)
        self.setCursor(Qt.PointingHandCursor)

    def mousePressEvent(self, e):
        if e.button() == Qt.LeftButton:
            e.accept()
            self.clicked.emit()
            return
        super().mousePressEvent(e)


class MsgRow(QWidget):
    """一行消息，可点。

    与 PersonRow 同构：左键按下即 emit（并 accept，别冒泡到 HudBar 破坏吸附/拖动）。
    文本保持**全文折行**（刚做的全文显示不能因为变成可点就退回截断）。
    """

    clicked = Signal(str)

    def __init__(self, text: str, muted: bool = False):
        super().__init__()
        self.text = text
        self.setCursor(Qt.PointingHandCursor)
        h = QHBoxLayout(self)
        h.setContentsMargins(6, 2, 4, 2)
        h.setSpacing(4)
        self.lab = QLabel("　" + text)
        self.lab.setObjectName("vmuted" if muted else "v")
        self.lab.setWordWrap(True)
        h.addWidget(self.lab)
        h.addStretch(1)
        self.setToolTip("点一下：单独判这条消息（用私聊那套潜台词口径）")
        self._apply(False)

    def _apply(self, hover: bool) -> None:
        self.setStyleSheet("background:#f0f4ff;border-radius:5px;" if hover
                           else "background:transparent;border-radius:5px;")

    def enterEvent(self, e):
        self._apply(True)
        super().enterEvent(e)

    def leaveEvent(self, e):
        self._apply(False)
        super().leaveEvent(e)

    def mousePressEvent(self, e):
        if e.button() == Qt.LeftButton:
            e.accept()          # 与 PersonRow 同：接受并不冒泡
            self.clicked.emit(self.text)
            return
        super().mousePressEvent(e)


# --------------------------------------------------------------------------
# OCR 线程（只刷新，不调 API）
# --------------------------------------------------------------------------
class Scanner(QThread):
    scanned = Signal(object)
    failed = Signal(str)
    blocked = Signal(str)          # 微信窗口不可用（不可见/最小化/被遮挡）
    tick = Signal(dict)            # 轮次进度：{skip, ocr_ms, total, period}

    def __init__(self, parent=None):
        super().__init__(parent)
        self._stop = False
        self._errs = 0
        self._last_block = None
        self._last_hash = None     # 上一帧哈希：同帧跳过 OCR
        self._last_view = None     # 上一份解析结果：同帧时复用它重新提交判定
        self.ignore_hwnd = None
        self.names = None
        self.titles = None
        self.last_tick_at = time.time()
        self._same_hash = 0

    def _self_occluded(self, w) -> bool:
        """面板矩形是否压住了微信的**消息区**（压聊天区才算，压输入框不算）。

        矩形相交就挡：宁可这一轮不判，也不能把面板上的字当成群消息。
        消息区取上一帧量出来的版面（最准），第一帧用文档里记的实测回落值。
        """
        if not self.ignore_hwnd:
            return False
        pr = ww.window_rect(self.ignore_hwnd)
        if not pr:
            return False
        lay = getattr(self._last_view, "layout", None)
        ml, mt, mr, mb = wc.message_area(w.rect, lay)
        ox = min(pr[2], mr) - max(pr[0], ml)
        oy = min(pr[3], mb) - max(pr[1], mt)
        if ox <= 8 or oy <= 8:
            return False                  # 8px 容差：贴边不算压
        # **重叠很小就不算遮挡**：小球（52px）压在消息区上约 1%，它是一张圆图、没有文字，
        # 读进去也污染不了输入；而按"相交即拦"会让球所在的位置一直扫不了
        # （真机：用户把微信拖到右边，球正好落在消息区里 → 面板一直报"面板压住了消息区"）。
        area_ov = ox * oy
        area_msg = max(1, (mr - ml) * (mb - mt))
        if area_ov / area_msg < 0.02:
            return False
        return True

    def _block(self, why: str) -> None:
        """只在原因变化时上报一次，避免每 2 秒刷同一条。"""
        if why != self._last_block:
            self._last_block = why
            dbg(f"扫描阻塞：{why}")
            self.blocked.emit(why)

    def stop(self) -> None:
        self._stop = True

    def run(self) -> None:
        while not self._stop:
            t0 = time.time()
            info = {"skip": False, "ocr_ms": 0, "people": 0}
            if getattr(self, "paused", False):
                # 暂停/免打扰：**不截屏、不 OCR、不调 API**（F-21 的"立即停捕获与网络"）。
                # 仍然发 tick：一是状态行要显示"已暂停"，二是看门狗不能把它当成卡死重启。
                self._block(f"已暂停（{HOTKEY_TEXT} 继续）")
                info.update(skip=True, paused=True, total=int((time.time() - t0) * 1000),
                            period=SCAN_PERIOD_MS)
                self.last_tick_at = time.time()
                try:
                    self.tick.emit(info)
                except Exception:
                    pass
                self.msleep(400)
                continue
            try:
                w = ww.find_main()
                if not w or w.minimized or not w.visible:
                    self._block("找不到可见的微信窗口")
                elif self._self_occluded(w):
                    # **自遮挡**：面板自己压住了消息区。这一条必须挡在截屏之前 ——
                    # 面板是置顶的，截屏会把面板上的字（"群里的人·点一个人看判断"…）
                    # 当成会话标题/消息读进来，然后发给模型并写进存档。
                    # 实测真发生过：group_meta.json 里出现了叫
                    # "、、品群里的人：点一个人着判断，，判新注府2" 的会话。
                    self._block("面板压住了微信消息区：把它挪开（或右键 →「收成小球」）")
                else:
                    # 关键前置检查：截屏抓的是**屏幕上该矩形的像素**，不是"微信的内容"。
                    # 微信被别的窗口盖住时，OCR 会读到覆盖层的文字并当成群消息发给模型。
                    ignore = {self.ignore_hwnd} if self.ignore_hwnd else set()
                    occ, why = ww.occlusion(w.hwnd, w.rect, ignore=ignore)
                    if occ:
                        self._block(f"微信窗口被遮挡：{why}")
                    else:
                        self._last_block = None
                        img = wc.grab(w.rect)
                        h = wc.frame_hash(img)
                        if h == self._last_hash:
                            # 同帧 → 整轮跳过 OCR。OCR 实测 3.8s/轮，而群聊多数时间画面不变，
                            # 这一步把平均负担降一个量级。
                            #
                            # 但**不能永远信哈希**：实测出现过面板一直显示"画面未变"、
                            # 而屏幕上内容其实变了（面板从此再不更新，用户的原话是"卡住了，
                            # 内容变了但没识别"）。所以连续 N 轮同一帧就强制真识别一次 ——
                            # 代价是每 N 轮多花一次 OCR（约 4s），换来"永不长期失明"。
                            self._same_hash = getattr(self, "_same_hash", 0) + 1
                            if self._same_hash == SAME_HASH_LOG:
                                dbg(f"画面连续 {SAME_HASH_LOG} 轮未变（hash={h[:12]}）")
                            if self._same_hash >= SAME_HASH_FORCE:
                                dbg(f"画面连续 {self._same_hash} 轮未变 → 不信哈希，强制重识别一次")
                                self._same_hash = 0
                                self._last_hash = None      # 走下面的真识别分支
                            #
                            # 但**跳过必须仍然把结果回放出去**：去抖要求"连续两次看到同一签名"
                            # 才算稳定，而画面不变时若整轮不回调，第二次确认永远不会到来 ——
                            # 两个优化会互相死锁（实测：日志只有一行"等待稳定"，永不提交）。
                            # 帧哈希相同本身就是最强的稳定证据，直接复用上一份结果即可。
                            info["skip"] = True
                            if self._last_view is not None:
                                self.scanned.emit(self._last_view)
                        else:
                            self._last_hash = h
                            self._same_hash = 0
                            t_ocr = time.time()
                            view = wc.parse(img, self.names, self.titles)
                            self._last_view = view
                            info["ocr_ms"] = int((time.time() - t_ocr) * 1000)
                            info["people"] = len(view.speakers())
                            self.scanned.emit(view)
            except Exception as e:
                # 不能静默吞：界面只会"没反应"，真因全被吃掉
                self._errs += 1
                dbg(f"扫描异常 #{self._errs}: {type(e).__name__}: {e}")
                if self._errs <= 3:
                    self.failed.emit(f"{type(e).__name__}: {e}")

            # 周期修正：睡"周期 - 本轮耗时"，而不是固定睡一个周期
            elapsed_ms = (time.time() - t0) * 1000
            info["total"] = int(elapsed_ms)
            info["period"] = SCAN_PERIOD_MS
            self.last_tick_at = time.time()      # 看门狗用它判断"有没有在推进"
            try:
                self.tick.emit(info)
            except Exception as e:               # 槽里炸了不能把扫描循环带走
                dbg(f"tick 槽异常: {type(e).__name__}: {e}")
            remain = SCAN_PERIOD_MS - elapsed_ms
            steps = max(1, int(remain // 200)) if remain > 0 else 1
            for _ in range(steps):
                if self._stop:
                    break
                self.msleep(200 if remain > 0 else 0)


# --------------------------------------------------------------------------
# API 线程：两段式分诊
# --------------------------------------------------------------------------
class TriageWorker(QThread):
    done = Signal(dict)
    failed = Signal(str)
    stage = Signal(dict)           # {stage, i, k, note}
    msg_done = Signal(dict)        # 单条消息判断的结果（用户点消息触发）

    def __init__(self, parent=None):
        super().__init__(parent)
        self._stop = False
        # 队列化而不是单槽（§6.6 复核）：单槽 `self._job = job` 会让自动分诊
        # 在跑时用户点人补的那次细判被下一轮扫描**静默顶掉** —— 右栏挂着
        # "正在细判…"，日志里一个字都没有。deque 的 append/popleft 在 GIL 下
        # 是原子的，GUI 线程与 worker 线程共用安全。
        self._jobs: deque = deque()
        self._msg_job = None       # 单条消息判断：单槽（用户点的最后一条为准）
        self._QUEUE_MAX = 8
        self._busy = False
        self.client = None
        self.profile = SpeakerProfile()
        self.throttle = Throttle()
        self.cold_start = True

    def submit_msg(self, job) -> None:
        """提交一条"判这条消息"的任务（用户主动点 → 用单槽，不挤占分诊队列）。"""
        self._msg_job = job

    def drop_jobs(self) -> None:
        """丢掉排队中的分诊（F-21 暂停时要"立即停网络"，排队的活也不能再发）。"""
        try:
            self._jobs.clear()
            self._msg_job = None
        except Exception:
            pass

    def submit(self, job) -> None:
        """提交一次分诊任务（FIFO 排队，满了丢最旧的并留痕）。"""
        if len(self._jobs) >= self._QUEUE_MAX:
            dbg(f"分诊队列已满（{self._QUEUE_MAX}），丢弃最旧的一条待办任务")
            self._jobs.popleft()
        self._jobs.append(job)

    def stop(self) -> None:
        self._stop = True

    def run(self) -> None:
        try:
            self.client = JevClient(
                load_api_key(ENV_PATH),
                audit_path=AUDIT_PATH,
                caller="hud", verbose=False)
        except Exception as e:
            self.failed.emit(f"初始化失败：{e}")
            return
        while not self._stop:
            if self._msg_job is not None and not self._busy:
                job, self._msg_job = self._msg_job, None
                self._busy = True
                try:
                    group, viewer, speaker, msg, context = job
                    self.stage.emit({"stage": "细判", "i": 0, "k": 0,
                                     "note": f"单条消息：{speaker}"})
                    before = self.client.total_input_tokens
                    r = analyse_msg(self.client, group=group, viewer=viewer,
                                    speaker=speaker, msg=msg, context=context,
                                    profile=self.profile)
                    if not r.get("cached"):
                        self.throttle.record((self.client.total_input_tokens - before)
                                             / 1e6 * 0.042)
                    r["cost_today"] = self.throttle.cost_today
                    r["calls_hour"] = self.throttle.calls_this_hour
                    self.msg_done.emit(r)
                except Exception as e:                 # noqa: BLE001
                    self.failed.emit(f"单条消息判断失败：{type(e).__name__}: {e}")
                finally:
                    self._busy = False
                continue
            if not self._jobs or self._busy:
                self.msleep(100)
                continue
            job = self._jobs.popleft()
            self._busy = True
            try:
                group, topic, viewer, speakers, context = job[:5]
                history = job[5] if len(job) > 5 else {}
                self.stage.emit({"stage": "粗筛", "i": 0, "k": 0,
                                 "note": f"对 {len(speakers)} 人排序"})
                r = triage(self.client, group=group, topic=topic, viewer=viewer,
                           speakers=speakers, context=context, history=history,
                           profile=self.profile, throttle=self.throttle,
                           cold_start=self.cold_start,
                           on_stage=lambda i, k, note: self.stage.emit(
                               {"stage": "细判", "i": i, "k": k, "note": note}))
                r["cost_today"] = self.throttle.cost_today
                r["calls_hour"] = self.throttle.calls_this_hour
                r["total_records"] = self.client.audit.records
                self.cold_start = False
                self.done.emit(r)
            except Exception as e:
                dbg(f"分诊异常: {type(e).__name__}: {e}")
                self.failed.emit(f"{type(e).__name__}: {e}")
                traceback.print_exc()
            finally:
                self._busy = False


# --------------------------------------------------------------------------
class HudBar(QWidget):
    def __init__(self):
        super().__init__()
        self.setWindowFlags(
            Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint | Qt.Tool
            | Qt.WindowDoesNotAcceptFocus)
        self.setAttribute(Qt.WA_TranslucentBackground)
        self.setAttribute(Qt.WA_ShowWithoutActivating)
        self.setWindowTitle(WINDOW_TITLE)

        self.hwnd = None
        self.pinned = self.dragging = False
        self.drag_pos = QPoint()
        self.store = GroupStore()
        self.group = ""
        self.topic = ""
        self.viewer: dict = {}
        # 归因与标题需要跨轮记忆（见 wechat_capture 的说明）
        self.names = wc.NameMemory()
        self.titles = wc.TitleMemory()
        self._last_hint = None
        self.last_sig = None
        self._pending_sig = None      # 去抖：需连续两次看到同一签名才提交
        # 节流必须**按会话**记（常量注释与设置文案都写着"同一会话最小重判间隔"）：
        # 以前是一个全局浮点，切群会沿用上一个群的 20s 窗口 → 用户报"切群卡住"
        self._last_triage_by_conv: dict = {}
        self._skip_debounce = False        # 刚换了会话 → 首帧直接提交，不等"确认稳定"
        self._conv_results: dict = {}      # 会话键 → 上一轮结果（内存 LRU，不落盘）
        self._conv_id = ""                 # 会话键里那个"稳定读数"（见 _conv_key：抗 OCR 抖动）
        self._risk_pct = False             # 风险区块显示：False=高/中/低（默认），True=具体概率
        self._submitted_at = 0.0           # 埋点：提交 → 结果渲染 的耗时
        self._last_people: set = set()
        self._speaker_msgs: dict = {}
        self._last_ctx: list = []   # 上一轮的发言人集合，用于"出现新人"判定
        self.rows: dict[str, PersonRow] = {}
        self.results: dict[str, dict] = {}
        self.selected = None
        # 详情区默认就是展开的（右栏一开始就看得见），所以初值必须是 True：
        # 之前写 False，而 QScrollArea 建出来本来可见 —— 面板显示"已展开"、
        # 图标却是"▾"，第一次点它只换个图标、什么都不发生，看着像按钮坏了。
        self._expanded = True
        # 形态：面板 ↔ 小球。同一个顶层窗口切形态，不开第二个窗口 ——
        # 置顶、不抢焦点、单实例、位置跟随这些机制不用维护两套。
        self.prefs = UiPrefs()
        self.ball_mode = bool(self.prefs.get("ball_mode", False))
        self._hide_noted = False        # "已收成小球"的托盘气泡每次运行只提示一次
        self._worker_dead = ""          # 判断线程不可用的原因（报过一次就不再刷）
        self._worker_stuck_since = 0.0  # 有排队但线程没跑，从什么时候开始的
        self.ball_xy = self.prefs.get("ball_xy")
        self._ball_bad = None       # 扫描出问题时的原因（球变红）
        self._ball_qss = None       # 上次的样式，避免每 3 秒重复刷样式表
        self._seen_expand_at = self.prefs.get("expand_at")
        self._expand_checked = 0.0
        self._press_at = 0.0
        self._press_xy = QPoint()
        self._moved = 0
        self._build()
        self.set_ball(self.ball_mode, save=False, first=True)

        self.scanner = Scanner(self)
        self.scanner.scanned.connect(self.on_scan)
        self.scanner.failed.connect(self.on_scan_failed)
        self.scanner.blocked.connect(self.on_scan_blocked)
        self.scanner.tick.connect(self.on_tick)
        # 面板自己是置顶的；用户把它拖到微信上时不应被判定为"遮挡"。
        # winId() 会按需创建原生窗口，此处调用是安全的。
        try:
            self.scanner.ignore_hwnd = int(self.winId())
        except Exception as e:
            dbg(f"取自身句柄失败: {e}")
        self.scanner.names = self.names
        self.scanner.titles = self.titles
        self.scanner.start()

        self.worker = TriageWorker(self)
        self.worker.done.connect(self.on_triage)
        # 启动就先看一眼 Key：没有它，之后每次判断都会"无声地等待"（打包版最容易踩：
        # 用户只拷了 exe 目录、忘了放 .env）。这里直接写在状态行上，别让人等到"卡住"。
        try:
            from .jev_engine import load_api_key as _load_key
            _load_key(ENV_PATH)
        except Exception as _e:                      # noqa: BLE001
            self._worker_dead = f"缺少 API Key（{_e}）"
            QTimer.singleShot(0, lambda: self._hint(
                f"缺少 API Key：把 .env 放在 {PROJECT_DIR} 里（一行 jevkey=apikey_…），否则不会有判断",
                "#dc2626"))
        self.worker.msg_done.connect(self.on_msg_done)
        self.worker.failed.connect(self.on_fail)
        self.worker.stage.connect(self.on_stage)
        self.worker.start()

        self.t_pos = QTimer(self)
        self.t_pos.timeout.connect(self.follow)
        self.t_pos.start(POLL_MS)

    # ---------------- UI ----------------
    def _build(self):
        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        self.card = QFrame()
        self.card.setObjectName("card")
        root.addWidget(self.card)
        # 收起态的小球：和面板同一个顶层窗口，只是换个形态
        self.ball = QLabel("", self)
        self.ball.setObjectName("ball")
        self.ball.setFixedSize(BALL, BALL)
        self.ball.setAlignment(Qt.AlignCenter)
        self.ball.setVisible(False)
        root.addWidget(self.ball, 0, Qt.AlignCenter)
        v = QVBoxLayout(self.card)
        v.setContentsMargins(13, 9, 13, 9)
        v.setSpacing(7)

        head = QHBoxLayout()
        head.setSpacing(7)
        self.dot = QLabel("●")
        self.dot.setObjectName("dot")
        self.title = QLabel("等待微信")
        self.title.setObjectName("title")
        self.b_state = QLabel("")
        self.b_state.setObjectName("bstate")
        self.b_cost = QLabel("")
        self.b_cost.setObjectName("bcost")
        self.btn_me = QPushButton("填群昵称")
        self.btn_me.setObjectName("link")
        self.btn_me.clicked.connect(self.ask_nickname)
        # 日志按用户要求"全量永久保留"且含完整聊天原文，必须能看见占用并一键清掉
        self.btn_log = QPushButton("日志")
        self.btn_log.setObjectName("link")
        self.btn_log.setToolTip("审计日志占用；点两次清空")
        self.btn_log.clicked.connect(self.on_log_click)
        self._log_clear_armed = 0.0
        self.btn_pin = QPushButton("📌")
        self.btn_pin.setObjectName("icon")
        self.btn_pin.setFixedSize(ICON_BTN, ICON_BTN)
        self.btn_pin.setCheckable(True)
        self.btn_pin.setToolTip("固定位置（不再自动吸附）")
        self.btn_pin.toggled.connect(self.set_pinned)
        # 「关闭展板」（用户 2026-09-24）：**只隐藏面板，不退出程序**。
        # 为什么不做成"退出"：✕ 就是因为误触一次整个面板没了才被删掉的（09-23）；
        # 隐藏的代价只是再点一下托盘，退出仍走右键菜单「退出」。
        self.btn_hide = QPushButton("关闭")
        self.btn_hide.setObjectName("link")
        self.btn_hide.setToolTip("关闭展板：收成小球（面板收起、球留在屏幕上，点球或双击托盘展开；"
                                 "退出程序请用右键菜单「退出」）")
        self.btn_hide.clicked.connect(self.hide_panel)
        # 标题栏不放「退出」（误触一次面板就没了）、不放「收成小球」（用户："没啥用"）、
        # 也不放「▴ 收起详情」（用户 2026-09-24："去除缩小"）—— 三者都留在菜单里：
        # 退出与收起详情在右键菜单，收成小球在右键/托盘菜单 + 双击托盘。
        for w in (self.dot, self.title, self.b_state, self.b_cost,
                  self.btn_me, self.btn_log):
            head.addWidget(w)
        head.addStretch(1)
        for w in (self.btn_pin, self.btn_hide):
            head.addWidget(w)
        v.addLayout(head)

        # ---- 进度行：让用户随时知道"它在干什么、到哪一步了" ----
        # 没有它时，面板在扫描/判断期间看起来和卡死没有区别 ——
        # 用户能看到的只有左列空白，而空白既可能是"正在判断"也可能是"已经判完、没人需要你"。
        prow = QHBoxLayout()
        prow.setSpacing(8)
        self.bar = QProgressBar()
        self.bar.setObjectName("bar")
        self.bar.setRange(0, 100)
        self.bar.setValue(0)
        self.bar.setTextVisible(False)
        self.bar.setFixedHeight(4)
        self.status = QLabel("正在启动…")
        self.status.setObjectName("status")
        # 未填"决定性输入"时的提示：单独一个 label，不去挤 status 的文字
        # （status 每轮扫描都在变，混在一起写会互相盖掉）
        self.nudge = QLabel("")
        self.nudge.setObjectName("nudge")
        self.nudge.setVisible(False)
        prow.addWidget(self.bar, 1)
        prow.addWidget(self.status)
        prow.addWidget(self.nudge)
        v.addLayout(prow)
        self._stage = "启动"
        self._stage_at = time.time()
        self._last_done = None
        self._scan_info = {}

        bh = QHBoxLayout()
        bh.setContentsMargins(0, 2, 0, 0)
        bh.setSpacing(12)      # 16px 的列间距会把右栏挤出面板（见右栏最小宽度那句）

        left = QWidget()
        self.left_panel = left                  # 私聊里整列隐掉（只有一个人，列表没有意义）
        lv = QVBoxLayout(left)
        lv.setContentsMargins(0, 0, 0, 0)
        lv.setSpacing(3)
        lq = QLabel("群里的人　·　点一个人看判断")
        lq.setObjectName("sec")
        self.l_head = lq                       # 措辞按场景换（私聊没有"群里的人"）
        lv.addWidget(lq)
        # 列头（用户截图问"这些值是干什么用的"）：`PersonRow` 的五列宽度是写死的，
        # 但从来没人告诉用户哪列是什么 —— 于是 `安全 / 应该回 / 1/10 / 信息不足`
        # 只能靠猜。这里给一行列头，列宽**逐个对齐** PersonRow 的 14/118/96/76/46，
        # 第二列与最后一列的文案随场景换（私聊里第二列放的是危险等级文字）。
        self.l_cols = QWidget()
        ch = QHBoxLayout(self.l_cols)
        ch.setContentsMargins(7, 0, 7, 0)      # 与 PersonRow 一致（对齐的前提）
        ch.setSpacing(ROW_SPACING)
        self._l_col_labels = []                # [(QLabel, 该列包含的宽度)]
        for txt, w in (("昵称", ROW_MARK_W + ROW_SPACING + ROW_NICK_W),
                       ("在群里的角色", ROW_ROLE_W),
                       ("要不要回", ROW_WORTH_W), ("关注度", ROW_ATTN_W)):
            lb = QLabel(txt)
            lb.setObjectName("colhead")
            lb.setFixedWidth(w)
            ch.addWidget(lb)
            self._l_col_labels.append(lb)
        ch.addStretch(1)
        self.l_risk_head = QLabel("风险信号")   # 徽标贴右边缘 → 列头也跟着靠右
        self.l_risk_head.setObjectName("colhead")
        self.l_risk_head.setAlignment(Qt.AlignCenter)
        self.l_risk_head.setMinimumWidth(ROW_CHIP_MIN_W)
        self.l_risk_head.setMaximumWidth(ROW_CHIP_W)
        ch.addWidget(self.l_risk_head)
        lv.addWidget(self.l_cols)
        self.listbox = QVBoxLayout()
        self.listbox.setSpacing(1)
        lv.addLayout(self.listbox)
        self.hint = QLabel("正在读取微信窗口…")
        self.hint.setObjectName("muted")
        lv.addWidget(self.hint)
        lv.addStretch(1)
        bh.addWidget(left, 3)

        right = QWidget()
        self.right_panel = right
        # 这里原本是 272：面板宽度被迫等于微信宽度（真机 657）时，左列最小宽度 + 272 装不下，
        # 布局只能把右栏往右顶 —— 实测右栏右边缘到 747px、超出面板 90px，
        # **所有"值"（要个解释 / 1/9 安全 / 判不了）都落在面板外面看不见**，只剩左边的标签。
        # 现在改成 230：够放下"两列键值"（每格 ~100px），也让两列在 640 宽的面板里装得下。
        right.setMinimumWidth(230)
        rv = QVBoxLayout(right)
        rv.setContentsMargins(0, 0, 0, 0)
        rv.setSpacing(3)
        self.r_head = QLabel("判断")
        self.r_head.setObjectName("sec")
        self.r_body = QVBoxLayout()
        self.r_body.setSpacing(3)
        self.r_hint = QLabel("点左边任意一个人，这里显示模型对这个人的判断。")
        self.r_hint.setObjectName("muted")
        self.r_hint.setWordWrap(True)
        rv.addWidget(self.r_head)
        rv.addLayout(self.r_body)
        rv.addWidget(self.r_hint)
        rv.addStretch(1)
        bh.addWidget(right, 2)

        self.body_host = QWidget()
        self.body_host.setLayout(bh)
        self.scroll = QScrollArea()
        self.scroll.setWidgetResizable(True)
        self.scroll.setFrameShape(QFrame.NoFrame)
        self.scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.scroll.setWidget(self.body_host)
        self.scroll.setMinimumHeight(170)
        v.addWidget(self.scroll)

        self.foot = QLabel("本机处理 · 只把群成员的最近几条消息发给 Jev 判断")
        self.foot.setObjectName("foot")
        v.addWidget(self.foot)
        # 右下角拖拽把手：用户自己定面板大小（此前尺寸完全由内容驱动，用户没法压缩）
        self.grip = QSizeGrip(self.card)
        self.grip.setToolTip("拖动可以调整面板大小（右键菜单可恢复自适应）")
        v.addWidget(self.grip, 0, Qt.AlignRight)
        self.grip.installEventFilter(self)
        self._user_resizing = False
        self.paused = False            # F-21 暂停/免打扰
        self.hist: dict[str, deque] = {}   # 当前会话：每人累积的历史（屏上滚掉的也留着）
        self.msg_sel = None        # 点了哪条消息：(发言人, 内容)；None = 显示按人判断
        self.msg_result = None     # 那条消息的判断结果
        self.hist_all: dict = self._load_hist()   # 所有会话的历史（落盘用）
        self._hist_dirty = False
        self.tray = None

        self.setStyleSheet("""
        #card{background:rgba(250,250,250,244);border:1px solid #d2d2d2;
              border-radius:11px;}
        #dot{font-size:11px;color:#22a06b}
        #title{font-size:12.5px;font-weight:600;color:#1a1a1a}
        #bstate{font-size:10px;color:#fff;background:#9a9a9a;border-radius:3px;
                padding:1px 6px}
        #bcost{font-size:10px;color:#6b6b6b;background:#ececec;border-radius:3px;
               padding:1px 6px}
        #sec{font-size:10.5px;font-weight:700;color:#8a8a8a;letter-spacing:.04em}
        #muted{font-size:10.5px;color:#9a9a9a;line-height:150%}
        /* 左列表头：比正文更小更淡，宽度与 PersonRow 的各列一一对齐 */
        QLabel#colhead{font-size:10px;color:#a8a8a8}
        #foot{font-size:9.5px;color:#b0b0b0}
        QLabel#role{font-size:11.5px;color:#3d3d3d}
        QLabel#worth{font-size:11.5px;color:#3d3d3d}
        QLabel#attn{font-size:11px;color:#111;font-weight:600}
        QLabel#risk{font-size:9.5px;color:#fff;background:#c2410c;
                    border-radius:3px;padding:1px 5px}
        QLabel#lowsuff{font-size:9.5px;color:#8a5a00;background:#fff1cc;
                       border-radius:3px;padding:1px 5px}
        QLabel#k{font-size:11px;color:#8a8a8a}
        QLabel#v{font-size:11.5px;color:#141414}
        QLabel#vmuted{font-size:11px;color:#8a8a8a}
        QLabel#vh{font-size:11.5px;font-weight:600;color:#e551ba}
        QLabel#lv{font-size:12px;font-weight:600}
        QPushButton#icon{border:none;background:rgba(0,0,0,10);color:#666;
                         font-size:16px;border-radius:6px}
        QPushButton#icon:hover{color:#111;background:#e4e4e4}
        QPushButton#icon:checked{color:#c2410c;background:#ffe8d9;
                                 border-radius:4px}
        QPushButton#link{border:none;background:#fff1cc;color:#8a5a00;
                         border-radius:3px;padding:2px 7px;font-size:10.5px}
        QPushButton#link:hover{background:#ffe4a8}
        QPushButton#link_hot{border:1px solid #e08a2e;background:#fff4d6;
                             color:#8a4b00;border-radius:3px;padding:1px 7px;
                             font-size:10.5px;font-weight:600}
        QPushButton#link_hot:hover{background:#ffe4a8}
        #nudge{font-size:10.5px;color:#8a5a00;background:#fff4d6;
               border-radius:3px;padding:1px 6px}
        QLabel#ball{border-radius:26px;font-size:16px;font-weight:700;
                    color:#fff;background:#b0b0b0}
        QProgressBar#bar{background:#e8e8e8;border:none;border-radius:2px}
        QProgressBar#bar::chunk{background:#3f9e63;border-radius:2px}
        #status{font-size:10.5px;color:#8a8a8a}
        QScrollArea{background:transparent;border:none}
        QScrollBar:vertical{width:6px;background:transparent}
        QScrollBar::handle:vertical{background:#cfcfcf;border-radius:3px}
        QScrollBar::add-line,QScrollBar::sub-line{height:0}
        QMenu{background:#fbfbfb;border:1px solid #d2d2d2;padding:4px}
        QMenu::item{padding:5px 18px;font-size:12px}
        QMenu::item:selected{background:#e8f0fe}
        """)
        self.setFixedWidth(880)
        self._apply_fold()      # 图标要和"默认展开"的实际状态对上
        self._setup_tray()      # F-21：托盘入口
        self._register_hotkey()  # F-21：全局热键

    def _sync_scroll_height(self) -> None:
        """让滚动容器真的可滚。

        setWidgetResizable(True) 会把内容压到视口大小，于是内容永不溢出、
        永远没有滚动条，超出部分被直接裁掉（实测像素差 0.00）。
        解决：把内容控件的最小高度设为布局的实际需要高度。
        """
        lay = self.body_host.layout()
        if lay is None:
            return
        lay.activate()
        need = lay.sizeHint().height()
        if need <= 0 or self.body_host.minimumHeight() == need:
            return
        # 状态变化时记一笔：排查"为什么面板没有滚动条/为什么要滚动"时，先看这一行。
        # 面板高度现在是跟着内容走的（见 follow()），所以正常情况下应该是"一屏放下"。
        vp_h = self.scroll.viewport().height()
        now = ("内容 %d > 视口 %d → 需要滚动" % (need, vp_h) if need > vp_h
               else "内容 %d ≤ 视口 %d → 一屏放下" % (need, vp_h))
        if getattr(self, "_scroll_note", None) != now:
            self._scroll_note = now
            dbg("滚动状态：" + now)

        # 内容变长后面板会变高；不处理的话滚动位置会停在中间，看着像"跳了一下"。
        # 在底部就留在底部（最常见：正在看最新结论），否则按比例保持相对位置。
        bar = self.scroll.verticalScrollBar()
        old_max, old_val = bar.maximum(), bar.value()
        at_bottom = old_max > 0 and old_val >= old_max - 2
        ratio = (old_val / old_max) if old_max > 0 else 0.0

        self.body_host.setMinimumHeight(need)
        new_max = bar.maximum()
        if new_max > 0:
            bar.setValue(new_max if at_bottom else int(ratio * new_max))

    # ---------------- 吸附 ----------------
    def set_ball(self, on: bool, save: bool = True, first: bool = False):
        """切换形态：False = 整块面板，True = 一颗小球。

        两态共用一个顶层窗口：收起时把窗口缩到 52x52 只留小球，
        展开时恢复原尺寸。拖动、置顶、不抢焦点、单实例都不用重做。
        """
        on = bool(on)
        self.ball_mode = on
        dbg(f"set_ball({on}) first={first}")
        if on:
            self.card.setVisible(False)
            self.ball.setVisible(True)
            self._refresh_ball()
            self.setFixedSize(BALL, BALL)
            self._place_ball()
        else:
            self.ball.setVisible(False)
            self.card.setVisible(True)
            # 先解开 setFixedSize 钉死的上下限，再交回给 follow() 按微信宽算
            self.setMinimumSize(0, 0)
            self.setMaximumSize(16777215, 16777215)
            self.setFixedWidth(880)
            self._apply_fold()
            self.adjustSize()
            self.follow()
        if save:
            self.prefs.set("ball_mode", on)
        if first:
            self.setVisible(False)      # 等 follow() 决定露不露脸

    def _refresh_ball(self) -> None:
        """小球外观：只用颜色，不写条数（球小，数字既看不清又怕旁边有人看见）。

        优先级：扫描出问题（红）> 有 alert（橙红）> 有 todo（暗黄）> 静默（半透明灰）。
        条数放 tooltip —— 鼠标一停就看得到，不摆在屏上。
        """
        if not hasattr(self, "ball"):
            return
        states = [d.get("state") for d in self.results.values()]
        n_alert, n_todo = states.count("alert"), states.count("todo")
        if self.paused:
            bg, tip = "rgba(138,138,138,190)", f"已暂停（{HOTKEY_TEXT} 继续）"
        elif self._ball_bad:
            bg, tip = "rgba(220,38,38,205)", self._ball_bad
        elif n_alert:
            bg, tip = "rgba(194,65,12,235)", f"{n_alert} 条需要你 · 点开看"
        elif n_todo:
            bg, tip = "rgba(201,154,46,205)", f"{n_todo} 条待办 · 点开看"
        else:
            bg, tip = "rgba(176,176,176,135)", "没有需要你的消息 · 点开看"
        # 外环颜色：沿用原来那套状态色（红=扫描异常 / 橙红=alert / 暗黄=todo / 灰=静默）
        ring = {"rgba(220,38,38,205)": "#dc2626",
                "rgba(194,65,12,235)": "#c2410c",
                "rgba(201,154,46,205)": "#c99a2e",
                "rgba(138,138,138,190)": "#8a8a8a",
                "rgba(176,176,176,135)": "#b0b0b0"}.get(bg, "#b0b0b0")
        self._ball_ring = ring
        pm = self._ball_pixmap(ring, BALL)
        if not pm.isNull():
            qss = (f"QLabel#ball{{background:transparent;border-radius:{BALL // 2}px;"
                   "color:#fff;font-size:16px;font-weight:700}")
            if qss != self._ball_qss:
                self._ball_qss = qss
                self.ball.setStyleSheet(qss)
            self.ball.setText("")
            self.ball.setPixmap(pm)
        else:
            qss = (f"QLabel#ball{{background:{bg};border-radius:{BALL // 2}px;"
                   "color:#fff;font-size:16px;font-weight:700}")
            if qss != self._ball_qss:
                self._ball_qss = qss
                self.ball.setStyleSheet(qss)
            self.ball.setPixmap(QPixmap())     # 回落时清掉可能的旧贴图，别图和底色同时出现
        if self.ball.toolTip() != tip:
            self.ball.setToolTip(tip)

    def _check_scanner(self) -> None:
        """看门狗：扫描线程要么在推进，要么别静默。

        实测教训：出现过面板一直显示"画面未变"、而屏幕上内容已经变了，面板从此再不更新
        （用户："卡住了，内容变了但没识别"）。那次线程其实还活着，真正卡住的是"太信哈希"——
        已由 SAME_HASH_FORCE 兜住；这里再兜一层：线程**死了**或**不再推进**就重启，并写进调试日志。
        """
        sc = getattr(self, "scanner", None)      # follow() 可能在 scanner 建好之前就被调（set_ball → follow）
        if sc is None:
            return
        if (getattr(sc, "_stop", False) or getattr(self, "_shutting_down", False)
                or getattr(self, "paused", False)):
            return                      # 暂停期间不算"卡住"
        now = time.time()
        last = getattr(sc, "last_tick_at", None) or now
        # 容错：真身是 QThread；测试桩/替身不一定有 isRunning（别让看门狗把面板带崩）
        alive = bool(getattr(sc, "isRunning", lambda: True)())
        if alive and (now - last) < SCAN_STALL_S:
            return
        why = "扫描线程已退出" if not alive else f"扫描线程 {int(now - last)}s 没有推进"
        dbg(f"看门狗：{why} → 重启扫描线程")
        self._hint(f"{why}，已自动重启（详见 out/hud_debug.log）", "#dc2626")
        try:
            sc.stop()
            sc.wait(1500)
        except Exception:
            pass
        try:
            if isinstance(sc, Scanner):
                sc._stop = False           # QThread 跑完可以再 start()
                sc.last_tick_at = time.time()
                sc.start()
                dbg("看门狗：扫描线程已重启")
            else:
                dbg("看门狗：扫描器不是 Scanner 实例（测试桩？）→ 只记录不重启")
        except Exception as e:             # noqa: BLE001
            dbg(f"看门狗：重启失败 {type(e).__name__}: {e}")

    # ---------------- F-21 暂停 / 免打扰 ----------------
    def _conv_key(self) -> str:
        """会话键：**用稳定的那次读数**（`_conv_id`），不是当前这帧 OCR 读到的标题。

        OCR 抖动会让 `self.group` 变来变去；如果键跟着变，节流与结果缓存都会各记一份，
        等于"同一个群被当成好几个群"。`_conv_id` 只在**真的换会话**时才更新。
        """
        key = self._conv_id or self.group
        return f"{key}\u0000{self.topic}" if key else ""

    def _load_hist(self) -> dict:
        """读盘（累积历史跨重启保留）。坏了就当空，绝不让它挡住启动。"""
        try:
            import json as _json
            with open(HIST_PATH, "r", encoding="utf-8") as f:
                d = _json.load(f)
            if isinstance(d, dict):
                dbg(f"载入 {len(d)} 个会话的累积历史")
                return d
        except FileNotFoundError:
            pass
        except Exception as e:                     # noqa: BLE001
            dbg(f"累积历史载入失败（当空处理）: {type(e).__name__}: {e}")
        return {}

    def _use_conv_hist(self) -> None:
        """切到当前会话的历史（换会话时调用）。"""
        key = self._conv_key()
        slots = self.hist_all.setdefault(key, {}) if key else {}
        self.hist = {n: deque(v[-self._hist_cap():]) for n, v in slots.items()} \
            if self._hist_cap() > 0 else {}      # 引用一份可写副本，remember() 直接改它

    def _flush_hist(self) -> None:
        """把当前会话的历史写回磁盘（有变化才写）。"""
        if not self._hist_dirty:
            return
        key = self._conv_key()
        if not key:
            return
        try:
            import json as _json
            self.hist_all[key] = {n: list(dq) for n, dq in self.hist.items() if dq}
            while len(self.hist_all) > 20:          # 会话太多就丢最旧的（dict 保序）
                self.hist_all.pop(next(iter(self.hist_all)))
            os.makedirs(os.path.dirname(HIST_PATH), exist_ok=True)
            with open(HIST_PATH, "w", encoding="utf-8") as f:
                _json.dump(self.hist_all, f, ensure_ascii=False, indent=2)
            self._hist_dirty = False
        except Exception as e:                      # noqa: BLE001
            dbg(f"累积历史落盘失败: {type(e).__name__}: {e}")

    def _hist_cap(self) -> int:
        """每人累积多少条（0 = 关闭累积）。"""
        try:
            from .settings import SETTINGS
            return int(SETTINGS.get("history_msgs"))
        except Exception:
            return 12

    def remember(self, speakers) -> None:
        """把这一屏看到的、按人并进本机历史（去重 + 条数/字符双上限）。

        为什么有用：判断只看"屏幕上还看得见的"，消息一滚就没了 —— 于是"这人前后说过什么"
        模型永远看不到。累积后可以把更早的消息一起送进去（同类实现 jarvis 也有这一手）。
        只存本机内存、只在本进程活着时有效（不进磁盘、不进审计）。
        """
        cap = self._hist_cap()
        if cap <= 0:
            return
        for nick, msgs in speakers:
            if not nick or nick == "我":
                continue
            dq = self.hist.setdefault(nick, deque())
            for m in msgs:
                if not m or (dq and dq[-1] == m):
                    continue
                if m in dq:                      # 已在历史里（重复上屏）→ 提到最后
                    dq.remove(m)
                dq.append(m)
            while len(dq) > cap:
                dq.popleft()
            chars = sum(len(x) for x in dq)
            while chars > HIST_CHARS and len(dq) > 1:
                chars -= len(dq.popleft())
            self._hist_dirty = True

    def history_of(self, nick: str) -> list[str]:
        return list(self.hist.get(nick, ()))

    def open_settings(self) -> None:
        """打开设置（F-22）。保存后立即生效：节流上限即时生效，阈值同步进 qset。"""
        dlg = SettingsDialog(self)
        dlg.show()
        dlg.raise_()
        dlg.activateWindow()
        if dlg.exec() == QDialog.Accepted:
            note = getattr(dlg, "_notice", "设置已保存")
            dbg(f"设置已保存：{note}")
            self._hint(note, "#3f9e63")
        else:
            dbg("设置对话框取消")

    def toggle_pause(self, on: bool | None = None) -> None:
        """暂停（免打扰）：立即停采集与网络；再按一次继续。"""
        self.paused = (not self.paused) if on is None else bool(on)
        dbg(f"暂停切换 → paused={self.paused}")
        try:
            self.scanner.paused = self.paused
            if self.paused:
                self.worker.drop_jobs()          # 排队中的分诊也一并丢掉
        except Exception as e:
            dbg(f"设置暂停失败: {type(e).__name__}: {e}")
        if self.paused:
            self.dot.setStyleSheet("color:#8a8a8a")
            self._hint(f"已暂停（{HOTKEY_TEXT} 或托盘菜单继续）")
            self.set_progress("待命", 0, f"已暂停 · 免打扰中（{HOTKEY_TEXT} 继续）")
        else:
            self.dot.setStyleSheet("color:#22a06b")
            self._hint("已恢复采集", "#3f9e63")
            self.last_sig = None                 # 恢复后强制重新判一次
            self.set_progress("扫描", 5, "已恢复 · 扫描中")
        self._refresh_ball()
        self._refresh_tray()

    def _ball_pixmap(self, ring: str, size: int = BALL) -> QPixmap:
        """把 assets/ball.png 裁成圆形当球体，边缘画一圈状态色环。

        - 没有这个图片就返回空 —— 调用方回落到"纯色球"（老行为，绝不让外观挡住功能）
        - 状态语义保持不变，只是从"整球底色"改成"外环颜色"（球上依然是用户的图）
        """
        pm = QPixmap(size, size)
        pm.fill(Qt.transparent)
        if not os.path.exists(BALL_IMG):
            return QPixmap()
        src = QPixmap(BALL_IMG)
        if src.isNull():
            return QPixmap()
        p = QPainter(pm)
        p.setRenderHint(QPainter.Antialiasing)
        p.setRenderHint(QPainter.SmoothPixmapTransform)
        # 圆形裁切 + 居中缩放（按短边铺满，多余的裁掉）
        path = QPainterPath()
        path.addEllipse(1.5, 1.5, size - 3, size - 3)
        p.setClipPath(path)
        scaled = src.scaled(size, size, Qt.KeepAspectRatioByExpanding,
                            Qt.SmoothTransformation)
        p.drawPixmap((size - scaled.width()) // 2, (size - scaled.height()) // 2, scaled)
        p.setClipping(False)
        # 状态色外环（原来整球底色的语义）
        p.setBrush(Qt.NoBrush)
        p.setPen(QPen(QColor(ring), 3))
        p.drawEllipse(1.5, 1.5, size - 3, size - 3)
        p.end()
        return pm

    def _tray_icon(self):
        """托盘图标：同一个球（图片 + 状态色环），缩到 32px。"""
        ring = getattr(self, "_ball_ring", None) or (
            "#8a8a8a" if self.paused else "#c2410c")
        pm = self._ball_pixmap(ring, 32)
        if pm.isNull():                      # 没有图片就退回纯色圆点
            pm = QPixmap(32, 32)
            pm.fill(Qt.transparent)
            p = QPainter(pm)
            p.setRenderHint(QPainter.Antialiasing)
            p.setBrush(QColor(ring))
            p.setPen(QColor(255, 255, 255, 200))
            p.drawEllipse(4, 4, 24, 24)
            p.end()
        return QIcon(pm)

    def _setup_tray(self) -> None:
        """托盘：暂停/继续、收起/展开、退出（F-21 要求"托盘"入口）。"""
        try:
            if not QSystemTrayIcon.isSystemTrayAvailable():
                dbg("系统托盘不可用 → 跳过托盘（热键仍可用）")
                return
            self.tray = QSystemTrayIcon(self._tray_icon(), self)
            self.tray.setToolTip("微信分诊面板")
            self.tray.activated.connect(self._on_tray_activated)
            self._refresh_tray()
            self.tray.show()
            dbg("托盘图标已就绪")
        except Exception as e:                   # noqa: BLE001
            dbg(f"托盘初始化失败: {type(e).__name__}: {e}")

    def _refresh_tray(self) -> None:
        if self.tray is None:
            return
        m = QMenu()
        a1 = m.addAction("继续采集" if self.paused else "暂停采集（免打扰）")
        a1.triggered.connect(lambda: self.toggle_pause())
        a2 = m.addAction("展开面板" if self.ball_mode else "收成小球")
        a2.triggered.connect(lambda: self.set_ball(not self.ball_mode))
        a4 = m.addAction("设置…")
        a4.triggered.connect(self.open_settings)
        m.addSeparator()
        a3 = m.addAction("退出")
        a3.triggered.connect(self._on_close)
        self.tray.setContextMenu(m)
        self.tray.setIcon(self._tray_icon())
        state = "小球形态" if self.ball_mode else ("已暂停" if self.paused else "采集中")
        self.tray.setToolTip(f"微信分诊面板（{state}）")
        self._tray_menu = m                         # 防 GC

    def hide_panel(self, note: bool = True) -> None:
        """「关闭」= **收成一颗小球**（面板本体收起，球留在屏幕上）。

        为什么不是「彻底隐藏」：那版真机一试就露馅 —— 关掉之后屏幕上什么都没有，
        用户的原话是「小球看不见」，还得双击托盘才找得回来（日志：00:09:40 关闭 →
        00:09:47 召回 → 00:09:51 又关闭）。球才是最小形态该有的样子：留着、能点开、
        外环颜色就是状态。想彻底退出走右键菜单「退出」。
        """
        dbg("关闭展板 → 收成小球（面板收起，球留在屏幕上）")
        self.set_ball(True)
        self._refresh_tray()
        if note and not self._hide_noted:
            self._hide_noted = True        # 每次运行只提示一次
            if self.tray is not None:
                try:
                    self.tray.showMessage("已收成小球",
                                          "点球或双击托盘图标展开；退出程序请右键面板 →「退出」。")
                except Exception:
                    pass

    def show_panel(self) -> None:
        """把面板显示出来（双击托盘 / 托盘菜单 / 另一实例启动都走这里）。"""
        self.show()
        self._refresh_tray()
        dbg("召回面板")

    def _on_tray_activated(self, reason) -> None:
        if reason == QSystemTrayIcon.DoubleClick:
            # 双击托盘 = 收起/展开（球 ↔ 面板）
            self.show_panel()
            self.set_ball(not self.ball_mode)

    def _register_hotkey(self) -> None:
        """全局热键 Ctrl+Alt+H：面板隐藏/小球/被遮挡时都能按到（F-21）。"""
        try:
            u32 = ctypes.windll.user32
            ok = u32.RegisterHotKey(int(self.winId()), HOTKEY_ID,
                                    MOD_CONTROL | MOD_ALT, VK_H)
            if ok:
                dbg(f"全局热键已注册：{HOTKEY_TEXT}")
            else:
                dbg(f"全局热键注册失败（err={ctypes.get_last_error()}）——"
                    f"多半是已被别的程序占用，托盘菜单仍可用")
        except Exception as e:                   # noqa: BLE001
            dbg(f"注册热键异常: {type(e).__name__}: {e}")

    def nativeEvent(self, event_type, message):
        """接 Windows 的 WM_HOTKEY（Qt 不转发它，只能自己在原生事件里捞）。"""
        try:
            if event_type == b"windows_generic_MSG":
                import ctypes.wintypes as wt
                msg = ctypes.cast(int(message), ctypes.POINTER(wt.MSG)).contents
                if msg.message == WM_HOTKEY and msg.wParam == HOTKEY_ID:
                    dbg("收到全局热键 → 切换暂停")
                    self.toggle_pause()
                    return True, 0
        except Exception as e:                   # noqa: BLE001
            dbg(f"nativeEvent 解析失败: {type(e).__name__}: {e}")
        return super().nativeEvent(event_type, message)

    def _check_expand_request(self) -> None:
        """另一个进程又启动了面板 → 它会在存档里写下 expand_at，这里认领并展开。

        没有这一步时，收起成小球后重复启动 exe，用户看到的是"什么都没发生"
        （旧逻辑只会 ShowWindow+SetForegroundWindow 一个 52px 的球）。
        """
        now = time.time()
        if now - self._expand_checked < 2.0:      # follow() 每 600ms 一次，别每轮都读盘
            return
        self._expand_checked = now
        try:
            req = self.prefs.reload().get("expand_at")
        except Exception:
            return
        if req and req != self._seen_expand_at:
            self._seen_expand_at = req          # 先记账，避免 set_ball→follow 再认领一次
            dbg("收到另一实例的展开请求")
            self.show_panel()                   # 被"关闭展板"藏起来时，这也是召回路径
            self.set_ball(False)

    def _place_ball(self) -> None:
        """小球位置：用户拖过就用他给的；否则贴着微信右下角外侧，再夹进屏幕。"""
        scr = QGuiApplication.primaryScreen().availableGeometry()
        x, y = None, None
        if isinstance(self.ball_xy, (list, tuple)) and len(self.ball_xy) == 2:
            x, y = int(self.ball_xy[0]), int(self.ball_xy[1])
        else:
            w = ww.find_main(self.hwnd)
            if w and not w.minimized and w.visible:
                l, t, r, b = w.rect
                x, y = (r + BALL_GAP, b - BALL) if r + BALL_GAP + BALL <= scr.right() \
                    else (min(r, scr.right()) - BALL - BALL_GAP, b + BALL_GAP)
        if x is None:
            x, y = scr.right() - BALL - 4, scr.top() + 4
        x = max(scr.left(), min(int(x), scr.right() - BALL))
        y = max(scr.top(), min(int(y), scr.bottom() - BALL))
        if self.x() != x or self.y() != y:
            self.move(x, y)
        self.ball_xy = [x, y]

    def follow(self):
        # 自愈：PersonRow 会被重建销毁，release 事件可能丢失、dragging 卡住
        if self.dragging and QApplication.mouseButtons() == Qt.NoButton:
            self.dragging = False
        self._check_expand_request()
        self._check_scanner()
        self._check_worker()
        self._flush_hist()
        if self.dragging or self._user_resizing:
            # 用户正在拖右下角把手：这时**不能**再按内容算尺寸/挪位置，否则会和用户抢 ——
            # 真机实测：轮询把宽度重新 setFixedWidth 回原值，结果"只有高度能拖，宽度拖不动"
            return
        # 注意：`pinned`（📌）只冻结**位置**，不冻结**尺寸** ——
        # 尺寸现在是内容驱动的（宽度下限 + 高度跟内容），固定住不重算会把两列详情裁掉
        # （自检抓到的：固定后视口 672 < 内容需要 701，右栏的值整片看不见）。
        w = ww.find_main(self.hwnd)
        scr = QGuiApplication.primaryScreen().availableGeometry()
        if w:
            self.hwnd = w.hwnd
        if not w or w.minimized or not w.visible:
            self.setVisible(False)
            return
        self.setVisible(True)
        if self.ball_mode:
            # 小球不跟微信走：它是用户自己摆的（拖过就记住），只跟着"微信在不在"显隐
            self._place_ball()
            return
        l, t, r, b = w.rect
        # 宽度优先跟微信一样宽，但**绝不能小于内容真正需要的宽度**：
        # 面板原来被 setFixedWidth 钉在微信宽度上（真机 657），而左右两列的最小宽度
        # 加起来要 661 + 卡片边距/滚动条 ≈ 695 —— 于是右栏被顶到视口外，
        # 用户只看得见"真实意图/危险等级"这些标签，右边的值（要个解释、1/9 安全、判不了）
        # 全在面板外面。宁可面板比微信宽一点，也不能把结论裁掉。
        # 先把布局跑一遍再问"最少要多宽"，否则拿到的是上一轮的旧值（实测：加了人之后
        # 仍按没有行时的宽度算，宽度下限等于没生效）
        bh = self.body_host.layout()
        if bh is not None:
            bh.activate()
        need = self.body_host.minimumSizeHint().width() + 46
        width = max(MIN_W, min(r - l, scr.width() - 24), min(need, scr.width() - 24))
        if self.width() != width:
            self.setFixedWidth(width)
        avail = min(max(180, b - t), scr.height() - 140)
        self.scroll.setMaximumHeight(max(96, avail - 82))
        # 高度也得跟着**内容**走：`min(sizeHint, avail)` 是错的 —— QScrollArea 的 sizeHint
        # 不随内容增长（它只保证"内容能滚"），所以面板会停在开面板那一刻的高度，
        # 右栏十几行被塞进滚动条后面，用户的原话是"看都看不全"。
        # 用户手动定过大小 → 尊重它；否则按内容算
        user = self._user_size()
        right = self.right_panel
        dm = self.topic == "单聊"
        if user is not None:
            # 先按"右栏可见"量一次，得到全展开所需宽度 —— 它才是该不该进紧凑模式的判据。
            # （写死阈值会出事：真机实测拖到 693 宽时，宽度被内容需要 873 兜住，纹丝不动）
            if not right.isVisible():
                right.setVisible(True)
                self.body_host.layout().activate()
            need_full = self.body_host.minimumSizeHint().width() + 46
            # 私聊里**不进紧凑模式**：左列本来就隐了，右栏就是全部内容，
            # 再把右栏隐掉就只剩一片空白了
            #
            # 有选中的人时也**不进紧凑**：用户点了一个人就是想看详情，
            # 这时候再把右栏藏起来，表现就是"点了没反应"（用户真报过这条）。
            # 面板会自动变宽到装得下详情；面板尺寸存档不动，清空选择后照样回到紧凑。
            compact = (not dm) and self.selected is None and user[0] < need_full - 40
            # 用标记位判断"要不要切形态"：以前拿 right.isVisible() 比，某些情况下
            # 它每轮都判成"需要切"，于是每 600ms 就 setVisible + 写一条日志（实测刷屏）
            if bool(getattr(self, "_compact_on", None)) != compact:
                self._compact_on = compact
                right.setVisible(not compact)
                self.body_host.layout().activate()
                dbg(f"紧凑模式={'开' if compact else '关'}（用户宽 {user[0]}，"
                    f"全展开需要 {need_full}，选中={self.selected!r}）")
                if compact:
                    self._hint("面板较窄：详情已隐藏 · 点一个人会自动展开", "#8a5a00")
        elif not right.isVisible():
            right.setVisible(True)                  # 恢复自适应时把右栏放回来
        # 折起详情时**不能再拿内容高度算窗口高** —— 内容虽被隐藏，minimumSizeHint 照样报
        # 它的自然高度，于是"点收起反而更高"（用户实测：点了缩小按钮面板更大，
        # 多出来的空白还被状态行的 label 拉成一大块黄底）。
        content_h = (self.body_host.minimumSizeHint().height()
                     if self._expanded else 0)
        # 非滚动部分（表头/进度行/底栏/卡片边距）的高度：用"当前高度 − 视口高度"量出来 ——
        # 它不随窗口高度变化，量一次就是准的（写死 82 会少 8px，详情最后一行仍被切）。
        # 刚从小球展开时窗口还没铺开，用 88 兜底，下一个轮询周期就自愈。
        chrome = max(self.height() - self.scroll.viewport().height(), 88)
        if user is not None:
            # 只做夹取，不替用户改主意：宽度不小于"当前形态下内容真正需要的宽度"（免得裁掉值），
            # 高度不小于硬下限（内容超了就滚动 —— 滚动条本来就是干这个的）
            need_w = self.body_host.minimumSizeHint().width() + 46
            width = max(PANEL_MIN_W, min(user[0], scr.width() - 24), min(need_w, scr.width() - 24))
            H = max(PANEL_MIN_H, min(user[1], avail))
        else:
            H = min(max(content_h + chrome + 2, 180), avail)
        if b + GAP + H <= scr.bottom():
            x, y = l, b + GAP
        elif r + GAP + width <= scr.right():
            x, y = r + GAP, t
        elif l - GAP - width >= scr.left():
            x, y = l - GAP - width, t
        else:
            x, y = scr.right() - width, scr.bottom() - H
        x = max(scr.left(), min(x, scr.right() - width))
        y = max(scr.top(), min(y, scr.bottom() - H))
        if not self.pinned and (self.x() != x or self.y() != y):
            self.move(int(x), int(y))          # 位置：固定了就不动
        # 尺寸：即使固定了也要重算（固定只代表"别挪位置"，不代表"别管内容放不放得下"）
        if self.width() != width:
            self.setFixedWidth(int(width))
        if self.height() != H:
            self.resize(int(self.width()), int(H))

    def eventFilter(self, obj, ev):
        """盯住右下角拖拽把手：拖动期间要放开"固定尺寸"，松手后把尺寸记进存档。

        面板平时是 setFixedWidth/固定尺寸的（免得内容一变就自己跳），
        所以拖把手之前必须先解开固定，否则 QSizeGrip 拖不动。
        """
        if obj is getattr(self, "grip", None):
            if ev.type() == QEvent.MouseButtonPress and ev.button() == Qt.LeftButton:
                self._user_resizing = True
                self.setMinimumSize(PANEL_MIN_W, PANEL_MIN_H)
                self.setMaximumSize(16777215, 16777215)
                dbg("用户开始手动调整面板大小")
            elif ev.type() == QEvent.MouseButtonRelease and self._user_resizing:
                self._user_resizing = False
                self.prefs.set("panel_size", [int(self.width()), int(self.height())])
                dbg(f"面板大小已记住 {self.width()}x{self.height()}")
                self._hint(f"面板大小已记住 {self.width()}×{self.height()}"
                           "（右键菜单可恢复自适应）", "#3f9e63")
        return super().eventFilter(obj, ev)

    def reset_size(self):
        """回到"尺寸跟内容走"（清掉用户尺寸 + 把右栏放回来）。"""
        self.prefs.data.pop("panel_size", None)
        self.prefs.save()
        self.r_head.parentWidget().setVisible(True)
        self.setMinimumSize(0, 0)
        self.setMaximumSize(16777215, 16777215)
        self._hint("已恢复自适应大小（跟着内容走）", "#3f9e63")
        dbg("恢复自适应大小")

    def _user_size(self):
        """用户在存档里定过的面板尺寸（宽, 高）；没定过返回 None。"""
        v = self.prefs.get("panel_size")
        if isinstance(v, (list, tuple)) and len(v) == 2:
            try:
                return (int(v[0]), int(v[1]))
            except (TypeError, ValueError):
                return None
        return None

    def set_pinned(self, on: bool):
        dbg(f"set_pinned({on})")
        self.pinned = bool(on)
        self.btn_pin.setToolTip(
            "已固定，不再自动吸附（点一下取消）" if on
            else "固定位置（不再自动吸附到微信窗口）")
        if not on:
            self.follow()

    def mousePressEvent(self, e):
        dbg(f"HudBar.mousePress {e.globalPosition().toPoint().toTuple()}")
        if e.button() == Qt.LeftButton:
            self.dragging = True
            self.drag_pos = (e.globalPosition().toPoint()
                             - self.frameGeometry().topLeft())
            self._press_xy = e.globalPosition().toPoint()
            self._press_at = time.time()
            self._moved = 0

    def mouseMoveEvent(self, e):
        if self.dragging and e.buttons() & Qt.LeftButton:
            p = e.globalPosition().toPoint()
            self._moved = max(self._moved, (p - self._press_xy).manhattanLength())
            self.move(p - self.drag_pos)

    def mouseReleaseEvent(self, e):
        was_drag = self._moved >= DRAG_SLOP
        self.dragging = False
        if not self.ball_mode or e.button() != Qt.LeftButton:
            return
        # 小球：拖 = 挪位置（松手即存），点 = 展开面板。两者靠位移区分 ——
        # 不做这个区分的话，想挪一下小球就会顺手把面板弹出来。
        if was_drag:
            self.ball_xy = [self.x(), self.y()]
            self.prefs.set("ball_xy", self.ball_xy)
            dbg(f"小球挪到 {self.ball_xy}")
        elif time.time() - self._press_at < 0.6:
            self.set_ball(False)

    def mouseDoubleClickEvent(self, e):
        self.btn_pin.setChecked(False)

    def contextMenuEvent(self, e):
        dbg("contextMenuEvent")
        self._build_context_menu().exec(e.globalPos())

    def _build_context_menu(self) -> QMenu:
        """把右键菜单建出来并返回（**不在这里 exec**）。

        拆成两半是为了能验证：自检要断言"菜单项齐全""每一项点下去真的生效"，
        原来只能真的把菜单弹出来 —— 菜单一次次闪在用户屏幕正中（用户连问两次
        "怎么一直出现"），offscreen 平台下 `exec()` 的嵌套循环还会永远不返回。
        现在测试直接拿这个对象读项、trigger 项，弹出来那一步由 contextMenuEvent
        单独负责，两边用的是同一段构建代码。
        """
        m = QMenu(self)
        if self.ball_mode:
            # 球形态下"固定位置/收起详情"都没有意义：球本来就不跟微信走，也没有详情区
            a0 = m.addAction("展开面板")
            a0.triggered.connect(lambda: self.set_ball(False))
            m.addSeparator()
        else:
            a = m.addAction("取消固定，回到吸附位" if self.pinned else "固定位置")
            a.triggered.connect(lambda: self.btn_pin.setChecked(not self.pinned))
            b = m.addAction("收起详情" if self._expanded else "展开详情")
            b.triggered.connect(self.toggle)
            a1 = m.addAction("收成小球")
            a1.triggered.connect(lambda: self.set_ball(True))
            if self._user_size() is not None:
                a2 = m.addAction("恢复自适应大小")
                a2.triggered.connect(self.reset_size)
        c = m.addAction("填写我在本群的昵称")
        c.triggered.connect(self.ask_nickname)
        st = m.addAction("设置…（频率 / 成本 / 阈值 / Key）")
        st.triggered.connect(self.open_settings)
        m.addSeparator()
        d = m.addAction("清空本群判断缓存")
        d.triggered.connect(self.clear_cache)
        m.addSeparator()
        q = m.addAction("退出")
        q.triggered.connect(self._on_close)
        return m

    def keyPressEvent(self, e):
        dbg(f"keyPress key={e.key()}")
        if e.key() == Qt.Key_Escape:
            self._on_close()
        else:
            super().keyPressEvent(e)

    def _apply_scene_labels(self) -> None:
        """左列 / 右栏的措辞与**显隐**跟着场景换。

        私聊里没有"群里的人"，也不该说"点左边任意一个人"——只有一个人，
        照搬群聊措辞会让人以为面板把私聊当群聊处理了（用户一眼就看出来了）。
        更进一步：私聊**整列隐掉**。左列本来占 3/5 宽度，只为放一行"某某"，
        却把右栏挤到 ~248px —— 真机上那点宽度不够放"键+值"两列，值会被自己的
        格子容器切掉（实测截图：`在试探你在不`、`要个具体安排` 都贴着边断掉）。
        隐掉左列后右栏拿到全宽，面板也跟着变窄（~450px），少盖一大片屏。
        """
        dm = self.topic == "单聊"
        self.left_panel.setVisible(not dm)
        self.l_head.setText("对方　·　点一下看判断" if dm
                            else "群里的人　·　点一个人看判断")
        # 列头文案随场景换：私聊里第二列放的是 `danger_word`（如"安全"），末列是"信息不足"
        if getattr(self, "_l_col_labels", None):
            self._l_col_labels[1].setText("危险等级" if dm else "在群里的角色")
            self._l_col_labels[2].setText("要不要回")
            self._l_col_labels[3].setText("关注度")
            self.l_risk_head.setText("信息量" if dm else "风险信号")
        if self.selected is None:          # 右栏正在显示某人的判断时不动它的标题
            self.r_head.setText("判断")
            self.r_hint.setText(
                "对方的最新判断会显示在这里（不用点）。" if dm
                else "点左边任意一个人，这里显示模型对这个人的判断。")
        self.body_host.layout().activate()  # 让隐掉一列后的宽度/高度立刻重算

    def toggle(self):
        self._expanded = not self._expanded
        self._apply_fold()
        self.follow()          # 立刻按新状态重算窗口高（否则要等最多 600ms 的轮询）

    def _apply_fold(self):
        """折叠状态的唯一出口：详情区与底栏跟着变。

        标题栏的 ▴ 按钮已按用户要求去掉（2026-09-24："去除缩小"），入口只剩右键菜单；
        面板默认就是展开的（`_expanded = True`），所以这里通常只被菜单的
        「收起详情/展开详情」调到。
        """
        self.scroll.setVisible(self._expanded)
        self.foot.setVisible(self._expanded)
        self.adjustSize()

    def _on_close(self):
        """唯一退出路径：右键菜单「退出」（✕ 已按用户要求移除）。

        不能在这里直接 close()：菜单 `exec()` 还开着，实测 close() 被那层嵌套循环吃掉 ——
        `_on_close` 明明被调用了（自检里打了桩确认），窗口却留在屏幕上不消失。
        所以先收起菜单，再把真正的关闭排到下一轮事件循环里执行。
        """
        dbg("menu quit")
        popup = QApplication.activePopupWidget()
        if popup is not None:
            popup.close()
            QTimer.singleShot(0, self._close_now)   # 等这层嵌套循环退出来再关
            return
        self._close_now()                           # 没有菜单挡着（例如 Esc）：立刻关

    def _close_now(self):
        """真正关掉：先让它从屏幕上消失，再走关闭流程。

        `_shutdown()` 里要 join 两个线程（各最多 1.5s），不能等它跑完才隐藏窗口，
        否则用户点了「退出」会对着一个不动的面板干等。
        隐藏之后 Qt 有可能不再派发 closeEvent —— 所以 `_shutdown()` 在这里直接调一次，
        保证线程不会留成僵尸（历史上踩过 11 个进程/4 个窗口）。
        """
        dbg("_close_now: 隐藏 + 关闭")
        self.hide()
        self.close()
        self._shutdown()
        QApplication.quit()

    def clear_cache(self):
        self.msg_sel = None
        self.msg_result = None
        self.worker.profile = SpeakerProfile()
        self.results.clear()
        self.selected = None
        self._clear_right()
        self.r_hint.setText("缓存已清空。下次自动扫描会重新调用 API。")
        self.b_state.setText("")
        self._refresh_ball()              # 结论清空了，球的颜色不能还停在上一轮

    def refresh_log_button(self) -> None:
        try:
            n = os.path.getsize(AUDIT_PATH)
        except OSError:
            n = 0
        for unit in ("B", "KB", "MB", "GB"):
            if n < 1024 or unit == "GB":
                human = f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
                break
            n /= 1024.0
        self.btn_log.setText(f"日志 {human}")
        self.btn_log.setToolTip(
            "审计日志占用（含完整聊天原文，全量永久保留）。点两次清空。")

    def on_log_click(self):
        now = time.time()
        if now - self._log_clear_armed > 4.0:
            self._log_clear_armed = now
            self._hint("再点一次「日志」即清空审计日志（含完整聊天原文，不可恢复）")
            return
        self._log_clear_armed = 0.0
        try:
            ok, why = self._clear_audit_log()
            if ok:
                self._hint("审计日志已清空", "#3f9e63")
            else:
                self._hint(f"清空失败：{why}")
        except Exception as e:
            self._hint(f"清空失败：{type(e).__name__}: {e}")
        self.refresh_log_button()

    def _clear_audit_log(self) -> tuple[bool, str]:
        """清空审计日志：**优先走带锁的 JevAudit.clear()**（§6.6 复核）。

        HUD 原来自己 `open(AUDIT_PATH, "wb")` 截断，不持任何锁：worker 线程
        或另一个实例正在写记录时，截断与写入会交错出半行/空洞，`seq` 也不回退
        （清空后编号接着涨）。client 还没建起来时（一次都没跑过）退化为文件截断。
        """
        client = getattr(self.worker, "client", None)
        if client is not None:
            return client.audit.clear()
        with open(AUDIT_PATH, "wb") as f:
            f.truncate(0)
        return True, "已清空（客户端尚未初始化，直接截断）"

    # ---------------- 场景身份（关系 / 群性质） ----------------
    def scene_key(self) -> str:
        """存档键：群用会话标题原名（不动老数据），私聊加后缀防重名串味。"""
        return self.group + (DM_KEY_SUFFIX if self.topic == "单聊" else "")

    def build_viewer(self) -> dict:
        """按场景组装 viewer —— 送进 state 的就是它。

        群聊：群昵称 / 职责 / 群性质；私聊：关系 / 身份。
        私聊的关系没填时回落到全局默认关系（同类实现 jarvis 的做法），
        再没有就是 None → state 里显式写「（未填写）」。
        """
        if not self.group:
            return {}
        rec = self.store.get(self.scene_key())
        if self.topic == "单聊":
            return {"关系": rec.get("rel") or self.store.default_relationship(),
                    "身份": rec.get("identity")}
        return {"群昵称": rec.get("me"), "职责": rec.get("duty"),
                "群性质": rec.get("kind")}

    def missing_input(self) -> str:
        """这个会话还缺哪个"决定性输入"（空串 = 不缺）。"""
        if not self.group:
            return ""
        v = self.viewer or {}
        if self.topic == "单聊":
            return "" if v.get("关系") else "关系"
        return "" if v.get("群昵称") else "群昵称"

    def ask_nickname(self):
        """弹对话框填"决定性输入"。名字沿用 ask_nickname（右键菜单/按钮都指向它）。"""
        if not self.group:
            return
        dm = self.topic == "单聊"
        rec = self.store.get(self.scene_key())
        dlg = NicknameDialog(self.group, scene=("dm" if dm else "group"), rec=rec,
                             default_rel=self.store.default_relationship() or "", parent=self)
        dlg.show()
        dlg.raise_()
        dlg.activateWindow()
        if dlg.exec() == QDialog.Accepted:
            vals = dlg.value()
            set_def = vals.pop("set_default", False)
            self.store.set_scene(self.scene_key(), "dm" if dm else "group", **vals)
            if set_def and vals.get("rel"):
                self.store.set_default_relationship(vals["rel"])
            self.viewer = self.build_viewer()
            self.refresh_me_button()
            self.last_sig = None      # 身份变了，强制重新分诊
            self.worker.profile = SpeakerProfile()   # 缓存键含身份，但直接清掉更省事
            self._hint("已记住，下一条消息起按新身份判断", "#3f9e63")
            dbg(f"场景身份已设为 {self.viewer!r}（scene={self.topic}）")

    def refresh_me_button(self):
        missing = self.missing_input()
        v = self.viewer or {}
        if self.topic == "单聊":
            self.btn_me.setText("填关系")
            self.btn_me.setToolTip(
                f"我和{self.group}：{v.get('关系') or '（未填）'}"
                f"｜我的身份：{v.get('身份') or '（未填）'}"
                if not missing else "点这里填「我和这人什么关系」——它决定「该不该回」")
        else:
            self.btn_me.setText("填群信息")
            nick = v.get("群昵称")
            self.btn_me.setToolTip(
                f"我在本群：{nick}｜职责：{v.get('职责') or '（未填）'}"
                f"｜群性质：{v.get('群性质') or '（未填）'}"
                if not missing else "点这里填「我在本群的昵称」——它决定「有没有人在点我名」")
        self.btn_me.setVisible(bool(missing))
        # 未填时高亮（不弹模态框，只在面板上显眼一点）
        self.btn_me.setObjectName("link_hot" if missing else "link")
        self.btn_me.setStyleSheet("")
        self.btn_me.style().unpolish(self.btn_me)     # 让 objectName 换样式立刻生效
        self.btn_me.style().polish(self.btn_me)
        # 状态行那句提示：缺判据就说去哪填；不缺就把"这次的判据"摆出来
        # （原来这是右栏单独一行，为了给右栏省一行、也为了更显眼，挪到状态行）
        if not hasattr(self, "nudge"):
            return
        if missing:
            self.nudge.setText("· 点「填关系」补判据：它决定「该不该回」" if missing == "关系"
                               else "· 点「填群信息」补判据：它决定「有没有人在点我名」")
            self.nudge.setStyleSheet("")
            self.nudge.setVisible(True)
            return
        dm = self.topic == "单聊"
        v = self.viewer or {}
        crit = (f"关系={v.get('关系') or '（未填）'}｜身份={v.get('身份') or '（未填）'}"
                if dm else
                f"群性质={v.get('群性质') or '（未填）'}｜职责={v.get('职责') or '（未填）'}")
        self.nudge.setText("· 判据 " + crit)
        self.nudge.setStyleSheet("#nudge{font-size:10.5px;color:#6b6b6b;background:transparent}")
        self.nudge.setVisible(True)
        # 状态行宽度有限（状态文字本身已经很长）：超了就省略，完整内容进 tooltip，
        # 免得它把面板宽度顶大或自己被裁掉
        fm = QFontMetrics(self.nudge.font())
        short = fm.elidedText(self.nudge.text(), Qt.ElideRight, 300)
        self.nudge.setText(short)
        self.nudge.setToolTip("· 判据 " + crit)

    # ---------------- 扫描 ----------------
    # ---------------- 进度条 ----------------
    def set_progress(self, stage: str, pct: int, text: str):
        self._stage, self._stage_at = stage, time.time()
        self.bar.setValue(max(0, min(100, pct)))
        self.status.setText(text)

    def on_tick(self, info: dict):
        """每轮扫描的回执：让"在扫描"这件事可见，并暴露实际节奏。"""
        self._scan_info = info
        if self._stage in ("启动", "扫描", "完成", "待命"):
            if info.get("paused"):
                pct, txt = 0, f"已暂停 · 免打扰中（{HOTKEY_TEXT} 继续）"
            elif info.get("skip"):
                # 措辞要经得起误读：原来写"画面未变，已跳过识别"，用户看了以为是
                # "面板没发现我这边内容变了"。这是**性能提示**（省一次 OCR），不是判断结果。
                pct, txt = 6, "扫描中 · 画面与上次相同（已判过，无需重判）"
            else:
                ms = info.get("ocr_ms", 0)
                pct, txt = 20, f"扫描中 · 识别 {ms}ms"
            if self._last_done:
                txt += f" · 上次结论 {self._last_done}"
            self.set_progress("扫描", pct, txt)

    def on_stage(self, d: dict):
        if d.get("stage") == "粗筛":
            self.set_progress("粗筛", 40, f"已排好序 · {d.get('note','')}")
        elif d.get("stage") == "细判":
            i, k = d.get("i", 0), d.get("k", 1) or 1
            self.set_progress("细判", 50 + int(45 * i / k),
                              f"细判 {i}/{k} · {d.get('note','')}")

    def _hint(self, text: str, color: str = "#d97706"):
        """提示只在文字变化时更新，避免每 2 秒刷同一条（实测会刷屏）。"""
        if text != self._last_hint:
            self._last_hint = text
            self.hint.setText(text)
        self.hint.setVisible(True)
        self.dot.setStyleSheet(f"color:{color}")

    def on_scan_blocked(self, msg: str):
        """微信不可用（不可见/最小化/被遮挡）→ 不扫描、更不调 API。"""
        self._hint(msg)
        self._ball_bad = f"扫描不到微信：{msg[:36]}"
        self._refresh_ball()
        self.last_sig = None
        self._pending_sig = None

    def on_scan_failed(self, msg: str):
        self.dot.setStyleSheet("color:#dc2626")
        self.hint.setText(f"扫描失败：{msg[:110]}")
        self.hint.setVisible(True)
        self._ball_bad = f"扫描失败：{msg[:36]}"
        self._refresh_ball()

    def on_scan(self, v):
        if v.error and not v.title:
            # 版面检测失败时标题区也读不到，此时"读不到标题"是**结果**而不是原因：
            # 先把版面为什么没量出来说清楚，否则用户看到的永远是"读不到会话标题"，
            # 而真正的原因（窗口太小 / 布局没铺好）被一起吞掉。
            self._hint(v.error)
            return
        if not v.title:
            # 不再静默返回：标题读不到时面板会一直空白，而用户看不到任何原因
            self._hint("读不到会话标题（OCR 未识别出标题区），换一下窗口位置或让它保持可见")
            return
        # 先算这一帧的发言人（会话切换判定要用它）：标题像同一个、且发言人还有交集 → 同一个会话。
        # 两条一起用才安全：只比标题会把"…基地(39)"和"…基地(42)"这种姐妹群并成一个；
        # 只比发言人则"同一个群换了一批人说话"会被当成换会话。
        speakers = v.speakers() if (v.messages and not v.error) else []
        now_people = {n for n, _ in speakers}
        prev_people = set(self._last_people or set())
        same_conv = bool(self.group) and (
            _title_same_conv(v.title, self.group)
            or (_title_similar(v.title, self.group)
                and (not prev_people or bool(prev_people & now_people))))
        if not same_conv:
            prev = self._conv_key()                 # 上一个会话的键（要先算，下面就要覆盖 group 了）
            self.group = v.title
            self._conv_id = v.title                 # 会话键跟着这次"真切换"定格（抗 OCR 抖动）
            self.topic = ("群聊" if v.is_group else "单聊")
            self.viewer = self.build_viewer()      # 按场景组装（群/私聊字段不同）
            self.refresh_me_button()
            self.last_sig = None
            fm = QFontMetrics(self.title.font())
            self.title.setText(fm.elidedText(v.title, Qt.ElideRight, 300)
                               + ("（群）" if v.is_group else ""))
            self.selected = None           # 换会话了，右栏不该还挂着上一个人的判断
            # 判断结果同样是上一个会话的（§6.6 复核）：不清的话小球会一直按旧群的
            # alert 显示橙红（`_refresh_ball` 统计 `self.results` 全表），
            # 左列也会在新会话首轮分诊完成前挂着旧群的人名。
            self.results.clear()
            self._last_people = set()
            self._last_ctx = []
            self._pending_sig = None
            # 换会话是**强信号**（标题变了就是真的换群/换私聊了，不是 OCR 抖动）：
            # ① 不再等那轮"确认稳定"的去抖（省一个扫描周期 ~3s）
            # ② 立刻把看过的上一轮结果摆回右栏（内存缓存），不用干等
            # ③ 状态行写清"正在判第一轮"，别让用户对着一片空白猜
            self._skip_debounce = True
            self._restore_conv_results()
            self._refresh_ball()
            self._apply_scene_labels()
            dbg(f"会话切换：{prev!r} → {self._conv_key()!r}（免去抖 + 取缓存结果）")
        elif len(_norm_sig(v.title)) > len(_norm_sig(self.group or "")):
            # 同一个会话、只是这次 OCR 读得更全（真机实测：长标题常被读短）→ 只更新显示名，
            # 会话键 / 节流 / 结果缓存**都不动**，免得同一个群被当成好几个群
            dbg(f"会话标题读得更全：{self.group!r} → {v.title!r}（会话键不变）")
            self.group = v.title
            fm2 = QFontMetrics(self.title.font())
            self.title.setText(fm2.elidedText(v.title, Qt.ElideRight, 300)
                               + ("（群）" if v.is_group else ""))
        if v.error or not v.messages:
            self._hint("读不到消息区" if v.error else "此会话暂无消息")
            return
        self.dot.setStyleSheet("color:#22a06b")
        self.hint.setVisible(False)
        if self._ball_bad:                 # 从"扫描出问题"恢复：球该回到正常色了
            self._ball_bad = None
            self._refresh_ball()

        if not speakers:                   # 发言人已在上面算过（会话切换判定需要用）
            self.hint.setText("这个会话还没抓到别人的消息")
            self.hint.setVisible(True)
            return
        # 签名用归一化文本：抵抗 OCR 抖动
        sig = tuple((n, tuple(_norm_sig(m) for m in msgs))
                    for n, msgs in speakers)
        if sig == self.last_sig:
            self._pending_sig = None
            return
        # 去抖：OCR 每 2 秒会因滚动/新消息产生微小差异，签名一变就提交会把调用刷爆
        # （实测 20 秒内提交 6 次）。要求连续两次看到同一签名，再叠加最小间隔。
        new_person = self._last_people and (set(n for n, _ in speakers)
                                            - self._last_people)
        if sig != self._pending_sig and not new_person and not self._skip_debounce:
            # 签名已归一化（见 _norm_sig），抖动已被抹平，所以等一轮就够；
            # 原先等两轮是双重保守，实测把响应时间拖到 12s 以上。
            self._pending_sig = sig
            dbg(f"签名变化，等待稳定（{len(speakers)} 人）")
            self.set_progress("扫描", 12, "内容有变化 · 正在确认（下一轮提交）")
            return
        now = time.time()
        min_interval = MIN_TRIAGE_INTERVAL
        try:                        # F-22：最小重判间隔也可配置（默认 20s）
            from .settings import SETTINGS
            min_interval = int(SETTINGS.get("min_interval_s"))
        except Exception:
            pass
        # **按会话**算节流（原来是一个全局 `_last_triage`）：
        # 常量注释与设置项文案都写着"**同一会话**最小重判间隔"，实现却是全局的 ——
        # 结果是"在 A 群刚判过 → 切到 B 群"要沿用 A 群的 20s 窗口，最长干等 20 秒，
        # 而这段时间左列还被清空了，用户看到的就是"切群卡住"（2026-09-24 用户报）。
        conv = self._conv_key()
        last_conv_triage = self._last_triage_by_conv.get(conv, 0.0)
        if now - last_conv_triage < min_interval and not new_person:
            # 内容变了但还在节流窗口里 —— 必须**说出来**：否则面板看着就是"卡住了"
            # （用户："内容变了，但没识别"）。这里把剩余秒数摆在状态行上。
            left = int(min_interval - (now - last_conv_triage)) + 1
            dbg(f"距本会话上次分诊不足 {min_interval}s，跳过（还剩 {left}s）")
            self.set_progress("扫描", 12, f"内容已变 · 节流中（约 {left}s 后重判）")
            self._pending_sig = sig
            return
        if new_person:
            dbg(f"出现新发言人 {sorted(new_person)}，立即分诊")
        if self._skip_debounce:
            dbg("新会话首帧 → 免去抖直接提交")
        self._skip_debounce = False
        self._last_triage_by_conv[conv] = now
        self._pending_sig = None
        self.last_sig = sig
        self._last_people = {n for n, _ in speakers}
        # recent_messages() 返回的是 (发言人, 内容) 元组，不是 Line 对象
        ctx = list(v.recent_messages(MAX_CONTEXT_LINES))
        self._last_ctx = ctx
        self._speaker_msgs = dict(speakers)
        self.remember(speakers)          # 按人累积（屏上滚掉的也留在本机历史里）
        self._submitted_at = now         # 埋点：提交 → 结果 的耗时
        dbg(f"提交分诊: {len(speakers)} 人（会话={conv!r}）")
        self.set_progress("粗筛", 30, f"提交判断 · {len(speakers)} 人待筛")
        hist = {n: self.history_of(n) for n, _ in speakers}
        self.worker.submit((self.group, self.topic, self.viewer, speakers, ctx, hist))

    # ---------------- 分诊结果 ----------------
    def on_triage(self, r: dict):
        sk = r.get("skim") or {}
        details = r.get("details") or []
        self.b_cost.setText(f"今日 {r.get('calls_hour',0)} 次/时 · "
                            f"${r.get('cost_today',0):.4f}")
        self.refresh_log_button()

        # 粗筛结论放在状态徽标里
        if sk:
            # `sk.get("needs_me", 0)` 拿不到默认值：私聊那条路径的 skim 是**显式的**
            # `{"needs_me": None, "risk_any": None}`（单聊没有粗筛这一步）。
            # 于是 `round(None * 100)` 抛 TypeError，异常从 Qt 槽里冒出去 ——
            # 后面整段（渲染名单、页脚、进度、小球）全都不执行，
            # 面板表现成"提交了分诊却什么都没变"。实测 19:18:31 / 19:23:41 两次私聊都这样。
            bad = sk.get("needs_me") is None and sk.get("risk_any") is None
            nm, ra = _num(sk.get("needs_me")), _num(sk.get("risk_any"))
            if bad:
                # 不摆一个"需我 0%"的假徽标：私聊本来就没有这一步
                self.b_state.setText("私聊 · 直接细判")
                self.b_state.setStyleSheet(
                    "font-size:10px;color:#fff;border-radius:3px;padding:1px 6px;"
                    "background:#3f9e63")
            else:
                self.b_state.setText(
                    f"需我 {round(nm*100)}% · 风险 {round(ra*100)}%")
                self.b_state.setStyleSheet(
                    f"font-size:10px;color:#fff;border-radius:3px;padding:1px 6px;"
                    f"background:{'#c2410c' if ra > 0.5 or nm > 0.5 else '#3f9e63'}")

        # 把"粗筛排过序、但没细判"的人也渲染出来（静默行）——
        # 否则用户看不到任何解析结果，无法判断工具是否在工作。
        parsed = r.get("all_speakers") or []
        # 细判结果必须**覆盖**已有条目，不能只给"还没出现过的人"建占位行：
        # 点击「只粗筛过的人」时会在该人的 state_reasons 里写下占位文字"正在细判…"，
        # 只建占位、不覆盖，那个占位就永远留着 —— 实测面板永久卡住、点几次都刷不掉
        # （细判明明成功返回了，见 out/hud_debug.log 与审计日志的 detail 记录）。
        for d in details:
            name = d.get("nickname") or ""
            if name:
                self.results[name] = {**d, "pending_detail": False}
        detailed = {d.get("nickname") for d in details}
        for name in parsed:
            if name in detailed:
                continue
            cur = self.results.get(name)
            # 点击「只粗筛过的人」会写下占位文字"正在细判…"。如果这一轮粗筛判定"没人需要
            # 细判"（或被节流），就不会有任何细判结果来替换它 —— 面板于是永久停在
            # "正在细判…"，看起来像卡死。这里把它改成实话：本轮没细判这个人。
            if cur and (cur.get("state_reasons") or [""])[0] == "正在细判…":
                cur["state_reasons"] = ["粗筛判定无需关注（本轮未细判）"]
                cur["pending_detail"] = True
        for name in parsed:
            if name not in self.results:
                self.results[name] = {
                    "nickname": name, "msgs": [], "state": "silent",
                    "state_reasons": ["未细判（粗筛判定无需关注）"],
                    "role": "", "intent": "", "worth": "", "attention": 0,
                    "attention_mass": 0.0, "addressed_to_me": 0.0,
                    "risks": {}, "sufficiency": 0.0, "worth_conf": 0.0,
                    "model": "", "usage": {}, "cached": False,
                    "pending_detail": True,
                }
        self.results = dict(self.results)
        order = parsed or [d["nickname"] for d in details]
        rows_data = [self.results[n] for n in order if n in self.results]
        rows_data.sort(key=lambda d: ({"alert": 0, "todo": 1}.get(d["state"], 2),
                                     -d.get("attention", 0)))
        self._rebuild_rows(rows_data, parsed_count=len(parsed))

        if r.get("skipped"):
            why = "; ".join(f"{k}: {v}" for k, v in r["skipped"])
            self.foot.setText(f"本机处理 · 已节流（{why}）")
        elif r.get("gate_reason"):
            self.foot.setText(f"本机处理 · 细判原因：{r['gate_reason']}")
        else:
            self.foot.setText(
                f"本机处理 · 只把群成员的最近几条消息发给 Jev 判断 · "
                f"细判 {len(details)} 人 · 日志 {r.get('total_records',0)} 条")

        # 默认沉默：没有 alert 就不打扰，只提示"已看过"
        ts = time.strftime("%H:%M:%S")
        nm = _num((sk or {}).get("needs_me"))
        # 私聊没有粗筛，就没有"需我 x%"这回事 —— 不编一个 0% 出来
        tail_pct = "" if not sk or sk.get("needs_me") is None else f" · 需我 {round(nm*100)}%"
        if details:
            self._last_done = f"{ts} 细判 {len(details)} 人"
            self.set_progress("完成", 100,
                              f"完成 · 细判 {len(details)} 人{tail_pct}")
        else:
            self._last_done = f"{ts} 无人需要关注"
            self.set_progress("完成", 100,
                              f"完成 · 粗筛 {len(self._last_people or [])} 人，"
                              f"无人需要你出手{tail_pct}")

        alerts = [d for d in details if d["state"] == "alert"]
        if self.topic == "单聊" and details:
            # 私聊没有左列可点（整列隐掉了）→ 必须自动选中对方，否则详情永远不出现
            # （原来只有 alert 才会自动选中，判成 todo/silent 时面板看着像空的）
            pick = (alerts[0]["nickname"] if alerts else details[0]["nickname"])
            if pick != self.selected:
                self.on_person_click(pick)
        elif alerts and alerts[0]["nickname"] != self.selected:
            self.on_person_click(alerts[0]["nickname"])
        if self.selected and self.selected in self.results:
            # 右栏必须跟着**每一轮**重画（不管上面有没有换人）：否则它会停在上一轮的快照上。
            # ⚠️ 这里踩过坑：加"私聊自动选中"时把本分支写成了上面的 elif，
            # 于是私聊里"选中的人没变"时**永远不重画** —— 新结果进了 results、界面还挂在旧消息上，
            # 真机表现就是用户说的"卡住了 / 内容变了但没识别 / 最近的消息根本没识别"。
            self._show_detail(self.results[self.selected])
        self._ball_bad = None             # 这一轮判完了，球的颜色按结论来
        self._refresh_ball()
        self.adjustSize()
        self.follow()
        # 这一轮存进内存缓存（切回这个会话时立刻摆出来，不用再等一轮）
        self._remember_conv_results()
        if self._submitted_at:
            dbg(f"提交→结果渲染完成 {int((time.time() - self._submitted_at) * 1000)}ms"
                f"（会话={self._conv_key()!r} 细判 {len(details)} 人）")
            self._submitted_at = 0.0

    def _rebuild_rows(self, details, parsed_count: int = 0):
        while self.listbox.count():
            it = self.listbox.takeAt(0)
            if it.widget():
                it.widget().deleteLater()
        self.rows = {}
        if not details:
            self.hint.setText(
                f"粗筛看过 {parsed_count} 人：都不需要你出手。\n"
                "（名单已列出，点任意一个可以按需细判）")
            self.hint.setVisible(True)
            self._sync_scroll_height()
            return
        self.hint.setVisible(False)
        for d in details:
            # 按概率从高到低排：左列只显示最高的那一枚徽标（见 PersonRow 的说明）
            risks = [k for k, val in sorted(d["risks"].items(), key=lambda kv: -kv[1])
                     if val > 0.5]
            row = PersonRow(
                key=d["nickname"], nick=d["nickname"],
                role=((d.get("danger_word") or "") if d.get("scene") == "dm"
                      else label(d["role"])),
                worth=label(d["worth"]), attention=d["attention"],
                state=d["state"], risks=risks,
                low_suff=d["sufficiency"] > 1.5,
                attn_mass=d.get("attention_mass", 1.0))
            row.set_selected(d["nickname"] == self.selected)
            row.clicked.connect(self.on_person_click)
            self.listbox.addWidget(row)
            self.rows[d["nickname"]] = row
        self._sync_scroll_height()

    def _remember_conv_results(self) -> None:
        """把刚判完的这一轮按会话存进**内存**缓存（切回时立刻显示）。

        为什么需要：切群/切私聊是高频动作，而重新判一轮要等一个扫描周期 + 一次调用；
        看过的会话切回来还要再等一遍，体感就是"切群慢"。这里**只存内存、不落盘** ——
        聊天原文不新增文件（`out/` 里现有的那几个已经够敏感了）。LRU 上限 CONV_CACHE_MAX。
        """
        conv = self._conv_key()
        if not conv or not self.results:
            return
        self._conv_results[conv] = {
            "results": dict(self.results),
            "speaker_msgs": dict(self._speaker_msgs),
            "ctx": list(self._last_ctx),
            "at": time.time(),
        }
        while len(self._conv_results) > CONV_CACHE_MAX:
            oldest = min(self._conv_results, key=lambda k: self._conv_results[k]["at"])
            self._conv_results.pop(oldest, None)

    def _restore_conv_results(self) -> None:
        """切到某个会话时：内存里有它上一轮结果就立刻摆出来，并**标明是上一轮**。

        宁可显示标了时间的旧结论，也不要让用户对着一片空白等一轮 —— 但绝不能让人
        把旧结论当成实时结论，所以提示里写清"X 分钟前 · 正在刷新"。
        """
        c = self._conv_results.get(self._conv_key())
        if not c or not c.get("results"):
            self._hint("新会话 · 正在判第一轮…")
            return
        self.results = dict(c["results"])
        self._speaker_msgs = dict(c.get("speaker_msgs") or {})
        self._last_ctx = list(c.get("ctx") or [])
        self._last_people = set(self.results)
        order = sorted(self.results, key=lambda n: -self.results[n].get("attention", 0))
        self._rebuild_rows([self.results[n] for n in order], parsed_count=len(order))
        mins = int(max(0, time.time() - c["at"]) // 60)
        ago = "刚刚" if mins < 1 else f"{mins} 分钟前"
        self._hint(f"显示的是上一轮结果（{ago}）· 正在刷新…", "#c99a2e")

    def on_person_click(self, key: str):
        t_click = time.time()
        dbg(f"on_person_click {key}")
        if self.msg_sel and self.msg_sel[0] != key:
            self.msg_sel = None                    # 换人了 → 退出消息视图
            self.msg_result = None
        was_compact = not self.right_panel.isVisible()
        self.selected = key
        if was_compact:
            # 紧凑模式下右栏是隐藏的：点人等于"我要看详情" → 让 follow() 把它放出来
            dbg("点人：原为紧凑模式（右栏隐藏）→ 自动展开详情")
            self._hint("已展开详情（面板较窄时会自动变宽）", "#3f9e63")
        for k, row in self.rows.items():
            row.set_selected(k == key)
        d = self.results.get(key)
        if not d:
            return
        if d.get("pending_detail"):
            # 粗筛没细判过的人：点击时按需补一次细判（只调用一次，之后走缓存）
            d["pending_detail"] = False
            d["state_reasons"] = ["正在细判…"]
            self._show_detail(d)
            sp = self._speaker_msgs.get(key)
            if sp:
                # triage 要的是 [(昵称, 消息列表)]。之前传 [sp]（只有消息列表）：
                # 该人恰好 1 条消息时 unpack 报 "expected 2, got 1"（自动选中 alert 的人
                # 就会走到这条路，不需要点鼠标）；≥2 条时更糟——不报错，但昵称位置被
                # 填成了消息正文。实测见 out/hud_debug.log 15:19:13。
                self.worker.submit((self.group, self.topic, self.viewer,
                                    [(key, sp)], self._last_ctx,
                                    {key: self.history_of(key)}))
            dbg(f"点人（粗筛过、需补细判）渲染耗时 {int((time.time()-t_click)*1000)}ms")
            return
        self._show_detail(d)
        dbg(f"点人渲染耗时 {int((time.time()-t_click)*1000)}ms（走的是细判过的路径，应 <50ms）")

    def on_msg_click(self, text: str) -> None:
        """点了一条消息 → 判这一条（复用私聊那套 11 题口径）。"""
        nick = self.selected or ""
        if not nick or not (text or "").strip():
            # 没有主语就没法判（也是"别白花钱"的兜底）：消息行只在选中某个人时才画出来，
            # 正常点不到这里，防的是切人/清缓存的极端时序
            dbg(f"点消息但没有选中的发言人 → 忽略：{nick!r} {(text or '')[:30]!r}")
            return
        dbg(f"点消息判断：{nick!r} {(text or '')[:30]!r}")
        self.msg_sel = (nick, text)
        self.msg_result = None
        d = self.results.get(nick)
        if d:
            self._render_msg_detail(d)                  # 先画出"正在判断…"
        self.worker.submit_msg((self.group, dict(self.viewer or {}), nick, text,
                                list(self._last_ctx or [])))

    def on_msg_done(self, r: dict) -> None:
        """单条消息判断回来了。"""
        if not self.msg_sel:
            return
        msgs = r.get("msgs") or []
        if not msgs or msgs[0] != self.msg_sel[1]:
            dbg("单条消息判断回来时已切走 → 丢弃")
            return
        self.msg_result = r
        d = self.results.get(self.msg_sel[0])
        if d:
            self._render_msg_detail(d)
        self.adjustSize()
        self.follow()

    def _render_msg_detail(self, d: dict) -> None:
        """右栏的"消息判断"视图：一条消息 + 私聊那套指标 + 返回按人判断。"""
        self._clear_right()
        self.r_hint.setVisible(False)     # 这句是"按人判断"的提示，消息视图里不该露（真机截图抓到过）
        r = self.msg_result
        nick, text = self.msg_sel or ("", "")
        fm = QFontMetrics(self.r_head.font())
        self.r_head.setText("消息判断 · " + (nick or "?")
                            + " 「" + fm.elidedText(text, Qt.ElideRight, 90) + "」")
        back = QPushButton("← 返回按人判断")
        back.setObjectName("link")
        back.setCursor(Qt.PointingHandCursor)
        back.clicked.connect(self.on_msg_back)
        self.r_body.addWidget(back)
        if not r:
            self.r_body.addWidget(QLabel("正在判这条…（只送这一条 + 前后文；判过的不再花钱）"))
            self._sync_scroll_height()
            return
        ti, sn = r.get("true_intent") or r.get("role") or "", r.get("she_needs") or ""
        lit = r.get("literal")
        has_sub = (lit if lit is not None else 1.0) < 0.5
        pairs = [("真实意图", label(ti), ti != "insufficient_info"),
                 ("对方需要", label(sn), sn != "insufficient_info"),
                 ("危险等级", f"{r.get('danger', 0):.0f}/9 {r.get('danger_word', '')}",
                  r.get("danger", 0) >= 4),
                 ("有潜台词", "有" if has_sub else "没有", has_sub),
                 ("该不该回", label(r.get("worth", "")),
                  r.get("worth") in ("should_reply", "must_reply_now")),
                 # 分数越高越**不足**（0=完全够用…3=严重不足）→ 必须带上等级文字，
                 # 否则 "2.40 / 3" 会被读成"挺充分"（用户看不懂的一半原因在这）
                 ("信息充分度", f"{suff_level(r.get('sufficiency', 0))}"
                                f"（{r.get('sufficiency', 0):.2f} / 3）",
                  r.get("sufficiency", 0) > 1.5)]
        self._kv_pairs(pairs)
        self._render_risk_block(r.get("risks") or {})
        lv = QLabel(f"{STATE_LABELS.get(r.get('state','silent'), r.get('state',''))}　"
                    f"关注度 {r.get('attention', 0)} / 10")
        lv.setObjectName("lv")
        lv.setStyleSheet(f"font-size:12px;font-weight:600;"
                         f"color:{STATE_COLOR.get(r.get('state'), '#1f1f1f')}")
        self.r_body.insertWidget(1, lv)
        if r.get("state_reasons"):
            rr = QLabel("触发：" + "，".join(r["state_reasons"]))
            rr.setObjectName("muted")
            rr.setWordWrap(True)
            self.r_body.insertWidget(2, rr)
        tail = (f"把握 {r.get('worth_conf', 0):.2f} · {r.get('model')} · "
                f"{(r.get('usage') or {}).get('input_tokens','?')} tok")
        if r.get("cached"):
            tail = "（本机缓存）" + tail
        self.foot.setText("本机处理 · 只发这一条消息 + 前后文用于判断　·　" + tail)
        self._sync_scroll_height()

    def on_msg_back(self) -> None:
        """从"消息判断"回到"按人判断"。"""
        self.msg_sel = None
        self.msg_result = None
        d = self.results.get(self.selected or "")
        if d:
            self._show_detail(d)

    def _clear_right(self, keep_hint: bool = False):
        """清空右栏。

        `deleteLater()` 要等事件循环跑到 DeferredDelete 才真销毁，中间这段时间旧控件
        **还画在原地** —— 点一条消息时"按人详情 → 消息判断"连续两次重画，就会看到两层
        文字叠在一起（真机截图里抓到过：`有事要你办` 压着 `有事要你办`、`应该回` 压着
        `判不了`）。所以先 hide 再排队销毁，画面当帧就是干净的。
        """
        while self.r_body.count():
            it = self.r_body.takeAt(0)
            w = it.widget()
            if w is not None:
                w.hide()
                w.deleteLater()
                continue
            sub = it.layout()          # 子布局里的控件：takeAt 拿不到，得自己下钻（否则永久留在屏幕上）
            if sub is not None:
                self._drop_layout_widgets(sub)
        if not keep_hint:
            self.r_hint.setVisible(True)

    def _drop_layout_widgets(self, lay) -> None:
        while lay.count():
            it = lay.takeAt(0)
            w = it.widget()
            if w is not None:
                w.hide()
                w.deleteLater()
                continue
            sub = it.layout()
            if sub is not None:
                self._drop_layout_widgets(sub)

    def _kv_cell(self, k, v, hot=False, budget: int = KV_BUDGET_2COL) -> QWidget:
        """一个"键 值"单元格：**键定宽、值紧跟其后**（用户："值太远了"）。

        budget = 值最多占多少像素，超长值省略（完整值进 tooltip）。两列排布时一格只有
        ~170px，必须省略；**一行一对时按右栏整宽给足**，不再省略 —— 否则会出现"明明有
        地方，值却被自己的容器切成半截"（真机截图踩过：`在试探你在不`、`要个具体安排`
        都贴着边断掉）。

        值改成左对齐紧贴键之后，长值反而比以前更有地方（起点更靠左），而短值不再被
        顶到格子最右边。`setMaximumWidth` 是为了**别被拉伸**：一行两格时如果让格子撑满，
        值和键又会分开 —— 宁可整行右边留白。
        """
        w = QWidget()
        h = QHBoxLayout(w)
        h.setContentsMargins(0, 0, 4, 0)
        h.setSpacing(KV_GAP)
        a = QLabel(k)
        a.setObjectName("k")
        a.setFixedWidth(KV_LABEL_W)          # 各格的键对齐，值也就跟着对齐
        b = QLabel(str(v))
        b.setObjectName("vh" if hot else "v")
        b.setWordWrap(False)
        full = str(v)
        shown = QFontMetrics(b.font()).elidedText(full, Qt.ElideRight, max(40, budget))
        b.setText(shown)
        if shown != full:
            b.setToolTip(full)
        h.addWidget(a)
        h.addWidget(b)
        h.addStretch(1)                      # 留白全在值后面
        w.setMaximumWidth(KV_LABEL_W + KV_GAP + max(40, budget) + 12)
        return w

    def _pairs_two_columns(self) -> bool:
        """右栏够不够宽排两列键值？不够就一行一对（值占满整宽，面板宽度也不用加宽）。"""
        w = self.right_panel.width() or self.right_panel.minimumWidth()
        return w >= 260

    def _kv_pairs(self, pairs) -> None:
        """把若干「键/值」两两排成一行 —— 右栏原本一行一对，17 行放不下。

        用宽度换高度：每行两对，高度差不多砍半（代价是右栏最小宽度要放宽，
        面板宽度有下限兜着，不会把值裁掉）。
        """
        if not self._pairs_two_columns():
            # 右栏放不下两列 → 一行一对：值按整宽省略（基本不省略），不漏内容
            budget = (max(40, self.right_panel.width() - KV_LABEL_W - KV_GAP - 14)
                      if self.right_panel.width() else 96)
            for k, v, hot in pairs:
                self.r_body.addWidget(self._kv_cell(k, v, hot, budget=budget))
            return
        row = None
        for i, (k, v, hot) in enumerate(pairs):
            if i % 2 == 0:
                row = QWidget()
                rl = QHBoxLayout(row)
                rl.setContentsMargins(0, 0, 0, 0)
                rl.setSpacing(10)
                self.r_body.addWidget(row)
            # 「信息充分度」的值带等级 + 分数（如「不太够（2.40 / 3）」）比别的值长，
            # 用两列那点预算（96px）会被省略成「大致够（1.40 / …」——单独给它更宽的一格。
            budget = 150 if k == "信息充分度" else KV_BUDGET_2COL
            row.layout().addWidget(self._kv_cell(k, v, hot, budget=budget), 1)

    def _kv(self, k, v, hot=False):
        """单行「键 值」——风险行用它。

        与 `_kv_cell` 同一套排法（键定宽、值紧跟），否则风险行的百分比还贴在面板最右边，
        跟上面刚改好的键值区块对不齐，一眼就能看出两种风格。
        """
        w = QWidget()
        h = QHBoxLayout(w)
        h.setContentsMargins(0, 0, 0, 0)
        h.setSpacing(KV_GAP)
        a = QLabel(k)
        a.setObjectName("k")
        a.setFixedWidth(KV_LABEL_W)
        if isinstance(v, QWidget):
            b = v                      # 已经是个控件（例如可点的 ClickLabel）→ 别再包一层
        else:
            b = QLabel(str(v))         # 曾经把控件传进来被 str() 成 repr，标签成了孤儿（自检抓到的）
            b.setObjectName("vh" if hot else "v")
            b.setWordWrap(True)
        h.addWidget(a)
        h.addWidget(b)
        h.addStretch(1)
        self.r_body.addWidget(w)

    def _risk_level(self, v: float) -> tuple[str, bool, bool]:
        """概率 → (等级文字, 是否高亮, 是否算"门控命中")。

        **档位锚在已校准的门控阈值上**（不是随手定的 60/30），这样"面板显示什么"与
        "面板会怎么做"永远一致，而且跟着设置里的两个门槛走：
          高 = ≥ 最高打扰门槛（STRONG，默认 0.75）—— 单类就够印证，会进 ⚠
          中 = ≥ Noul 门槛（NOUL_HIT，默认 0.35）—— 门控认它算"命中"，但还需第二个信号
          低 = 其余（仍然列出来，只要 ≥8%）
        用户 2026-09-24：默认给人看的应该是"要不要管"，概率是想深究时才看的。
        """
        from . import qset as _q        # 运行时取值：设置里改了门槛，档位立刻跟着变
        strong = float(getattr(_q, "STRONG", 0.75))
        hit = float(getattr(_q, "NOUL_HIT", 0.35))
        # 两个门槛都在设置里可调，可能被调成"中门槛 > 高门槛"（自检就构造过这种组合）——
        # 那会让"中"永远达不到、档位失去意义。取夹取值，保证 高 >= 中 恒成立。
        mid = min(hit, strong)
        if v >= strong:
            return "高", True, True
        if v >= mid:
            return "中", True, True
        return "低", False, False

    def _render_risk_block(self, risks: dict) -> None:
        """右栏的「风险」区块（按人详情与单条消息判断共用）。

        默认显示 高/中/低；标题或任意一行**点一下**就在"等级 ⇄ 具体概率"之间切换
        （只重画，不重新判断、不花钱）。切换状态存在 `self._risk_pct`，跨会话保留。
        """
        title = ClickLabel("风险　" + ("点一下看等级" if self._risk_pct
                                    else "点一下看具体概率"))
        title.setObjectName("k")
        title.setToolTip("概率是模型对四类危害分别给的可信度；"
                         "高 = 单类就够印证（≥0.75），中 = 门控认它算命中（≥0.35），"
                         "低 = 其余（仍会列出）。点一下切换显示方式。")
        title.clicked.connect(self._toggle_risk_pct)
        self.r_body.addWidget(title)
        any_risk = False
        for k, v in sorted((risks or {}).items(), key=lambda kv: -kv[1]):
            if v < 0.08:
                continue
            any_risk = True
            lvl, hot, _hit = self._risk_level(v)
            val = ClickLabel(f"{round(v * 100)}%" if self._risk_pct else lvl)
            val.setObjectName("vh" if hot else "v")
            val.setToolTip(f"{RISK_LABELS.get(k, k)}：{round(v * 100)}%"
                           f"（等级 {lvl}；点一下{'看等级' if self._risk_pct else '看具体概率'}）")
            val.clicked.connect(self._toggle_risk_pct)
            self._kv("　" + RISK_LABELS.get(k, k), val)
        if not any_risk:
            self._kv("　无", "都低于 8%")

    def _toggle_risk_pct(self) -> None:
        """等级 ⇄ 概率：只重画右栏，不重新判断（也不花钱）。"""
        self._risk_pct = not self._risk_pct
        dbg(f"风险显示切换 → {'概率' if self._risk_pct else '高中低'}")
        nick = self.selected or ""
        d = self.results.get(nick)
        if self.msg_sel and self.msg_sel[0] == nick and d:
            self._render_msg_detail(d)
        elif d:
            self._show_detail(d)

    def _show_detail(self, d: dict):
        self._clear_right()
        self.r_hint.setVisible(False)
        # 点了某条消息 → 右栏切成"消息判断"（带返回）。这样重画时状态不会丢。
        if self.msg_sel and self.msg_sel[0] == d.get("nickname"):
            self._render_msg_detail(d)
            return
        # 标题不要"引一句"当预览：那会让人以为"只分析了这一句"（用户就问了这个问题）。
        # 改为在标题下写清**判断范围**：这一个人的这一批消息（屏幕上 N 条 + 本机累积 M 条 + 整段对话 K 条）。
        self.r_head.setText("判断 · " + (d["nickname"] or "?"))
        n_recent = len(d.get("msgs") or [])
        n_early = len(d.get("earlier") or [])
        n_ctx = int(d.get("ctx_n") or 0)
        scope = f"依据：最近 {n_recent} 条"
        if n_early:
            scope += f" + 本机累积 {n_early} 条"
        if n_ctx:
            scope += f" + 整段对话 {n_ctx} 条"
        scope += "（按人整体判断，不是逐句）"
        sc = QLabel(scope)
        sc.setObjectName("muted")
        sc.setWordWrap(True)
        self.r_body.addWidget(sc)

        st = d["state"]
        attn_txt = (f"关注度 {d['attention']} / 10"
                    if d.get("attention_mass", 1.0) >= 0.5
                    else f"关注度 —（判断不了占了大头，质量 {d.get('attention_mass',0):.2f}）")
        lv = QLabel(f"{STATE_LABELS.get(st, st)}　{attn_txt}")
        lv.setObjectName("lv")
        lv.setStyleSheet(f"font-size:12px;font-weight:600;"
                         f"color:{STATE_COLOR.get(st, '#1f1f1f')}")
        self.r_body.addWidget(lv)
        if d["state_reasons"]:
            rr = QLabel("触发：" + "，".join(d["state_reasons"]))
            rr.setObjectName("muted")
            rr.setWordWrap(True)
            self.r_body.addWidget(rr)

        # 私聊与群聊判的不是同一批东西：私聊看的是潜台词与危险等级（没有"群角色"，
        # 也没有"是否在点我"——私聊里当然是在跟我说话），标签跟着换，别张冠李戴。
        _dm = d.get("scene") == "dm"
        ti = d["role"] or d.get("true_intent", "")
        sn = d.get("she_needs", "") if _dm else d["intent"]
        # `literal` 是"字面意思"的把握：0.0 表示**确定有潜台词**。
        # 旧写法 `(d.get("literal") or 1) < 0.5` 在 0.0 时被 `or` 兜底成 1，
        # 显示恰好相反（§6.6 复核）。这里只对 None 兜底。
        lit = d.get("literal")
        has_sub = (lit if lit is not None else 1.0) < 0.5
        pairs = [("真实意图" if _dm else "角色", label(ti), ti != "insufficient_info"),
                 ("对方需要" if _dm else "意图", label(sn), sn != "insufficient_info")]
        if _dm:
            pairs.append(("危险等级", f"{d.get('danger', 0):.0f}/9 {d.get('danger_word', '')}",
                          d.get("danger", 0) >= 4))
            pairs.append(("有潜台词", "有" if has_sub else "没有", has_sub))
        else:
            pairs.append(("是否在点我", f"{round(d['addressed_to_me']*100)}%",
                          d["addressed_to_me"] > 0.5))
        pairs.append(("该不该回", label(d["worth"]),
                      d["worth"] in ("should_reply", "must_reply_now")))
        pairs.append(("信息充分度",
                      f"{suff_level(d['sufficiency'])}（{d['sufficiency']:.2f} / 3）",
                      d["sufficiency"] > 1.5))
        self._kv_pairs(pairs)          # 两列排布：原来是 6 行，现在 3 行

        self._render_risk_block(d.get("risks") or {})

        if d["msgs"]:
            mk = QLabel("他最近的消息")
            mk.setObjectName("k")
            self.r_body.addWidget(mk)
            shown = d["msgs"][:MSG_ROWS]      # 上限见 MSG_ROWS（每多一行，面板就要多长一截）
            self._last_msg_rows = len(shown)  # 供自检断言"真的只渲染了 N 条"
            for m in shown:
                # **显示全文 + 可点**：全文折行（用户要求，别截断）；
                # 每条都能点 —— 点一下就单独判这一条（用户要求）
                row = MsgRow(m)
                row.clicked.connect(self.on_msg_click)
                self.r_body.addWidget(row)

        earlier = d.get("earlier") or []
        total = d.get("hist_total") or len(earlier)
        if total:
            # 累积数**始终显示**：否则"攒的这几条正好都在屏幕上"看起来就像没累积
            # （用户报"我看了阿泽的消息并没有累积"，指的就是这个观感）
            hk = QLabel(f"更早的消息（本机累积 {total} 条"
                        + ("" if earlier else "，都还在屏幕上") + "）")
            hk.setObjectName("k")
            self.r_body.addWidget(hk)
            for m in earlier[-6:]:
                erow = MsgRow(m, muted=True)
                erow.clicked.connect(self.on_msg_click)
                self.r_body.addWidget(erow)

        tail = (f"把握 {d['worth_conf']:.2f} · {d['model']} · "
                f"{d['usage'].get('input_tokens','?')} tok")
        if d.get("cached"):
            tail = "（本机缓存）" + tail
        self.foot.setText("本机处理 · 只发送群成员最近几条消息用于判断　·　" + tail)
        self._sync_scroll_height()
        self.adjustSize()
        self.follow()

    def on_fail(self, msg: str):
        """Worker 报错：不只写在右栏小字里，**状态行与页脚也要写**。

        踩过的坑（用户："怎么卡住了"）：打包版旁边没有 .env → 判断线程初始化就失败、
        之后提交的任务**永远不会被处理**，而面板当时只在右栏留一行小字，进度行一直停在
        "提交判断 · N 人待筛" —— 看起来就是在忙，实际什么都不会发生。
        """
        self.r_hint.setVisible(True)
        self.r_hint.setText(f"调用失败：{msg[:130]}")
        self._hint(f"判断不可用：{msg[:60]}", "#dc2626")
        self.set_progress("完成", 100, f"判断不可用：{msg[:40]}")
        self.foot.setText(f"判断不可用 · {msg[:90]}")
        self._worker_dead = msg
        self.adjustSize()

    def _check_worker(self) -> None:
        """判断线程的看门狗：**有任务在排队，但线程没在跑** → 必须说出来。

        没有它的时候，Worker 一旦没起来（最常见：Key 没配 / 初始化失败），
        面板就永远停在"提交判断 · N 人待筛"，用户只能看到"卡住"。
        """
        w = getattr(self, "worker", None)
        if w is None or self.paused or getattr(self, "_shutting_down", False):
            return                                 # __init__ 期 follow() 会先到，属性可能还没建
        try:
            running = bool(w.isRunning())
            finished = bool(w.isFinished())         # 线程**已结束**才算"死"（没启动过不算）
            pending = bool(getattr(w, "_jobs", None))
        except Exception:
            return                                  # 桩/别的实现没有这些方法 → 不掺和
        if running or finished is False or not pending:
            self._worker_stuck_since = 0.0
            return
        now = time.time()
        if not self._worker_stuck_since:
            self._worker_stuck_since = now
        if now - self._worker_stuck_since < 5:
            return
        if self._worker_dead:
            return                      # 已经报过（on_fail 报的那次更具体）
        reason = "判断线程没在跑"
        try:
            from .jev_engine import load_api_key
            load_api_key(ENV_PATH)      # 只为把"Key 没配"这个最常见原因说准
        except Exception as e:          # noqa: BLE001
            reason = f"缺少 API Key（{e}）"
        tip = f"{reason} —— 把 .env 放在 {PROJECT_DIR} 里（内容一行：jevkey=apikey_…）"
        dbg(f"判断线程卡住：{tip}")
        self._worker_dead = tip
        self._hint(tip[:80], "#dc2626")
        self.set_progress("完成", 100, tip[:46])
        self.foot.setText("判断不可用 · " + tip[:88])

    def _shutdown(self):
        """落盘 + 停线程。幂等：closeEvent 与"菜单退出"两条路都会走到这里。"""
        if getattr(self, "_shutting_down", False):
            return
        self._shutting_down = True
        dbg("关闭：落盘 + 停线程")
        self.store.save()
        try:
            if self.worker.client is not None:
                self.worker.client.audit.flush_now()
        except Exception:
            pass
        self.scanner.stop()
        self.worker.stop()
        self.scanner.wait(1500)
        self.worker.wait(1500)

    def closeEvent(self, e):
        dbg("closeEvent")
        self._shutdown()
        super().closeEvent(e)
        QApplication.quit()


def main():
    if _already_running():
        if activate_existing():
            print("面板已在运行，已把现有实例调到前台。")
        else:
            print("面板已在运行（未找到窗口，可能被隐藏）。")
        return 0
    app = QApplication(sys.argv)
    app.setQuitOnLastWindowClosed(True)
    _install_error_trap()
    try:
        from .settings import sync_thresholds
        sync_thresholds()      # 把本机设置里的阈值同步到 qset（F-22）
    except Exception as e:
        print(f"阈值同步失败：{e}")
    bar = HudBar()
    bar.show()
    print("群聊分诊工具栏已启动（按人 + 自动扫描 + 两段式节流）。")
    rc = app.exec()
    # 显式收尾：QThread 未结束会让解释器退出时挂住，反复启动就堆积僵尸进程
    bar.scanner.stop()
    bar.worker.stop()
    for th in (bar.scanner, bar.worker):
        if not th.wait(3000):
            dbg(f"线程未在 3s 内退出: {th}")
            os._exit(0)
    return rc


if __name__ == "__main__":
    raise SystemExit(main())
