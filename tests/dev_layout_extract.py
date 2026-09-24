# -*- coding: utf-8 -*-
"""版面与逐气泡提取的验证脚本（合成帧，不碰真实截图）。

为什么要合成帧：项目既有约束是"抓到的帧绝不落盘"，所以不能拿真实聊天截图当回归样本。
合成帧的好处是**真值已知**：pane_left / msg_top / msg_bot / 每条消息的说话人与文本都由
本脚本自己画出来，检测结果与真值逐项比对，任一项不符就 FAIL 并给出差异。

帧覆盖 spec §1 实测的三档尺寸与两种会话列表宽度，外加一张深色主题：
  A. 896x648  浅色  pane_left=285   群聊（含单字气泡、时间戳行、引用行、底部裁断块）
  B. 780x981  浅色  pane_left=308   群聊（含三行气泡、图片消息、引用行、顶部裁断块）
  C. 1243x981 深色  pane_left=308   群聊（含图片消息、连续发言继承、底部裁断块）

用法：PYTHONPATH=. python tests/dev_layout_extract.py
退出码：全部通过 0，任一失败 1。
"""
from __future__ import annotations

import sys
import numpy as np
from PIL import Image, ImageDraw, ImageFont

from wechat_triage_hud import wechat_capture as wc

FONT = "C:/Windows/Fonts/msyh.ttc"

# ---- 合成帧里的真实微信几何（spec §1/§2 实测值，不是猜的）----
HEADER_H = 80            # 消息区顶 = 顶边 + 80
INPUT_H = 152            # 消息区底 = 底边 − 152
AVATAR = 36              # 头像 36×36
AVATAR_LEFT_DX = 20      # 左头像列 x = 聊天区左 + 20
AVATAR_RIGHT_DX = 70     # 右头像列 x = 帧右边 − 70
BUBBLE_GAP_X = 16        # 头像与气泡的水平间距
BUBBLE_PAD = 14          # 气泡内左右留白
TEXT_PAD_Y = 8           # 气泡内上留白
LINE_PITCH = 38          # 气泡内行距实测 38px
BUBBLE_GAP_Y = 10        # 同侧相邻气泡的间距
NICK_ABOVE = 26          # 昵称行基线：气泡上方 26..8px（落在 y-32..y-4 条带里）
NICK_H = 12              # 昵称字号（约 12px 小字：不放大 2x 就检不出来）
TEXT_SIZE = 16           # 气泡正文字号
IMAGE_W, IMAGE_H = 160, 120
DESKTOP = (58, 74, 96)   # 帧右边/底边 7px 是窗口隐形边框，抓到的是边框外的屏幕像素
GROUP_TITLE = "青柠设计组项目群(39)"

THEMES = {
    "light": dict(pane=(245, 245, 245), panel_ai=(255, 255, 255), panel_me=(149, 236, 105),
                  text=(17, 17, 17), name=(178, 178, 178), list_bg=(237, 237, 237),
                  rail=(214, 214, 214), line=(224, 224, 224), sys=(153, 153, 153),
                  quote=(150, 150, 150), icon=(120, 130, 140)),
    "dark": dict(pane=(30, 30, 30), panel_ai=(44, 44, 44), panel_me=(62, 181, 117),
                 text=(232, 232, 232), name=(140, 140, 140), list_bg=(38, 38, 38),
                 rail=(24, 24, 24), line=(58, 58, 58), sys=(120, 120, 120),
                 quote=(130, 130, 130), icon=(80, 90, 100)),
}


def _font(size):
    return ImageFont.truetype(FONT, size)


