"""真实微信消息采集：截屏 → 像素版面检测 → 逐气泡 OCR → 「谁说的 / 说了什么」。

为什么走 OCR 而不是 UIA（实测结论）：
  微信 4.1.15.12 的 mmui 无障碍树**仅在 SPI_SETSCREENREADER 于微信进程启动时已置位
  才会构建**。未置位时真实聊天窗口（hwnd=986286，896x648）只暴露 1 个节点、零内容。
  已提供 screenreader_flag.py 置位，但**必须重启微信**才生效。在重启之前，
  OCR 是唯一能立刻读到真实内容的通道；重启后 UIA 后端可无缝替换。

## 版式：从像素量，不按窗口比例（2026-09-23 真机实测，见本机自用的版面实测记录（不入库））

旧实现按窗口比例裁切（CHAT_LEFT_R=0.348 / MSG_TOP_R=0.155 / MSG_BOTTOM_R=0.735），
只在窗口宽 885px 附近才对：宽 1243 时左边界算成 432、真实 308，吃掉聊天区左边 124px
（头像 + 每句话开头）；纵向 0.155×H 在 981 高的窗口上给出 152，真实一直是 80。
根因：**会话列表是固定像素宽度**，窗口变宽时多出来的宽度全给聊天区 —— 比例法结构性失效。

现在每帧现算：
  1. 面板底色 = 帧右半边众数色（不写死颜色，深浅主题都成立）
  2. pane_left = 列方向"面板底色占比 > 30%"的第一列（实测 285@宽657、308@宽780+）
  3. msg_top   = 聊天区内第一条"整行非底色占比 > 0.90"的行（实测 = 顶边 + 80）
  4. msg_bot   = 聊天区内 60% 高度以下第一条同样的行（实测 = 底边 − 152）
  5. 检测不出来 → 写进 view.error 并返回。**绝不回退到比例硬裁**（那正是要拆掉的东西）。

## 坐标：全部用**帧坐标**（GetWindowRect 那个矩形），不混用

`GetWindowRect` 比 DWM 可见边界大 7px（左/右/下各 7px 不可见边框，spec §1）。
抓帧抓的就是 GetWindowRect 的矩形（`wechat_window.WinInfo.rect` → `grab()`），
所以帧内一切都一致，但必须知道：**帧右边 − 7 = DWM 可见右边界**
（`wechat_window.visible_rect_of()` 可现取对照）→ 聊天区右界取 W−7、右头像列取 W−70。
混用屏幕/可见/客户区坐标就是 7px 系统性偏移。

## 消息切分：一条气泡 = 一条消息

在 [msg_top, msg_bot) × [pane_left, 帧可见右界) 内取非底色掩码 → 连通域 → 按判据分类。
掩码只取**消息区本身**：输入区的输入框描边、工具栏图标、头部的标题与公告条根本不进掩码，
不会和气泡粘成一块；反过来"碰到掩码边界"就等价于"在竖向上被切过" → 整条丢弃。
为什么不能按行距切：**气泡内行距 38px ≈ 气泡间间距 41px**，几何阈值分不开"同一气泡的
第二行"和"下一个气泡"（旧几何法把 "GPT-6 Sol high" 当成了发言人）。只能靠气泡底色连通域。
时间戳/日期/昵称行/引用行/系统提示都不是气泡 → 天然不进 OCR，因此旧正则
（TIME_RE / DATE_RE / NEWMSG_RE / SYS_RE / is_noise）全部退役，不再需要文字层过滤。

## 单聊（spec §5，657×981 实测）

版面 chrome 与群聊同一套（pane_left / msg_top=80 / msg_bot=底边−152 / 左右头像列都一样），
但**单聊没有昵称行**，而气泡上方那条带会读到**无气泡的系统提示**（"以上是打招呼的消息"、
"XX撤回了一条消息"）——它挡不住昵称校验，会被误当说话人。所以读昵称这一步是**可关的**
（`parse(..., read_names=...)`），单聊分支见 parse 的 docstring。
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

import cv2
import mss
import numpy as np

# ---------------- 版面检测参数（spec §1 实测）----------------
BG_TOL = 12              # 与面板底色的"同色"判定：三通道绝对差之和 ≤ 12
PANE_COL_BG_MIN = 0.30   # 列方向底色占比 > 0.30 → 该列属于聊天面板
ROW_NONBG_MIN = 0.90     # 整行非底色占比 > 0.90 → 版面分隔行（消息区上下边界）
MIN_W, MIN_H = 400, 260  # 再小的窗口连版面都铺不开，直接报错而不是硬裁
MIN_CHAT_W = 200         # 聊天区最小宽度
MIN_MSG_H = 100          # 消息区最小高度
# msg_bot 取"60% 高度以下的第一条分隔行"（原型实测规则），所以窗口太矮时压根找不到它：
# 需要 H − 152 > 0.6H，即 H > 约 380px。小于这个高度会走 view.error，不会静默乱猜。

RIGHT_BORDER_PX = 7      # GetWindowRect 右侧那 7px 不可见边框（见模块头"坐标"一节）
# 标题条：实测 896x648 窗口里标题在 y 40–75（msg_top=80），所以取 [msg_top-40, msg_top-5]。
# 但**裁剪带太窄会整行检不出**：实测 2 个字的标题（16px）在 35px 高的带里 OCR 返回空，
# 把带加高到 47px 就能读出来（检测器在文字贴着裁剪边、四周没余量时会丢掉整行）。
# 带往上多取 12px 只是头部那块纯底色，不会带进别的东西，所以按 52 取。
TITLE_ABOVE = 52
TITLE_BELOW = 5

# ---------------- 连通域判据（spec §2 实测）----------------
MIN_COMP_AREA = 300      # 噪声地板：小于它的像素团（时间戳、图标）直接不要
AVATAR_MIN, AVATAR_MAX = 20, 70        # 头像 36×36 见方（实测各档一致）
AVATAR_AR_MIN, AVATAR_AR_MAX = 0.6, 1.8   # 长宽比（原型真机跑的是 1.6，spec §2 定为 1.8）
AVATAR_FLAT_MAX = 0.5    # 头像主色占比低（照片头像没有主色）
BUBBLE_MIN_H, BUBBLE_MIN_W = 24, 28    # 「嗯」的气泡只有 43px 宽，阈值取 55 会漏
BUBBLE_FLAT_MIN = 0.35   # 气泡底色单一 → 主色占比高
BUBBLE_SOLID_MIN = 0.4   # 实心度 area/(w×h)：挡掉输入框描边、以及被裁断气泡与描边粘连
IMG_MIN_AREA, IMG_MIN_W = 2500, 60     # 图片/表情：足够大的实心非平底色块
IMG_FLAT_MAX = 0.55
IMG_SOLID_MIN = 0.4
SCROLLBAR_EDGE = 18      # 滚动条：贴右边缘（x+w > W−18）且高 > 60 的细长条
SCROLLBAR_MIN_H = 60
CLIP_MIN_H = 12          # 贴消息区边界且高 ≥12px 的块 = 被裁断的残块（1px 分隔线不算）
TOP_CLIP_TOL = 1         # 边界上的分隔线可能吃掉被裁块的第一行，所以留 1px 容差
VIS_MIN_H = 20           # 可见高度下限；不足或贴着边界 → 整条丢弃

# ---------------- 昵称条读法（spec §2/§3 实测）----------------
# 昵称行在气泡上方约 12px 处。条带相对气泡左上角：(y-32 .. y-4)、宽约 170px。
NAME_BAND_TOP, NAME_BAND_BOT = -32, -4
NAME_BAND_X = 62         # 相对 pane_left：左头像列在 +20、宽 36 → +56 之后留 6px 间隙
NAME_BAND_W = 170
NAME_MIN_ROOM = 16       # 上一条同侧气泡与本条之间的空隙 < 16px 就放不下 12px 的昵称行
NAME_INK_MIN = 0.005     # 条带里非底色像素占比低于它 → 这块底是纯的，确认没有昵称行
NAME_SCALE = 2.0         # **必须放大 2x**：12px 小字 max/4000 不放大检不出来
                         # （实测 "阿泽"→空、"X7"→0.502；放大 2x → 0.592 / 0.664）
NAME_PAD = 4             # 昵称条补一圈底色边再 OCR（文字顶着裁剪边会整行检不出，见 _pad_bg）
NAME_MIN_SCORE = 0.35
NAME_MAX_LEN = 12        # 昵称长度上限（原型实测值）；再长基本是把整行消息读进来了
IMAGE_NAME_DX = 44       # 图片/表情消息的昵称**与头像同一行**（不在头像上方）
IMAGE_NAME_DY = 2
IMAGE_NAME_W, IMAGE_NAME_H = 170, 26
IMAGE_NAME_MIN_SCORE = 0.35

# ---------------- 头像列（spec §1 实测）----------------
AVATAR_COL_LEFT = 20     # 左头像列 x = 聊天区左 + 20
AVATAR_COL_RIGHT = 70    # 右头像列 x = 帧右边 − 70
AVATAR_COL_TOL = 14      # 头像允许的横向偏移

# 正文 OCR：det 用 max/4000，**不放大**。实测比默认 min/736 快 8 倍、准确率逐字相同
# （spec §3：逐气泡 83ms vs 688ms；默认配置会把 155×36 的小气泡放大 20 倍再检测）。
TEXT_MIN_SCORE = 0.30
# 群名末尾的成员数：「青柠设计组项目群(39)」。
# 右括号**可选**：括号是细笔画，实测（深色主题合成帧）标题 OCR 会掉右括号读成
# 「…(39」——这会直接决定 is_group，而 is_group 又决定要不要读昵称（见 parse），
# 一个细笔画不该让整份说话人归因塌掉。
GROUP_RE = re.compile(r"[(（]\s*(\d{1,5})\s*[)）]?")
# 纯数字/日期：时间戳行（21:45）与日期行（9月22日 星期二 20:29）长得和昵称一样。
# 注意**不能反向用"含数字"过滤**：那会误杀 `X7` 这类昵称，进而让说话人被继承成上一条。
NAME_NUMERIC_RE = re.compile(r"^[\d\s:：.月日年星期一二三四五六日/-]+$")

IMG_TEXT = "[图片/表情]"
UNREADABLE_TEXT = "[文字未识别]"   # 气泡在、文字读不出来时的占位（下去给模型看的东西）


@dataclass
class Layout:
    """一帧的版面。字段全部是**帧坐标**（见模块头"坐标"一节）。"""
    pane_left: int
    msg_top: int
    msg_bot: int
    right: int                       # 聊天区右边界 = 帧右边 − 7px（DWM 可见边界）
    bg: tuple = (0, 0, 0)            # 面板底色（本帧众数色）

    @property
    def center(self) -> float:
        return (self.pane_left + self.right) / 2


@dataclass(frozen=True)
class Block:
    """一个连通域块。kind: bubble / image / avatar。"""
    x: int
    y: int
    w: int
    h: int
    area: int
    flat: float                      # 主色占比
    solid: float                     # 实心度 area/(w×h)
    fill: tuple
    kind: str = ""

    @property
    def cx(self) -> float:
        return self.x + self.w / 2


@dataclass
class Line:
    text: str
    x0: int
    x1: int
    y0: int
    y1: int
    score: float
    direction: str = "unknown"   # in / out
    kind: str = "msg"            # msg（逐气泡体制下不再产出 name 行）
    sender: str = ""             # 发言人（气泡所在侧的头像列 + 头像旁昵称行）
    media: bool = False          # 图片/表情：text 是占位符，不是读到的文字
    empty: bool = False          # 气泡在、但 OCR 没读出文字（单字气泡实测读不出）

    @property
    def cx(self) -> float:
        return (self.x0 + self.x1) / 2


@dataclass
class ChatView:
    title: str                  # 会话标题（来自 OCR，**不要用窗口标题**）
    is_group: bool
    member_count: int | None
    lines: list = field(default_factory=list)      # 逐气泡的会话记录
    size: tuple = (0, 0)
    error: str | None = None
    layout: "Layout | None" = None                 # 本帧检测出的版面（诊断/测试用）
    dropped: list = field(default_factory=list)    # 被丢弃的边界残块（诊断/测试用）

    @property
    def messages(self) -> list:
        """真正的消息。逐气泡体制下 lines 里除了消息没有别的（时间戳/引用行/系统提示
        都不会进 OCR），所以这里只按 kind 过滤。"""
        return [x for x in self.lines if x.kind == "msg"]

    @property
    def incoming(self) -> list:
        return [x for x in self.messages if x.direction == "in"]

    @property
    def latest_incoming(self) -> Line | None:
        ins = self.incoming
        return ins[-1] if ins else None

    def recent(self, n: int = 8) -> list:
        """最近 n 条记录，带方向标注，供 Jev 作为 state 上下文。"""
        return list(self.lines)[-n:]

    @staticmethod
    def _text_of(ln: Line) -> str:
        # 读不出文字/图片的表情符号不能变成空字符串下去：模型看到空串会当成"没说话"，
        # 而漏读必须是看得见的（spec §3：单字气泡 8 种参数组合全空 → 引擎能力边界）。
        if ln.text:
            return ln.text
        return IMG_TEXT if ln.media else UNREADABLE_TEXT

    def speakers(self, max_each: int = 6) -> list:
        """按发言人分组，最近发过言的排前面 —— 两段式分诊的输入。

        返回 [(昵称, [该人最近的消息...])]，不含"我"自己发的。
        """
        seq = [(ln.sender or "?", self._text_of(ln)) for ln in self.messages
               if ln.direction != "out"]
        if not seq:
            return []
        order: dict = {}
        for i, (sp, _) in enumerate(seq):
            order[sp] = i          # 越大越近
        by: dict = {}
        for sp, tx in seq:
            by.setdefault(sp, []).append(tx)
        return [(sp, by[sp][-max_each:])
                for sp in sorted(by, key=lambda x: -order[x])]

    def recent_messages(self, n: int = 5) -> list:
        """最近 n 条真实消息，形如 [(发言人, 内容)]，按时间顺序。"""
        return [(x.sender or "?", self._text_of(x)) for x in self.messages[-n:]]


# ---- 跨轮记忆 ----
# 归因不再依赖跨轮记忆：昵称行现在直接从像素读（有墨迹/没墨迹 + 2x OCR），
# 因此"消息正文里恰好是个名字"的结构性误判已经消失了。
# NameMemory 保留下来只是为了让 hud.py 的既有调用不变，并留一份见过的昵称统计。
NAME_CONFIRM_AT = 2          # 累计出现多少次才确认为昵称


@dataclass
class NameMemory:
    """跨轮累积的昵称统计（新体制下不再参与归因，只做记录）。"""
    confirmed: set = field(default_factory=set)
    counts: dict = field(default_factory=dict)

    def observe(self, name: str) -> None:
        if not name:
            return
        self.counts[name] = self.counts.get(name, 0) + 1
        if self.counts[name] >= NAME_CONFIRM_AT:
            self.confirmed.add(name)

    def is_confirmed(self, name: str) -> bool:
        return name in self.confirmed


@dataclass
class TitleMemory:
    """标题变化慢，用跨轮多数表决压掉 OCR 错字（实测：青柠→老糖、项目→龙奶下）。"""
    recent: list = field(default_factory=list)
    keep: int = 5

    def vote(self, candidate: str) -> str:
        if candidate:
            self.recent.append(candidate)
            self.recent = self.recent[-self.keep:]
        if not self.recent:
            return ""
        counts: dict = {}
        for x in self.recent:
            counts[x] = counts.get(x, 0) + 1
        return max(counts.items(), key=lambda kv: kv[1])[0]


_ocr = None


def _get_ocr():
    """OCR 单例，det 用 max/4000 而不是默认的 min/736。

    实测（spec §3，7 行标准答案 × 5 种配置）：准确率逐字相同、差异全在耗时 ——
    逐气泡 83ms vs 默认 688ms，整块+默认+2x（旧实现）2940ms。默认 min 会把
    155×36 的小气泡放大 20 倍（面积 418 倍）再检测，纯浪费。
    """
    global _ocr
    if _ocr is None:
        from rapidocr_onnxruntime import RapidOCR
        try:
            # 1.2.3 里 det kwargs 必须成套传，否则 KeyError: 'model_path'
            _ocr = RapidOCR(det_model_path=None, det_limit_type="max",
                            det_limit_side_len=4000, use_angle_cls=False)
        except Exception as e:
            # 降级到默认 min/736：本文件实测默认配置**慢 8 倍**（2940ms vs 83ms），
            # 不吭声的话用户只会觉得"面板突然变卡"而查不到原因（§6.6 复核）。
            print(f"[OCR_WARN] det 参数配置失败，回退默认配置（实测慢约 8 倍）: "
                  f"{type(e).__name__}: {e}", file=sys.stderr, flush=True)
            _ocr = RapidOCR(use_angle_cls=False)
        try:
            # 别信构造函数一定生效：直接把预处理的 limit 参数按住（spec §3 给的另一条路）
            op = _ocr.text_detector.preprocess_op[0]
            if op.limit_type != "max" or op.limit_side_len != 4000:
                op.limit_type = "max"
                op.limit_side_len = 4000
                print("[OCR_WARN] 构造参数未生效，已强制改写预处理 limit 为 max/4000",
                      file=sys.stderr, flush=True)
        except Exception as e:
            print(f"[OCR_WARN] 预处理参数无法校验（可能是 rapidocr 版本差异），"
                  f"按默认配置运行: {type(e).__name__}: {e}",
                  file=sys.stderr, flush=True)
    return _ocr


def grab(rect: tuple) -> np.ndarray:
    l, t, r, b = rect
    with mss.MSS() as sct:
        img = np.array(sct.grab({"left": l, "top": t,
                                 "width": r - l, "height": b - t}))
    return img[:, :, :3][:, :, ::-1]  # BGRA -> RGB


def frame_hash(img: np.ndarray) -> str:
    """整帧哈希，用来判断"这一屏和上一屏是不是一模一样"。

    用途：OCR 实测 3.8s/轮，而群聊大部分时间画面不变。
    同帧直接跳过 OCR，能把平均负担降一个量级。blake2b 对 1.7MB 约 2ms。
    """
    import hashlib
    return hashlib.blake2b(img.tobytes(), digest_size=16).hexdigest()


# --------------------------------------------------------------------------
# 版面检测
# --------------------------------------------------------------------------
def _bg_mask(img: np.ndarray, bg) -> np.ndarray:
    """与面板底色同色的像素（背景）。用绝对差之和而不是逐通道比较，抗一点压缩噪声。"""
    d = np.abs(img.astype(np.int16) - np.array(bg, dtype=np.int16)).sum(-1)
    return d <= BG_TOL


def detect_layout(img: np.ndarray) -> tuple[Layout | None, str]:
    """从像素量出版面。返回 (Layout, "") 或 (None, 失败原因)。"""
    h, w = img.shape[:2]
    if w < MIN_W or h < MIN_H:
        return None, f"窗口太小（{w}x{h}，至少 {MIN_W}x{MIN_H}）"

    # 面板底色 = 帧**右半边**众数色。取右半边而不是整帧：左边是会话列表（另一种底色），
    # 整帧众数在某些窗口宽度下会落到会话列表上。众数色而不是写死 #F5F5F5，深浅主题都成立。
    sample = img[::6, w // 2::6].reshape(-1, 3)
    if sample.size == 0:
        return None, "取不到底色样本"
    vals, cnt = np.unique(sample, axis=0, return_counts=True)
    bg = tuple(int(v) for v in vals[cnt.argmax()])
    isbg = _bg_mask(img, bg)

    # 聊天区左界：会话列表宽是固定像素，窗口变宽多出来的全给聊天区，
    # 所以只能"量"不能"按比例算"。
    band = isbg[int(h * 0.25):int(h * 0.70)]
    if band.size == 0:
        return None, "取不到纵向样本带"
    colfrac = band.mean(0)
    cols = np.where(colfrac > PANE_COL_BG_MIN)[0]
    if cols.size == 0:
        return None, f"找不到聊天区左边界（没有一列的底色占比 > {PANE_COL_BG_MIN}）"
    pane_left = int(cols[0])
    if w - pane_left < MIN_CHAT_W:
        return None, f"聊天区太窄（左界 {pane_left}，宽 {w}）"

    # 消息区上下界：整行非底色占比 > 0.90 的分隔行。
    # 上界取 4% 高度以下的第一条（跳过窗口标题栏），下界取 60% 高度以下的第一条。
    rf = 1.0 - isbg[:, pane_left:].mean(1)
    rows = np.where(rf > ROW_NONBG_MIN)[0]
    top_cand = rows[rows > h * 0.04]
    bot_cand = rows[rows > h * 0.60]
    if top_cand.size == 0:
        return None, "找不到消息区上边界（没有整行非底色的分隔行）"
    if bot_cand.size == 0:
        return None, "找不到消息区下边界（没有整行非底色的分隔行）"
    msg_top, msg_bot = int(top_cand[0]), int(bot_cand[0])
    if msg_bot - msg_top < MIN_MSG_H:
        return None, f"消息区太矮（{msg_top}..{msg_bot}）"
    return Layout(pane_left=pane_left, msg_top=msg_top, msg_bot=msg_bot,
                  right=w - RIGHT_BORDER_PX, bg=bg), ""


def _dominant(img: np.ndarray, x: int, y: int, w: int, h: int) -> tuple[float, tuple]:
    """块内主色占比与其颜色（主色 = bbox 里出现最多的颜色）。"""
    comp = img[y:y + h, x:x + w].reshape(-1, 3)
    packed = ((comp[:, 0].astype(np.int32) << 16)
              | (comp[:, 1].astype(np.int32) << 8) | comp[:, 2])
    vals, cnt = np.unique(packed, return_counts=True)
    k = int(cnt.argmax())
    p = int(vals[k])
    return cnt[k] / float(len(comp)), ((p >> 16) & 255, (p >> 8) & 255, p & 255)


def _find_blocks(img: np.ndarray, lay: Layout, isbg: np.ndarray):
    """连通域分类：气泡 / 图片 / 头像 / 被丢弃的残块。

    掩码只取**消息区本身**（[msg_top, msg_bot) × [pane_left, lay.right)），两个好处：
      · 输入区（输入框只有描边、内部就是底色）与头部（标题、公告条）的像素不进掩码，
        不会和最后一条被裁断的气泡粘成一个 447×172 的大连通域（spec §2 实测踩过）；
      · 于是"碰到掩码边界"就等价于"这块在竖向上被切过" → 整条丢弃。
    右界取 lay.right（DWM 可见边界）：那之外 7px 是窗口边框外的屏幕像素，不是窗口内容。
    """
    h, w = img.shape[:2]
    mask = np.zeros((h, w), np.uint8)
    mask[lay.msg_top:lay.msg_bot, lay.pane_left:lay.right] = 1
    mask &= (~isbg).astype(np.uint8)
    n, _lab, st, _ = cv2.connectedComponentsWithStats(mask, 8)

    bubbles, images, avatars, dropped = [], [], [], []
    for i in range(1, n):
        x, y, bw, bh, area = (int(t) for t in st[i])
        if area < MIN_COMP_AREA:
            continue                      # 噪声地板：零碎像素（时间戳、图标）不成块
        if x + bw > w - SCROLLBAR_EDGE and bh > SCROLLBAR_MIN_H:
            continue                      # 滚动条：贴右边缘的细长条，按位置排除
        flat, fill = _dominant(img, x, y, bw, bh)
        solid = area / float(bw * bh)

        # 被消息区边界裁断的整条丢弃（spec §2：可见高度 < 20px 或跨过消息区底边）。
        # 为什么放在形状判据**之前**：被裁断的块往往和边界那条分隔线连成一个全宽
        # 连通域（框线类物体实心度极低），先按形状判会落进"根本不是气泡"里 ——
        # 结论虽然也是"不报"，但原因丢了、也看不见了。
        if y <= lay.msg_top + TOP_CLIP_TOL or y + bh >= lay.msg_bot:
            if bh >= CLIP_MIN_H:
                why = (f"可见高度只有 {bh}px（< {VIS_MIN_H}）" if bh < VIS_MIN_H
                       else "贴着消息区边界，文本可能不全")
                dropped.append({"x": x, "y": y, "w": bw, "h": bh,
                                "why": f"被边界裁断：{why}"})
            continue

        if (AVATAR_MIN <= bw <= AVATAR_MAX and AVATAR_MIN <= bh <= AVATAR_MAX
                and AVATAR_AR_MIN <= bw / float(bh) <= AVATAR_AR_MAX
                and flat < AVATAR_FLAT_MAX):
            avatars.append(Block(x, y, bw, bh, area, flat, solid, fill, "avatar"))
            continue

        # 主导色是不是就是面板底色？两种东西都是：①真图片/照片 ②截图或白底图 ——
        # 它们直接贴在面板上、**没有气泡底色**。实测：车轮规格截图被当成文字气泡，
        # 内部文字（`) 13"`、`10″`）被 OCR 出来当消息正文喂给模型。
        near_bg = int(np.abs(np.array(fill, np.int32)
                             - np.array(lay.bg, np.int32)).sum()) <= BG_TOL

        # 图片/表情必须在气泡判据**之前**判：图块也常是"实心、主色占比中高"，
        # 顺序反了就会被当成一条气泡消息。
        if (area > IMG_MIN_AREA and bw >= IMG_MIN_W and solid >= IMG_SOLID_MIN
                and (flat < IMG_FLAT_MAX or near_bg)):
            images.append(Block(x, y, bw, bh, area, flat, solid, fill, "image"))
        elif (bh >= BUBBLE_MIN_H and bw >= BUBBLE_MIN_W
                and flat > BUBBLE_FLAT_MIN and solid >= BUBBLE_SOLID_MIN
                and not near_bg):
            bubbles.append(Block(x, y, bw, bh, area, flat, solid, fill, "bubble"))
        # 其余（分隔线、图标、引用行、时间戳行）不是消息，直接不要
    return bubbles, images, avatars, dropped


# --------------------------------------------------------------------------
# OCR 小工具
# --------------------------------------------------------------------------
GRAY_MAX_CONTRAST = 130      # 引用块/时间戳是灰字（实测 ≤93），正文是高对比（实测 178~208）


def _box_contrast(patch: np.ndarray, box) -> float:
    """一个 OCR 框里的字与它自己底色的最大亮度差。

    为什么需要：**引用块与正文在同一个气泡里**。引用块是灰字（"4:51 郑总 拍了拍…"、
    "吴老板: 收到！马上加你" 这种），正文是高对比。整块一起 OCR 会把引用块当消息正文
    喂给模型，于是它看到的是碎片加一串人名，把整个群判成营销群（实测：5 个人全带风险
    芯片、有人被判"广告推广号 100%"，而输入只是"拍了拍"）。
    """
    if patch is None or patch.size == 0:
        return 0.0
    xs = [p[0] for p in box]
    ys = [p[1] for p in box]
    x0, x1 = max(0, int(min(xs))), min(patch.shape[1], int(max(xs)))
    y0, y1 = max(0, int(min(ys))), min(patch.shape[0], int(max(ys)))
    reg = patch[y0:y1, x0:x1]
    if reg.size == 0:
        return 0.0
    u, c = np.unique(reg.reshape(-1, 3), axis=0, return_counts=True)
    base = u[c.argmax()].astype(np.float64) @ np.array([0.299, 0.587, 0.114])
    lum = reg.astype(np.float64) @ np.array([0.299, 0.587, 0.114])
    return float(np.abs(lum - base).max())


def _ocr_patch(patch: np.ndarray, ocr, min_score: float,
               scale: float = 1.0, drop_gray: bool = False) -> tuple[str, float]:
    """对一小块图 OCR → (按行拼起来的文本, 平均置信度)。"""
    if patch is None or patch.size == 0 or patch.shape[0] < 6 or patch.shape[1] < 6:
        return "", 0.0
    if scale != 1.0:
        patch = cv2.resize(patch, None, fx=scale, fy=scale,
                           interpolation=cv2.INTER_CUBIC)
    res, _ = ocr(patch)
    got = []
    for b, text, sc in (res or []):
        s = float(sc)                      # 1.2.3 的 score 是字符串，必须显式转
        if s < min_score or not text.strip():
            continue
        if drop_gray and _box_contrast(patch, b) < GRAY_MAX_CONTRAST:
            continue          # 灰字 = 引用块/时间戳，不是这条消息的正文
        got.append((min(p[1] for p in b), min(p[0] for p in b), text.strip(), s))
    if not got:
        return "", 0.0
    got.sort(key=lambda t: (t[0], t[1]))
    return " ".join(t[2] for t in got).strip(), sum(t[3] for t in got) / len(got)


def _ocr_text(img: np.ndarray, ocr, x: int, y: int, w: int, h: int,
              min_score: float, scale: float = 1.0,
              drop_gray: bool = False) -> tuple[str, float]:
    """按坐标裁一块出来 OCR。drop_gray=True 时丢掉灰字（引用块/时间戳）只留正文。"""
    return _ocr_patch(img[max(0, int(y)):int(y) + int(h),
                          max(0, int(x)):int(x) + int(w)],
                      ocr, min_score, scale, drop_gray)


def _pad_bg(patch: np.ndarray, bg, pad: int) -> np.ndarray:
    """给小块补一圈面板底色的边。

    为什么要补：昵称条是按几何紧贴着裁的，文字很可能正好顶在裁剪边上。实测（深色帧的
    图片消息昵称）：文字顶着左边时 **1x 能读、2x 反而读空**；补 2–4px 底色边后 2x 就正常。
    检测器在没有上下文余量时会整行丢掉，补边是最省事的解法。
    """
    if patch is None or patch.size == 0:
        return patch
    out = np.empty((patch.shape[0] + pad * 2, patch.shape[1] + pad * 2, 3), np.uint8)
    out[:, :] = np.array(bg, np.uint8)
    out[pad:pad + patch.shape[0], pad:pad + patch.shape[1]] = patch
    return out


def _ink_ratio(img: np.ndarray, bg, x: int, y: int, w: int, h: int) -> float:
    """一块区域里"非面板底色"像素的占比 —— 判断这里到底有没有文字墨迹。

    为什么需要：读不到昵称时必须分开两种情况 ——
      · 这块底是纯的（无墨迹）→ 说明微信根本没画昵称行 → 是同一人连发的下一条，可继承
      · 有墨迹却读不出来 → 只能记 ?，**不能继承**（继承会产出看着很确定的错答案）
    """
    patch = img[max(0, int(y)):int(y) + int(h), max(0, int(x)):int(x) + int(w)]
    if patch.size == 0:
        return 0.0
    d = np.abs(patch.astype(np.int16) - np.array(bg, dtype=np.int16)).sum(-1)
    return float((d > BG_TOL).mean())


def _is_nickname(t: str) -> bool:
    """昵称校验：只滤"带冒号"和"纯数字/日期"。

    带冒号的（`名字: 内容`）是引用行，长得和昵称一模一样，只能靠冒号认出来；
    纯数字/日期的是时间戳行与日期行。
    **不要按"含数字"过滤** —— 本意是滤时间戳，却会把 `X7` 这类昵称一起误杀，
    后果是说话人被继承成上一条（实测）。
    """
    if not t or len(t) > NAME_MAX_LEN:
        return False
    if re.search(r"[:：]", t):
        return False
    if NAME_NUMERIC_RE.match(t):
        return False
    return True


def _read_name_band(img: np.ndarray, ocr, lay: Layout, x: int, y: int,
                    w: int, h: int, min_score: float) -> tuple[str, float]:
    """读一条昵称带 → (校验过的昵称, 墨迹占比)。放大 2x（12px 小字不放大检不出来）。"""
    ink = _ink_ratio(img, lay.bg, x, y, w, h)
    if ink < NAME_INK_MIN:
        return "", ink
    patch = _pad_bg(img[max(0, y):max(0, y) + h, max(0, x):max(0, x) + w],
                    lay.bg, NAME_PAD)
    t, _ = _ocr_patch(patch, ocr, min_score, scale=NAME_SCALE)
    return (t if _is_nickname(t) else ""), ink


def _pick_avatar(blk: Block, avatars: list, lay: Layout, mine: bool,
                 used: set) -> "Block | None":
    """给块找它那一侧头像列里最近、且还没被别的块认领的头像。"""
    col = (lay.right - AVATAR_COL_RIGHT) if mine else (lay.pane_left + AVATAR_COL_LEFT)
    best, best_d = None, 1 << 30
    for a in avatars:
        if a in used or abs(a.x - col) > AVATAR_COL_TOL:
            continue
        d = abs((a.y + a.h) - blk.y)
        if d < best_d:
            best, best_d = a, d
    return best


def _name_above(img, ocr, lay: Layout, blk: Block, prev: str,
                prev_bottom: int) -> tuple[str, "str | None"]:
    """读气泡上方的昵称条 → (说话人, 新的 prev)。

    微信只在某人连发消息的**第一条**上标昵称，所以昵称要"粘"到后续气泡上；
    但**继承只能发生在"确认这条没有昵称行"时**，判据全部来自像素证据：
      · 上一条同侧气泡与本条之间的空隙比昵称行还窄 ⇒ 确认没有 → 继承
      · 空隙够宽、但整条没有文字墨迹 ⇒ 确认没有 → 继承
      · 有墨迹却读不出（或读出来像引用行）⇒ "?"，并清空 prev，
        免得下一条继承到更旧的名字
    """
    top = max(blk.y + NAME_BAND_TOP, prev_bottom + 1)
    bot = blk.y + NAME_BAND_BOT
    if bot - top < NAME_MIN_ROOM:
        return (prev or "?"), prev
    name, ink = _read_name_band(img, ocr, lay, lay.pane_left + NAME_BAND_X,
                                top, NAME_BAND_W, bot - top, NAME_MIN_SCORE)
    if name:
        return name, name
    if ink >= NAME_INK_MIN:
        return "?", None
    return (prev or "?"), prev


def _name_beside_avatar(img, ocr, lay: Layout, owner: "Block | None") -> tuple[str, float]:
    """图片/表情消息的昵称**与头像同一行**（不在头像上方）。

    按"上方"读会读到上一条气泡的尾巴，把图挂到错误的人名下（实测踩过）。
    读不到就返回空串，由调用方记 ? —— 绝不继承。
    """
    if owner is None:
        return "", 0.0
    return _read_name_band(img, ocr, lay, owner.x + IMAGE_NAME_DX,
                           owner.y + IMAGE_NAME_DY, IMAGE_NAME_W,
                           IMAGE_NAME_H, IMAGE_NAME_MIN_SCORE)


# --------------------------------------------------------------------------
# 主流程
# --------------------------------------------------------------------------
def message_area(rect: tuple, layout: "Layout | None" = None) -> tuple:
    """微信**消息区**的屏幕矩形 (l, t, r, b) —— 自遮挡判定用。

    有上一帧量出来的版面（Layout）就用它，最准；没有（第一帧）就退到文档里记的实测值：
    单聊/群聊同一套 chrome（pane_left ≈ 0.42×宽、msg_top ≈ 顶边+80、msg_bot ≈ 底边−152）。
    这个回落只用于"判断面板有没有压住消息区"，不参与解析 —— 解析仍然坚持每帧现算。
    """
    l, t, r, b = rect
    if layout is not None:
        return (l + layout.pane_left, t + layout.msg_top,
                l + layout.right, t + layout.msg_bot)
    w = r - l
    return (l + int(w * 0.42), t + 80, r - 7, b - 152)


def parse(img: np.ndarray, names: "NameMemory | None" = None,
          titles: "TitleMemory | None" = None,
          read_names: bool | None = None) -> ChatView:
    """解析一帧。

    read_names: 要不要去读气泡上方的昵称条。None = 按场景自动（群聊读 / 单聊不读）。
      单聊实测（spec §5，657×981）：正文气泡上方那条带**全空** —— 单聊没有昵称行，
      而它正好会读到**无气泡的系统提示**（"以上是打招呼的消息"、"XX撤回了一条消息"），
      这种句子"≤12 字 + 无冒号 + 无标点"的昵称校验挡不住，会被误当说话人。
      所以"读昵称"必须是可关的一步，不要写死。
      TODO(单聊说话人)：单聊的对方就是会话标题，sender 应当取 view.title；本轮交付与
      验证门槛仍以群聊规则为准，单聊先记 ? —— 宁可 ? 也不要一个看着很确定的错名字。
    """
    h, w = img.shape[:2]
    view = ChatView(title="", is_group=False, member_count=None, size=(w, h))

    lay, why = detect_layout(img)
    if lay is None:
        # 检测不出来就说明原因并返回，**不回退到按比例硬裁**：比例裁切正是这次拆掉的东西
        view.error = f"版面检测失败：{why}"
        return view
    view.layout = lay

    ocr = _get_ocr()

    # ---- 标题 ----
    # 标题字号小，直接 OCR 容易误读（实测把「青柠设计组项目群」读成
    # 「老群的龙奶下养殖基地」）。放大 3 倍再识别明显更准。
    y0, y1 = max(0, lay.msg_top - TITLE_ABOVE), max(0, lay.msg_top - TITLE_BELOW)
    title_strip = img[y0:y1, lay.pane_left:]
    if title_strip.size:
        title_strip = cv2.resize(title_strip, None, fx=3.0, fy=3.0,
                                 interpolation=cv2.INTER_CUBIC)
        res, _ = ocr(title_strip)
        if res:
            raw_title = max(res, key=lambda r: float(r[2]))[1].strip()
            # 多轮表决：单次 OCR 的小字号错字会被多数票压掉
            view.title = titles.vote(raw_title) if titles is not None else raw_title
            m = GROUP_RE.search(view.title)
            if m:
                view.is_group = True
                view.member_count = int(m.group(1))

    # 场景分支：**只有群聊才去读昵称条**，单聊一律不读。
    #
    # 依据（spec §5 实测 + 本机单聊窗口实测）：单聊没有昵称行，而气泡上方那条带会读到
    # 无气泡的系统提示（"以上是打招呼的消息"、"XX撤回了一条消息"），"≤12 字 + 无冒号"
    # 的昵称校验挡不住这种句子，会被误当说话人。**实测证据**：在 657x981 的真实单聊窗口上
    # 跑默认设置，7 条消息里出现了 3 个看着很确定的说话人标签（全是噪声）；read_names=False
    # 时全部记 ?（诚实降级）。
    #
    # 判据用会话标题里的成员数：单聊标题是联系人名（没有 (N)）。标题完全没读出来时维持
    # "读"——那种帧 hud.py 会直接提示"读不到会话标题"并跳过，不会拿说话人去分诊。
    # TODO(单聊说话人)：单聊的对方就是会话标题，sender 应当取 view.title；本轮交付与验证
    # 门槛仍以群聊规则为准，单聊先记 ? —— 宁可 ? 也不要一个看着很确定的错名字。
    if read_names is None:
        read_names = view.is_group or not view.title

    # ---- 逐气泡 ----
    bubbles, images, avatars, dropped = _find_blocks(img, lay, _bg_mask(img, lay.bg))
    view.dropped = dropped
    center = lay.center
    lines: list = []
    prev = None                                    # 上一条**读到的**对方昵称
    prev_bottom = {"in": lay.msg_top, "out": lay.msg_top}
    used: set = set()
    # 被丢弃的残块同样占着版面：上边界裁断的残块正好可能压在下面那条消息的昵称条上，
    # 不把它算进"同侧上一块的下沿"，昵称条就会读进残块的尾巴、把说话人读成四不像。
    # 只算**在本条之上**的残块 —— 下面那些（比如最后一条被底部裁断的）与它无关。
    drop_bottom: dict = {"in": [], "out": []}
    for d in dropped:
        side = "out" if (d["x"] + d["w"] / 2) > center else "in"
        drop_bottom[side].append(d["y"] + d["h"])
    for blk in sorted(bubbles + images, key=lambda b: (b.y, b.x)):
        mine = blk.cx > center
        side = "out" if mine else "in"
        above = max([prev_bottom[side]]
                    + [b for b in drop_bottom[side] if b <= blk.y])
        owner = _pick_avatar(blk, avatars, lay, mine, used)
        if owner is not None:
            used.add(owner)
        score = 0.0
        empty = media = False
        if blk.kind == "image":
            text, media = IMG_TEXT, True
            if mine:
                sender = "我"
            elif not read_names:
                sender = view.title or "?"
            else:
                nm, ink = _name_beside_avatar(img, ocr, lay, owner)
                if nm:
                    sender, prev = nm, nm
                else:
                    sender = "?"                    # 读不到就不猜
                    if ink >= NAME_INK_MIN:
                        prev = None                 # 有昵称行但没读出来 → 别让后面继承旧的
        else:
            text, score = _ocr_text(img, ocr, blk.x + 5, blk.y + 4,
                                    blk.w - 10, blk.h - 8, TEXT_MIN_SCORE,
                                    drop_gray=True)
            empty = (text == "")
            if mine:
                sender = "我"
            elif not read_names:
                # 单聊：按头像列分左右（左=对方 右=我），不读昵称行（spec §5：单聊没
                # 有昵称行，那条带会读到系统提示）。对方就是会话标题 —— 之前一律记 ?
                # 会让分诊去判断一个叫"?"的人（实测面板左列真的显示成一个叫 ? 的人）。
                sender = view.title or "?"
            else:
                sender, prev = _name_above(img, ocr, lay, blk, prev, above)
        if names is not None and sender not in ("", "我", "?"):
            names.observe(sender)
        prev_bottom[side] = max(prev_bottom[side], blk.y + blk.h)
        lines.append(Line(text=text, x0=blk.x, x1=blk.x + blk.w,
                          y0=blk.y, y1=blk.y + blk.h, score=score,
                          direction=side, kind="msg", sender=sender,
                          media=media, empty=empty))
    # 一条气泡 = 一条消息（多行也算一条），按纵坐标排序即为会话顺序
    #
    # 已知限制（spec §5）：**带气泡的系统文案**（"我通过了你的朋友验证请求，现在我们可以
    # 开始聊天了"）带对方头像 + 标准左气泡，几何上与真人消息完全一致 → 本轮会被当成一条
    # 正常消息上报。无气泡的系统提示（"以上是打招呼的消息"、"XX撤回了一条消息"）天然出局，
    # 不用管。若要排除前者，做法是对少数固定文案做**小文案白名单**（封闭集合，本来就不是
    # 人说的话）；**不要用"气泡居中"之类的几何判据** —— 实测它会把正常消息（"可以问一下
    # 你多大吗"）误杀。
    view.lines = lines
    return view


def snapshot(rect: tuple, names: "NameMemory | None" = None,
             titles: "TitleMemory | None" = None,
             read_names: bool | None = None) -> ChatView:
    return parse(grab(rect), names, titles, read_names)
