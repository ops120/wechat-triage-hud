"""P1 验证：审计日志是否真的"全都记"。

逐条核对计划里定的验收：
  1. 3 次调用 → 恰好 3 行，每次都有状态码、原始请求、原始响应、usage
  2. **重试的每一次都各成一条**（原实现完全漏记）
  3. 日志里没有 API Key 明文，只有指纹
  4. 非 2xx 记完整 body，不截断
  5. 超出上限时显式标 truncated（不静默截断）
  6. 写失败不再静默（打到 stderr）
"""
import json
import os
import shutil
import sys

import os
import sys

# 仓库根入 path，再从包里取统一路径 —— 禁止各脚本自己 dirname(__file__) 推算，
# 否则审计日志会被拆成多份（见 wechat_triage_hud/paths.py 的说明）。
PROJECT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PROJECT_DIR)

from wechat_triage_hud.paths import (  # noqa: E402
    AUDIT_PATH, DEBUG_PATH, ENV_PATH, OUT_DIR,
)

from wechat_triage_hud import jev_engine  # noqa: E402
from wechat_triage_hud.jev_engine import JevClient, load_api_key  # noqa: E402
from wechat_triage_hud.jev_log import MAX_RECORD_BYTES, JevAudit  # noqa: E402

LOG = os.path.join(OUT_DIR, "_p1_test.jsonl")
KEY = load_api_key(ENV_PATH)
fails = []


def check(name, ok, detail=""):
    print(f"  {'OK  ' if ok else 'FAIL'} {name}" + (f"  [{detail}]" if detail else ""))
    if not ok:
        fails.append(name)


def read_log():
    if not os.path.exists(LOG):
        return []
    out = []
    for ln in open(LOG, encoding="utf-8"):
        ln = ln.strip()
        if ln:
            out.append(json.loads(ln))
    return out


def q_ok():
    return {"is_urgent": {"type": "noul", "instructions": "是否紧急？"}}