class Frame:
    """一张合成帧：先把微信的版面 chrome 画好，再往上摆消息。"""

    def __init__(self, w, h, theme, pane_left, title=GROUP_TITLE):
        self.W, self.H = w, h
        self.pane_left = pane_left
        self.msg_top = HEADER_H
        self.msg_bot = h - INPUT_H
        self.right = w - 7                      # DWM 可见右边界（帧右边 − 7）
        self.c = THEMES[theme]
        self.title = title
        self.im = Image.new("RGB", (w, h), self.c["pane"])
        self.d = ImageDraw.Draw(self.im)
        self.messages = []                      # 真值：每条消息的说话人/文本/是否媒体
        self.last_in = "?"                      # 左侧"上一个说话人"：连发时要说继承成谁
        self._chrome(title)

    # ---- chrome：图标栏 / 会话列表 / 头部 / 输入区 / 边框外的屏幕像素 ----
    def _chrome(self, title):
        c, d = self.c, self.d
        d.rectangle([0, 0, self.W - 1, self.H - 1], fill=c["pane"])
        d.rectangle([0, 0, 44, self.H - 1], fill=c["rail"])                 # 最左图标栏
        d.rectangle([44, 0, self.pane_left - 1, self.H - 1], fill=c["list_bg"])   # 会话列表
        for i in range(6):                                                 # 列表里的会话项
            y = 16 + i * 74
            d.rounded_rectangle([56, y, self.pane_left - 14, y + 62], radius=6,
                                fill=c["list_bg"])
            d.rectangle([64, y + 12, 100, y + 48], fill=c["icon"])
            d.rectangle([110, y + 18, self.pane_left - 34, y + 28], fill=c["line"])
            d.rectangle([110, y + 34, self.pane_left - 60, y + 42], fill=c["line"])
        d.text((self.pane_left + 16, self.msg_top - 34), title,
               font=_font(16), fill=c["text"])                             # 会话标题
        d.line([self.pane_left, self.msg_top, self.right, self.msg_top], fill=c["line"])
        d.line([self.pane_left, self.msg_bot, self.right, self.msg_bot], fill=c["line"])
        # 输入区：输入框**只有描边**、内部就是面板底色（spec §2 实测：flat=0.86）
        d.rounded_rectangle([self.pane_left + 16, self.msg_bot + 16, self.right - 16,
                             self.msg_bot + 92], radius=8, outline=c["line"], width=2)
        for i in range(3):                                                 # 工具栏图标
            x = self.right - 200 + i * 40
            d.rectangle([x, self.H - 46, x + 22, self.H - 24], fill=c["icon"])
        d.rectangle([self.right, 0, self.W - 1, self.H - 1], fill=DESKTOP)  # 隐形边框外
        d.rectangle([0, self.H - 7, self.W - 1, self.H - 1], fill=DESKTOP)

    # ---- 画一条消息，返回它的 y 下沿 ----
    def _avatar(self, x, y):
        # 头像是**照片**：多色、没有主色 —— spec §2 的头像判据正是"主色占比 < 0.5"。
        # 画成"纯色方块 + 一张脸"会被判据当成气泡（实测踩过：多出 4 条 36px 宽的"消息"）。
        self.photo(x, y, AVATAR, AVATAR)

    def left_bubble(self, cur, text, nick=None, lines=None, force_w=None, h=None,
                    record=True):
        """对方气泡。nick 非空则在其上方画昵称行（微信只在连发第一条标昵称）。"""
        c = self.c
        text_lines = lines or [text]
        f = _font(TEXT_SIZE)
        bw = force_w or max(int(f.getlength(t)) for t in text_lines) + BUBBLE_PAD * 2
        bh = h or (TEXT_PAD_Y * 2 + 20 + LINE_PITCH * (len(text_lines) - 1))
        y = cur + (28 if nick else 0)
        if nick:
            self.d.text((self.pane_left + 72, y - NICK_ABOVE), nick,
                        font=_font(NICK_H), fill=c["name"])
        self._avatar(self.pane_left + AVATAR_LEFT_DX, y)
        bx = self.pane_left + AVATAR_LEFT_DX + AVATAR + BUBBLE_GAP_X
        self.d.rounded_rectangle([bx, y, bx + bw - 1, y + bh - 1], radius=8,
                                 fill=c["panel_ai"])
        for i, t in enumerate(text_lines):
            self.d.text((bx + BUBBLE_PAD, y + TEXT_PAD_Y + i * LINE_PITCH), t,
                        font=f, fill=c["text"])
        if record:
            # 真值里的说话人：有昵称行就是昵称，没有昵称行就是"继承上一条"
            # （微信只在连发第一条上标昵称 —— 这正是要验证的那条规则）
            who = nick or self.last_in
            if nick:
                self.last_in = nick
            self.messages.append({"side": "in", "sender": who,
                                  "text": " ".join(text_lines),
                                  "lines": text_lines, "y": y, "w": bw, "h": bh})
        return y + bh + BUBBLE_GAP_Y

    def right_bubble(self, cur, text, image=False):
        """我方气泡（右对齐，无昵称行）。"""
        c = self.c
        f = _font(TEXT_SIZE)
        if image:
            bw, bh = IMAGE_W, IMAGE_H
        else:
            bw = int(f.getlength(text)) + BUBBLE_PAD * 2
            bh = TEXT_PAD_Y * 2 + 20
        bx = self.right - AVATAR_RIGHT_DX - BUBBLE_GAP_X - bw
        y = cur
        self._avatar(self.right - AVATAR_RIGHT_DX, y)
        if image:
            self.photo(bx, y, bw, bh)
        else:
            self.d.rounded_rectangle([bx, y, bx + bw - 1, y + bh - 1], radius=8,
                                     fill=c["panel_me"])
            self.d.text((bx + BUBBLE_PAD, y + TEXT_PAD_Y), text, font=f, fill=c["text"])
        self.messages.append({"side": "out", "sender": "我",
                              "text": wc.IMG_TEXT if image else text,
                              "lines": [text], "y": y, "w": bw, "h": bh, "media": image})
        return y + bh + BUBBLE_GAP_Y

    def left_image(self, cur, nick):
        """图片消息：昵称**与头像同一行**（不在头像上方，spec §2），缩略图在昵称行下方。

        为什么缩略图要错开一行：昵称条就是 [头像 x+44, 头像 y+2 .. +28] 那一条，
        缩略图若和头像同高，它的像素会落进昵称条里，OCR 就把图块和昵称一起读。
        """
        c = self.c
        y = cur
        self._avatar(self.pane_left + AVATAR_LEFT_DX, y)
        self.d.text((self.pane_left + AVATAR_LEFT_DX + AVATAR + 8, y + 12), nick,
                    font=_font(NICK_H), fill=c["name"])
        bx = self.pane_left + AVATAR_LEFT_DX + AVATAR + BUBBLE_GAP_X
        ty = y + 30                                    # 缩略图顶 = 昵称行下方
        self.photo(bx, ty, IMAGE_W, IMAGE_H)
        # 图片消息的昵称**与头像同一行**，不继承上一条（读不到就是 ?）
        self.last_in = nick
        self.messages.append({"side": "in", "sender": nick, "text": wc.IMG_TEXT,
                              "lines": [nick], "y": ty, "w": IMAGE_W, "h": IMAGE_H,
                              "media": True})
        return ty + IMAGE_H + BUBBLE_GAP_Y

    def photo(self, x, y, w, h):
        """一块"照片"：多色、无主色（主色占比 < 0.55，否则会被当成气泡）。"""
        rng = np.random.default_rng((x * 7919 + y * 104729) % (2 ** 31))
        block = rng.integers(0, 256, size=(h, w, 3), dtype=np.uint8)
        self.im.paste(Image.fromarray(block), (x, y))

    def plain(self, cur, text, size=12, centered=True, dy=6):
        """无气泡的灰字：时间戳行 / 日期行 / 引用行。"""
        f = _font(size)
        tw = int(f.getlength(text))
        x = (self.pane_left + self.right - tw) // 2 if centered else self.pane_left + 72
        self.d.text((x, cur + dy), text, font=f, fill=self.c["sys"])
        return cur + dy + size + 8

    def clipped_bottom(self, text, nick=None):
        """底部被消息区边界裁断的气泡（真值：必须被丢弃，不能上报）。"""
        y = self.msg_bot - 24                                  # 跨过 msg_bot
        self.left_bubble(y - 28, text, nick=nick, record=False)
        return y

    def clipped_top(self):
        """顶部被消息区边界裁断的气泡：先画，后面头部/分隔线会盖住它的上半部分。

        可见部分落在 y ∈ [msg_top, msg_top+14]：比一个昵称行还矮，
        所以下面那条消息的昵称行不会和它抢像素（真机上微信也是这么排的）。
        """
        y = self.msg_top - 22
        bx = self.pane_left + AVATAR_LEFT_DX + AVATAR + BUBBLE_GAP_X
        self.d.rounded_rectangle([bx, y, bx + 150, y + 36], radius=8,
                                 fill=self.c["panel_ai"])
        self.d.text((bx + BUBBLE_PAD, y + 8), "上面还有内容", font=_font(TEXT_SIZE),
                    fill=self.c["text"])

    def finish(self):
        """重画头部与分隔线：真实窗口里消息是被头部**盖住**的，所以最后画。"""
        self.d.rectangle([self.pane_left, 0, self.right - 1, self.msg_top - 1],
                         fill=self.c["pane"])
        self.d.text((self.pane_left + 16, self.msg_top - 34), self.title,
                    font=_font(16), fill=self.c["text"])
        self.d.line([self.pane_left, self.msg_top, self.right, self.msg_top],
                    fill=self.c["line"])
        return np.array(self.im)


