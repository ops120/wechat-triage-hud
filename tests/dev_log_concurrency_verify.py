"""验证审计日志的两项加固是否真的生效（而不是只在文档里写着）。

  1) **跨进程锁**：4 个进程各写 50 条，全部写完后每一行都必须是完整合法 JSON，
     总行数正确，seq 无重复 —— 无锁时多进程写会把行交织在一起，JSON 会解析失败。
  2) **fsync 节流**：fsync_every=10 时，写 50 条应只触发 5 次 os.fsync。
     用进程内计数器验证（monkeypatch os.fsync），不靠"看代码觉得对"。
"""
import json
import os
import subprocess
import sys
import textwrap

PROJECT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PROJECT_DIR)

from wechat_triage_hud.paths import OUT_DIR  # noqa: E402

LOG = os.path.join(OUT_DIR, "_concurrency_test.jsonl")
N_PROC = 4
N_REC = 50
fails = []


def check(name, ok, detail=""):
    print(f"  {'OK  ' if ok else 'FAIL'} {name}" + (f"  [{detail}]" if detail else ""))
    if not ok:
        fails.append(name)


WORKER = textwrap.dedent('''
    import os, sys
    sys.path.insert(0, r"{proj}")
    from wechat_triage_hud.jev_log import JevAudit
    tag = sys.argv[1]
    a = JevAudit(r"{log}", fsync_every=10)
    for i in range({n}):
        a.record(proc=tag, i=i, payload="x" * 400)
    a.flush_now()
''')


def main():
    os.makedirs(OUT_DIR, exist_ok=True)
    if os.path.exists(LOG):
        os.remove(LOG)

    # ---------- 1) 跨进程锁 ----------
    print(f"[1] 跨进程锁：{N_PROC} 个进程各写 {N_REC} 条")
    script = WORKER.format(proj=PROJECT_DIR, log=LOG, n=N_REC)
    procs = [subprocess.Popen([sys.executable, "-c", script, f"p{i}"],
                              stdout=subprocess.PIPE, stderr=subprocess.PIPE)
             for i in range(N_PROC)]
    errs = []
    for i, p in enumerate(procs):
        out, err = p.communicate(timeout=180)
        if p.returncode != 0:
            errs.append(f"p{i} rc={p.returncode} {err.decode('utf-8','replace')[:200]}")
    check("所有写进程正常退出", not errs, "; ".join(errs)[:160])

    lines = [ln for ln in open(LOG, encoding="utf-8")]
    check("总行数正确", len(lines) == N_PROC * N_REC,
          f"期望 {N_PROC*N_REC}，实际 {len(lines)}")

    bad = []
    seqs = []
    for idx, ln in enumerate(lines, 1):
        try:
            r = json.loads(ln)
            seqs.append(r.get("seq"))
        except Exception as e:
            bad.append((idx, str(e)[:50], ln[:70]))
    check("每一行都是完整合法 JSON（无交织）", not bad,
          f"{len(bad)} 行损坏，例：{bad[0] if bad else ''}")
    check("seq 无重复", len(set(seqs)) == len(seqs),
          f"{len(seqs)} 条中 {len(set(seqs))} 个不同")

    # ---------- 2) fsync 节流 ----------
    print(f"\n[2] fsync 节流：fsync_every=10，写 50 条应只 fsync 5 次")
    import wechat_triage_hud.jev_log as jl
    real_fsync = os.fsync
    calls = {"n": 0}

    def counting_fsync(fd):
        calls["n"] += 1
        return real_fsync(fd)

    os.fsync = counting_fsync
    jl.os.fsync = counting_fsync
    try:
        solo = os.path.join(OUT_DIR, "_fsync_test.jsonl")
        if os.path.exists(solo):
            os.remove(solo)
        a = jl.JevAudit(solo, fsync_every=10)
        for i in range(50):
            a.record(i=i, pad="y" * 200)
        got = calls["n"]
        check("fsync 次数被节流（≈5 次，而非 50 次）", 4 <= got <= 6,
              f"实际 {got} 次")
        a.flush_now()
        check("flush_now 额外触发一次落盘", calls["n"] == got + 1,
              f"{calls['n']} vs {got}+1")
        os.remove(solo)
    finally:
        os.fsync = real_fsync
        jl.os.fsync = real_fsync

    # 收尾
    os.remove(LOG)
    print("\n" + "=" * 60)
    if fails:
        print("失败项：")
        for f in fails:
            print("  -", f)
        return 1
    print("审计日志加固验证通过")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