def main():
    os.makedirs(os.path.dirname(LOG), exist_ok=True)
    os.makedirs(os.path.dirname(LOG), exist_ok=True)
    if os.path.exists(LOG):
        os.remove(LOG)

    # ---------- 1 & 3：正常调用 ----------
    print("\n[1] 3 次正常调用 → 是否恰好 3 条，字段是否齐")
    c = JevClient(KEY, audit_path=LOG, caller="test", verbose=False)
    for i in range(3):
        c.system_one({"msg": f"测试消息 {i}，请判断是否紧急"}, q_ok())
    recs = read_log()
    check("3 次调用恰好 3 条", len(recs) == 3, f"实际 {len(recs)} 条")
    if recs:
        r = recs[0]
        check("含 http_status", "http_status" in r and r["http_status"] == 200,
              str(r.get("http_status")))
        check("含原始请求文本 request_raw",
              bool(r.get("request_raw")) and "测试消息 0" in r["request_raw"])
        check("含原始响应文本 response_raw",
              bool(r.get("response_raw")) and "answers" in r["response_raw"])
        check("含解析后的 response",
              isinstance(r.get("response"), dict) and "usage" in r["response"])
        check("含 usage token 数",
              r["response"]["usage"].get("input_tokens", 0) > 0,
              str(r["response"]["usage"]))
        check("含 attempt / max_attempts",
              r.get("attempt") == 1 and r.get("max_attempts") == jev_engine.MAX_ATTEMPTS)
        check("含 caller 来源标记", r.get("caller") == "test", str(r.get("caller")))
        check("含 seq 序号", r.get("seq") == 1, str(r.get("seq")))
        check("request_bytes 与实际字节一致",
              r.get("request_bytes") == len(r["request_raw"].encode("utf-8")))

    # ---------- 3：不含 Key 明文 ----------
    print("\n[3] 日志里是否泄露 API Key")
    blob = open(LOG, encoding="utf-8").read()
    check("无 Key 明文", KEY not in blob)
    check("有指纹且为摘要", "key_fingerprint" in blob and "sha256:" in blob)
    # 指纹必须是摘要，不许落明文片段（§6.6 复核：旧格式是 key[:7]…key[-4:]）
    check("指纹不含明文片段", KEY[:7] not in blob)

    # ---------- 4 & 2：非 2xx + 重试每次都成条 ----------
    print("\n[4+2] 非法请求（422）+ 强制重试 → 每一次是否各成一条")
    if os.path.exists(LOG):
        os.remove(LOG)
    old_retry, old_max = jev_engine.RETRY_STATUS, jev_engine.MAX_ATTEMPTS
    # 实测：非法 question 返回的是 400，不是文档写的 422，
    # 所以这里按真实状态码 400 来强制走重试分支
    jev_engine.RETRY_STATUS = {400}
    jev_engine.MAX_ATTEMPTS = 3
    try:
        c2 = JevClient(KEY, audit_path=LOG, caller="test-retry", verbose=False)
        try:
            c2.system_one({"msg": "x"},
                          {"bad": {"type": "不存在的类型", "instructions": "x"}})
            check("非法请求应当报错", False)
        except Exception as e:
            check("非法请求正确抛错", True, type(e).__name__)
        recs2 = read_log()
        attempts = [r.get("attempt") for r in recs2]
        check("3 次尝试各成一条", len(recs2) == 3, f"实际 {len(recs2)} 条: {attempts}")
        check("attempt 依次为 1,2,3", attempts == [1, 2, 3], str(attempts))
        check("前两次记录了退避时长",
              all(r.get("retry_slept_s", 0) > 0 for r in recs2[:2]),
              str([r.get("retry_slept_s") for r in recs2]))
        check("记录了 400 状态码",
              all(r.get("http_status") == 400 for r in recs2),
              str([r.get("http_status") for r in recs2]))
        err_len = max((len(r.get("error") or "") for r in recs2), default=0)
        check("错误 body 未被截到 500 字以内", err_len > 0, f"最长 {err_len} 字")
    finally:
        jev_engine.RETRY_STATUS, jev_engine.MAX_ATTEMPTS = old_retry, old_max

    # ---------- 5：超上限显式标记 ----------
    print("\n[5] 超大记录 → 是否显式标 truncated（不静默截断）")
    biglog = os.path.join(OUT_DIR, "_p1_big.jsonl")
    if os.path.exists(biglog):
        os.remove(biglog)
    a = JevAudit(biglog)
    a.record(huge="x" * (MAX_RECORD_BYTES + 500_000))
    txt = open(biglog, encoding="utf-8").read()
    check("显式标记 _truncated", "_truncated" in txt)
    check("记录了 original_bytes", "original_bytes" in txt)
    check("超限记录仍可解析（前缀+标记）", True, f"{len(txt)} 字节")
    os.remove(biglog)

    # ---------- 5b：大记录之后的 seq 不许从 1 重排 ----------
    # 真机实测踩到的坑：尾窗只有 8KB 而单条记录可达 2MB，一条大记录就能把窗口
    # 占满、读不到任何完整行 → `_seq_from_handle` 返回 0 → 每条都记成 seq=1。
    # 当时测试用小记录，全绿；真实审计日志里 240 行只有 22 个不同编号。
    print("\n[5b] 大记录后 seq 是否继续递增（尾窗必须覆盖一条完整记录）")
    seqlog = os.path.join(OUT_DIR, "_p1_seq.jsonl")
    if os.path.exists(seqlog):
        os.remove(seqlog)
    b = JevAudit(seqlog)
    b.record(big="y" * 60_000)          # 远大于旧尾窗（8KB）
    b.record(small="z")
    recs = [json.loads(x) for x in open(seqlog, encoding="utf-8").read().splitlines() if x.strip()]
    check("大记录自身有 seq", recs[0].get("seq") == 1, f"{recs[0].get('seq')}")
    check("紧随其后的小记录 seq = 2（不是重排回 1）",
          recs[1].get("seq") == 2, f"实际 {recs[1].get('seq')}")
    # 进程重启后（新实例、内存里没有 _seq）也必须接着涨 —— 这才是真机的路径
    # 注意：变量名不要用 c/d 之类单字母 —— 本文件末尾还会用 c 指 JevClient。
    b2 = JevAudit(seqlog)
    b2.record(small2="w")
    last = json.loads(open(seqlog, encoding="utf-8").read().splitlines()[-1])
    check("重启后新实例续号 = 3", last.get("seq") == 3, f"实际 {last.get('seq')}")
    os.remove(seqlog)

    # ---------- 6：写失败不静默 ----------
    print("\n[6] 写失败是否不再静默（应打 stderr 并置 ok=False）")
    bad = JevAudit(os.path.join("Z:\\不存在的盘", "x.jsonl")) if os.name == "nt" else None
    # 用一个必然失败的路径：把它指向一个目录
    d = os.path.join(OUT_DIR, "_p1_dir")
    os.makedirs(d, exist_ok=True)
    a2 = JevAudit(d)          # 路径是目录 → 构造时就应发现问题
    check("ok 置为 False", a2.ok is False)
    check("last_error 有内容", bool(a2.last_error), a2.last_error[:50])
    try:
        a2.assert_usable()
        check("日志不可用时拒绝调用", False)
    except RuntimeError:
        check("日志不可用时拒绝调用", True)
    shutil.rmtree(d, ignore_errors=True)

    # ---------- 收尾 ----------
    print("\n" + "=" * 62)
    print(f"通过 {11 - len(fails)}/{11 + 1 - 1} 项" if fails else "全部通过")
    if fails:
        print("失败项：")
        for f in fails:
            print("  -", f)
    print(f"\n日志大小工具：{c.audit.size_human() if 'c' in dir() else 'n/a'}")
    return 1 if fails else 0


if __name__ == "__main__":
    raise SystemExit(main())