# --------------------------------------------------------------------------
# 三张帧
# --------------------------------------------------------------------------
def frame_a():
    """896x648 浅色 pane_left=285：单字气泡 / 时间戳 / 引用行 / 底部裁断。"""
    fr = Frame(896, 648, "light", 285)
    cur = fr.msg_top + 8
    cur = fr.left_bubble(cur, "啥时候满5年？", nick="苏工")          # 1 有昵称
    cur = fr.left_bubble(cur, "我这边也是")                            # 2 连发 → 继承
    cur = fr.plain(cur, "9月22日 星期二 20:29")                 # 时间戳/日期行（不是气泡）
    cur = fr.plain(cur, "以上是打招呼的消息")                     # 无气泡系统提示（不是气泡）
    cur = fr.right_bubble(cur, "选快的模型会好点")                      # 3 我方
    cur = fr.left_bubble(cur, "嗯", nick="周工", force_w=43)            # 4 单字气泡（读不出文字）
    cur = fr.plain(cur, "小满: qwen3.8怎么样", centered=False)  # 引用行（不是气泡）
    cur = fr.right_bubble(cur, "收到")                                  # 5 我方
    fr.clipped_bottom("这条要整条丢掉", nick="小满")            # 底部裁断 → 丢弃
    return fr.finish(), fr.messages, dict(pane_left=285, msg_top=80, msg_bot=648 - 152, is_group=True, clip=True)


