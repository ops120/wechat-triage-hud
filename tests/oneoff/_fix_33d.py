# -*- coding: utf-8 -*-
# 一次性修补脚本：③d build_questions_dm 继承题路径替换（用后即删）
import io

p = 'wechat_triage_hud/qset.py'
s = io.open(p, encoding='utf-8').read()
old = ('            v["instructions"] = (ins.replace("我.群昵称", "我.身份")\n'
       '                                    .replace("我.职责", "对话.关系")\n'
       '                                    .replace("群成员发的文字", "对方发的文字"))')
new = ('            # 私聊 state 没有 `这个人.最近消息`/`最近全群消息`/`我.群昵称`/`我.职责`，\n'
       '            # 继承来的题面必须指到真实存在的字段（我.身份 / 对话.关系 /\n'
       '            # 对方.最近消息 / 最近的整段对话），否则模型找不到字段只能硬猜。\n'
       '            v["instructions"] = (\n'
       '                ins.replace(\n'
       '                    "仅评估 `这个人.最近消息`，结合 `我` 的群昵称与职责、"\n'
       '                    "以及 `最近全群消息` 的上下文。",\n'
       '                    "仅评估 `对方.最近消息`，结合 `我.身份` 与 `对话.关系`、"\n'
       '                    "以及 `最近的整段对话` 的上下文。")\n'
       '                    .replace("我.群昵称", "我.身份")\n'
       '                    .replace("我.职责", "对话.关系")\n'
       '                    .replace("群成员发的文字", "对方发的文字"))')
assert s.count(old) == 1, f"count={s.count(old)}"
io.open(p, 'w', encoding='utf-8', newline='').write(s.replace(old, new))
print('REPLACED')
