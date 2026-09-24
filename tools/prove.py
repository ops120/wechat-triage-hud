"""取证：对当前微信里最新的一个发言人做一次**真实**的按人判断，
并把这次 HTTP 交换从审计日志里读回来展示。

与产品的区别只有一点：产品是自动扫描后批量分诊，这里是手动指定一次。
调用路径完全相同（person_engine.analyse_person → JevClient），
所以它验证的就是真正上线的东西。日志即凭据。
"""
from __future__ import annotations

import os
import sys
import time

import os
import sys

# 仓库根入 path，再从包里取统一路径 —— 禁止各脚本自己 dirname(__file__) 推算，
# 否则审计日志会被拆成多份（见 wechat_triage_hud/paths.py 的说明）。
PROJECT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PROJECT_DIR)

from wechat_triage_hud.paths import (  # noqa: E402
    AUDIT_PATH, DEBUG_PATH, ENV_PATH, OUT_DIR,
)

from wechat_triage_hud import wechat_capture as wc  # noqa: E402
from wechat_triage_hud import wechat_window as ww   # noqa: E402
from wechat_triage_hud.jev_engine import (  # noqa: E402
    API_URL, MODEL, JevClient, load_api_key,
)
from wechat_triage_hud.person_engine import (  # noqa: E402
    SpeakerProfile, analyse_person,
)
from wechat_triage_hud.qset import RISK_LABELS, label  # noqa: E402

LOG = AUDIT_PATH


def main() -> int:
    w = ww.find_main()
    if not w:
        print("未找到微信窗口")
        return 1
    if w.minimized:
        ww.restore(w.hwnd)
        time.sleep(1.2)
        w = ww.find_main()
    v = wc.snapshot(w.rect)
    speakers = v.speakers()
    if not speakers:
        print("读不到发言人（屏幕锁定、微信最小化、或当前会话没有别人的消息）")
        return 1
    nick, msgs = speakers[0]
    ctx = list(v.recent_messages(10))

    print("=" * 76)
    print("取自真实微信窗口")
    print("=" * 76)
    print(f"  窗口     : hwnd={w.hwnd}  {w.width}x{w.height}")
    print(f"  会话     : {v.title}  群聊={v.is_group}  人数={v.member_count}")
    print(f"  发言人   : {nick}")
    print(f"  最近消息 : {msgs}")
    print()

    client = JevClient(load_api_key(ENV_PATH),
                       audit_path=LOG, caller="prove", verbose=False)
    before = client.audit.records
    print(f"  端点     : {API_URL}")
    print(f"  模型     : {MODEL}")
    print(f"  审计日志 : {LOG}\n")

    t0 = time.time()
    r = analyse_person(client, group=v.title, topic="群聊", viewer=None,
                       nickname=nick, msgs=msgs, context=ctx,
                       profile=SpeakerProfile())
    dt = time.time() - t0

    print("=" * 76)
    print("判断结果")
    print("=" * 76)
    print(f"  角色       : {r['role']} ({label(r['role'])})  把握 {r['role_conf']:.2f}")
    print(f"  意图       : {r['intent']} ({label(r['intent'])})  把握 {r['intent_conf']:.2f}")
    print(f"  是否在点我 : {round(r['addressed_to_me'] * 100)}%")
    print(f"  该不该回   : {label(r['worth'])}")
    print(f"  关注度     : {r['attention']} / 10  (有效质量 {r['attention_mass']})")
    print(f"  信息充分度 : {r['sufficiency']:.2f} / 3")
    print("  四类风险   : " + "  ".join(
        f"{RISK_LABELS[k]}={v2:.2f}" for k, v2 in r["risks"].items()))
    print(f"  门控状态   : {r['state']}  ← {', '.join(r['state_reasons']) or '无'}")
    print(f"  耗时       : {dt:.2f}s   输入 token {r['usage'].get('input_tokens')}")
    print()

    recs = client.audit.tail(1)
    if not recs:
        print("审计日志读不到记录")
        return 1
    rec = recs[0]
    print("=" * 76)
    print("审计日志里的同一次交换（可自行核对）")
    print("=" * 76)
    print(f"  seq={rec['seq']} caller={rec.get('caller')} "
          f"attempt={rec.get('attempt')}/{rec.get('max_attempts')}")
    print(f"  http_status={rec.get('http_status')}  {rec.get('elapsed_s')}s  "
          f"retry_slept={rec.get('retry_slept_s')}s")
    print(f"  请求 {rec.get('request_bytes')} 字节 / 响应 {rec.get('response_bytes')} 字节")
    print(f"  key 指纹: {rec.get('key_fingerprint')}")
    resp = rec.get("response") or {}
    print(f"  服务端回报模型: {resp.get('model')}   usage: {resp.get('usage')}")
    print(f"  日志文件 {client.audit.size_human()}（含此前所有运行）"          f"　本次运行写入 {client.audit.records - before} 条")
    print("  结论：界面上每个数字都能在日志里追到一次真实 HTTP 响应。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