def frame_b():
    """780x981 浅色 pane_left=308：三行气泡 / 图片消息 / 顶部裁断。"""
    fr = Frame(780, 981, "light", 308)
    fr.clipped_top()                                            # 顶部裁断 → 丢弃
    # 顶部残块的可见部分占 [msg_top, msg_top+14]，下面这条要给它留出昵称行的位置
    cur = fr.msg_top + 46
    cur = fr.left_bubble(cur, "不过简单的活用国产的flash模型也没问题", nick="文墨")
    cur = fr.left_bubble(cur, "", lines=["AGI还没到就得进入杀价阶段了",
                                         "现在就开始杀价了",
                                         "第三行收尾"])              # 三行 = 一条消息
    cur = fr.left_image(cur, "苏工")                           # 图片消息（昵称在头像旁）
    cur = fr.right_bubble(cur, "今天又来了小米flash")
    cur = fr.right_bubble(cur, "我的双机dgx已经有四个模型可选择")
    return fr.finish(), fr.messages, dict(pane_left=308, msg_top=80, msg_bot=981 - 152, is_group=True, clip=True)


def frame_c():
    """1243x981 深色 pane_left=308：图片消息 + 连发继承 + 底部裁断。"""
    fr = Frame(1243, 981, "dark", 308)
    cur = fr.msg_top + 8
    cur = fr.left_bubble(cur, "先看这个", nick="周工")
    cur = fr.left_bubble(cur, "第二个问题也在这里了")                    # 连发 → 继承 周工
    cur = fr.right_bubble(cur, "收到，我看看")
    cur = fr.left_image(cur, "周工")
    fr.clipped_bottom("下面这条也不全", nick="周工")                # 底部裁断 → 丢弃
    return fr.finish(), fr.messages, dict(pane_left=308, msg_top=80, msg_bot=981 - 152, is_group=True, clip=True)


def frame_d():
    """657x981 浅色 **单聊**（标题没有 (N)）：昵称读取必须自动关掉（spec §5）。

    单聊的坑：正文上方那条带是空的，而系统提示（"以上是打招呼的消息"）正好落在带里 ——
    "≤12 字 + 无冒号"的昵称校验挡不住它，会被误当说话人。所以单聊帧里刻意把这句话
    放在一条气泡的正上方（带内），验证它**没有**变成任何人的名字。
    """
    fr = Frame(657, 981, "light", 285, title="幂喵")             # 单聊标题：没有 (39)
    cur = fr.msg_top + 8
    cur = fr.plain(cur, "以上是打招呼的消息")                     # 无气泡系统提示，落在昵称带里
    cur = fr.left_bubble(cur, "在吗", nick=None)                  # 单聊没有昵称行
    cur = fr.left_bubble(cur, "看一下这个")
    cur = fr.right_bubble(cur, "在的")
    cur = fr.left_bubble(cur, "好")
    return fr.finish(), fr.messages, dict(pane_left=285, msg_top=80, msg_bot=981 - 152, is_group=False, clip=False)


FRAMES = [("A 896x648 浅色 pane_left=285", frame_a),
          ("B 780x981 浅色 pane_left=308", frame_b),
          ("C 1243x981 深色 pane_left=308", frame_c),
          ("D 657x981 浅色 单聊 pane_left=285", frame_d)]


# --------------------------------------------------------------------------
# 断言
# --------------------------------------------------------------------------
def _norm(t: str) -> str:
    """OCR 会加/丢空格（实测同一块文字两种 det 配置的空格位置不同），比对时一律抹掉。"""
    return "".join((t or "").split())


class Check:
    def __init__(self):
        self.fail = 0

    def eq(self, tag, got, want):
        ok = got == want
        if not ok:
            self.fail += 1
        print(f"  [{'ok ' if ok else 'FAIL'}] {tag}: got={got!r} want={want!r}")
        return ok

    def true(self, tag, cond, detail=""):
        if not cond:
            self.fail += 1
        print(f"  [{'ok ' if cond else 'FAIL'}] {tag}{('  ' + detail) if detail else ''}")
        return bool(cond)


def run_case(name, build):
    print(f"\n=== {name} ===")
    img, truth, layout_true = build()
    view = wc.parse(img)                     # 不带 TitleMemory：标题真值每次都一样
    c = Check()

    # 1) 版面：pane_left / msg_top / msg_bot 与合成时的真值一致
    c.eq("pane_left", view.layout.pane_left if view.layout else None,
         layout_true["pane_left"])
    c.eq("msg_top", view.layout.msg_top if view.layout else None, layout_true["msg_top"])
    c.eq("msg_bot", view.layout.msg_bot if view.layout else None, layout_true["msg_bot"])
    if view.layout is None:
        return c.fail
    c.eq("聊天区右界(帧右−7)", view.layout.right, img.shape[1] - 7)
    c.eq("view.error", view.error, None)
    c.eq("is_group（群聊/单聊场景判定）", view.is_group, layout_true["is_group"])

    # 2) 气泡数 = 真值条数（被裁断的块不算）
    got = view.lines
    c.eq("消息条数", len(got), len(truth))

    # 3) 逐条：说话人 + 文本命中真值
    for i, t in enumerate(truth):
        if i >= len(got):
            c.true(f"第{i + 1}条", False, "缺失")
            continue
        ln = got[i]
        c.eq(f"第{i + 1}条 说话人", ln.sender, t["sender"])
        # 单字气泡实测读不出（8 种参数全空）→ 它在真值里就按"引擎读不出"处理
        is_single_char = len(_norm(t["text"])) == 1
        if is_single_char:
            # 单字气泡：spec §3 实测 8 种参数组合**全部读不出**（引擎能力边界，连框都
            # 检不出）。所以这里两种结果都算对：读出来了就必须读到"嗯"，读不出就必须
            # 显式留成 empty（下面单独断言）—— 但无论哪种，"气泡存在"这条记录都不能丢。
            c.true(f"第{i + 1}条 文本（单字气泡：读出则必须准确）",
                   _norm(ln.text) == _norm(t["text"]) or ln.empty,
                   f"got={ln.text!r} want={t['text']!r} empty={ln.empty}")
        else:
            c.true(f"第{i + 1}条 文本", _norm(ln.text) == _norm(t["text"]),
                   f"got={ln.text!r} want={t['text']!r}")
        c.eq(f"第{i + 1}条 方向", ln.direction, t["side"])
        # 气泡框与合成坐标一致（容差 2px：圆角反锯齿）
        c.true(f"第{i + 1}条 气泡框",
               abs(ln.y0 - t["y"]) <= 2 and abs((ln.x1 - ln.x0) - t["w"]) <= 2,
               f"y0={ln.y0}/{t['y']} w={ln.x1 - ln.x0}/{t['w']}")
        if t.get("media"):
            c.true(f"第{i + 1}条 是图片/表情", ln.media and ln.text == wc.IMG_TEXT)
        if is_single_char:
            # 单字气泡必须留成"有气泡、无文字"（8 种参数实测全空 → 引擎边界）：
            # 静默丢掉的话，漏读在面板上就是不可见的。
            c.true("单字气泡保留为「有气泡、无文字」",
                   ln.empty and ln.text == "" and ln in view.messages,
                   f"empty={ln.empty} text={ln.text!r} 在 messages 里="
                   f"{any(x is ln for x in view.messages)}")

    # 4) 时间戳/日期/引用行/无气泡系统提示没有变成消息
    merged = " ".join(_norm(x.text) for x in got)
    for bad in ("9月22日", "星期二", "qwen3.8", "以上是打招呼的消息"):
        c.true(f"无气泡灰字未成消息: {bad}", bad not in merged)
    # 5) 被裁断的块被丢弃（view.dropped 有记录，且没有变成消息）
    if layout_true["clip"]:
        c.true("被边界裁断的块被丢弃", len(view.dropped) >= 1,
               f"dropped={[d['why'] for d in view.dropped]}")
    else:
        # 没有裁断块的帧：消息区掩码外的东西（输入框描边、工具栏图标、头部）不该产生 dropped
        c.eq("无裁断块时 dropped 为空", view.dropped, [])
    c.true("裁断残块的文字没进消息", "这条要整条丢掉" not in merged
           and "下面这条也不全" not in merged and "上面还有内容" not in merged)
    # 6) 深色主题下正文照样读得出来（spec §6 未验证项，这里给出证据）
    return c.fail


def run_detect_fail_cases() -> int:
    """版面检测不出来时必须**报错返回**，绝不能回退到按比例硬裁。

    旧实现的三条比例常量（CHAT_LEFT_R=0.348 / MSG_TOP_R=0.155 / MSG_BOTTOM_R=0.735）
    在检测失败时会照样裁出一块来 OCR —— 宽 1243 时左边界算成 432、真实 308。
    所以这里专门造两张"量不出版面"的帧，断言：error 有原因、没有 lines（不硬裁）。
    """
    print("\n=== 版面检测失败路径（不许按比例硬裁）===")
    c = Check()

    # ① 窗口太小
    small = np.full((200, 300, 3), 245, np.uint8)
    v = wc.parse(small)
    c.true("窗口太小 → view.error 写明原因", bool(v.error), f"error={v.error!r}")
    c.eq("窗口太小 → 没有 layout", v.layout, None)
    c.eq("窗口太小 → 一条消息都不产出（不硬裁）", v.lines, [])

    # ② 布局没铺好：聊天面板在（pane_left 能测到），但消息区上下边界那条整行分隔线没有
    img = np.full((700, 900, 3), 237, np.uint8)                # 会话列表底色
    img[:, 300:] = (245, 245, 245)                             # 聊天面板
    img[300:340, 380:700] = (255, 255, 255)                    # 一条白色气泡（不是整行）
    v = wc.parse(img)
    c.true("找不到消息区边界 → view.error 写明原因", bool(v.error), f"error={v.error!r}")
    c.true("原因里点名是消息区边界", "边界" in (v.error or ""), f"error={v.error!r}")
    c.eq("找不到边界 → 一条消息都不产出（不硬裁）", v.lines, [])
    return c.fail


def main() -> int:
    print("微信版面/逐气泡提取验证（合成帧）")
    fails = 0
    for name, build in FRAMES:
        fails += run_case(name, build)

    fails += run_detect_fail_cases()

    # 单聊分支（spec §5）：同一张群聊帧，关掉昵称读取 → 左侧一律 ?，绝不能读到系统提示那类句子
    print("\n=== 单聊分支：read_names=False（spec §5）===")
    img, truth, _ = frame_a()
    c = Check()
    view = wc.parse(img, read_names=False)
    left = [x for x in view.lines if x.direction == "in"]
    c.true("左侧消息不读昵称，一律记 ?", left and all(x.sender == "?" for x in left),
           f"senders={[x.sender for x in left]}")
    c.true("右侧仍记「我」",
           all(x.sender == "我" for x in view.lines if x.direction == "out"))
    c.eq("关昵称不影响条数", len(view.lines), len(truth))
    c.true("文本仍然读出", all(_norm(x.text) for x in view.lines
                               if not x.empty))
    fails += c.fail

    print()
    if fails:
        print(f"FAIL —— {fails} 项不符")
        return 1
    print("PASS —— 版面/气泡/归属/裁断/单聊分支全部符合真值")
    return 0


if __name__ == "__main__":
    sys.exit(main())
