"""Jev 调用全量审计日志 —— 凡发送给 Jev 的、凡 Jev 返回的，全部落盘。

为什么单独一个模块：这是"数字是不是真的"唯一可永久复核的凭据，也是后面
测量逃逸率、编造、重试行为、成本的唯一数据源。所以它必须
**强制、完整、不可静默失败**，不能像原来那样是可选参数 + `except: pass`。

原实现的六个缺陷（已核对）：
  1. 选配：`if not self.audit_path: return`，默认 None，run.py 根本没传
  2. 重试完全不留痕：429/529 时直接 continue，退避那几次一个字节都不记
  3. 记的是 Python dict，不是真实发出的字节 / 服务端返回的原文
  4. 错误响应被截到 500 字符才记录
  5. 无并发保护：单条 3–6KB，超过单次 write() 的原子保证，多线程会交织
  6. 写失败被 except: pass 静默吞掉 —— 日志坏了你不会知道

设计：
  · 单写入点 + 锁，一条记录一次 write() 写完整行
  · 同进程内加工件内锁；**跨进程再加文件锁**（多实例曾同时写过同一文件）
  · 每条 flush()；fsync 按条数节流（默认每 10 条一次，可用 fsync_every 调）
  · 每次 HTTP 尝试各记一条（含 attempt / 本次退避时长）
  · 原始请求文本与原始响应文本都留。**request_raw 是权威的完整请求**
    （含 state 与 questions）；不再另存一份结构化的 state/questions，否则日志体积翻倍
  · 非 2xx 记完整 body，不截断
  · 超上限时显式写 truncated + original_bytes，绝不静默截断
  · 只记 API Key 指纹，绝不记明文
  · 写失败打到 stderr 并置 ok=False，界面可显示"日志异常"
"""
from __future__ import annotations

import hashlib
import json
import os
import sys
import threading
import time

try:
    import msvcrt          # Windows 文件锁
    _HAS_MSVCRT = True
except ImportError:        # 非 Windows：退化为仅进程内锁
    _HAS_MSVCRT = False

# 单条记录上限。超过则截断并显式标记，绝不静默丢内容。
MAX_RECORD_BYTES = 2_000_000

# 读文件尾找上一条 seq 的窗口。必须 ≥ 单条上限（再加一点余量），否则一条大记录
# 就能把整个窗口占掉、读不到任何完整行 → seq 从 1 重新开始。
# 真机实测踩过：中位行长 13KB 而窗口只有 8KB，240 行里 103 行 seq 都是 1。
TAIL_WINDOW_BYTES = MAX_RECORD_BYTES + 64 * 1024


class JevAudit:
    """JSONL 审计写入器。进程内所有调用共用一个文件、一把锁。"""

    def __init__(self, path: str, fsync_every: int = 10):
        """fsync_every: 每写多少条才真正 fsync 一次。

        每条都 fsync 时，单次调用无所谓，但自动扫描后每条群消息一次调用会明显拖慢。
        节流后最多丢最后 fsync_every-1 条，这是显式取舍，不是静默行为。
        """
        if not path:
            raise ValueError("审计日志路径必填 —— 不允许存在不写日志的调用路径")
        self.fsync_every = max(1, int(fsync_every))
        self._since_fsync = 0
        self._lock_warned = False
        self.path = os.path.abspath(path)
        self._lock = threading.Lock()
        # seq 必须**全文件唯一**：多个客户端/多次启动都会往同一个文件写，
        # 若各自从 1 开始，"按编号追溯"就失效了（实测日志里出现多个 #1）。
        # 初始化时只读文件末尾 4KB 里的最后一条，O(1) 拿到续号。
        self._seq = self._last_seq()
        self.records = 0
        self.ok = True
        self.last_error = ""
        self.error_count = 0
        try:
            os.makedirs(os.path.dirname(self.path), exist_ok=True)
            # 立刻试写一次，把"建不出目录 / 没权限 / 路径是目录"这类问题
            # 在构造阶段就暴露出来，而不是等到真正要记的时候才发现。
            with open(self.path, "a", encoding="utf-8"):
                pass
        except Exception as e:
            self.ok = False
            self.error_count += 1
            self.last_error = f"{type(e).__name__}: {e}"
            print(f"[JEV_AUDIT_ERROR] 审计日志不可用（{self.path}）: "
                  f"{self.last_error}", file=sys.stderr, flush=True)

    def assert_usable(self) -> None:
        """在发出未记录的调用之前调用它。

        日志写不进去时不能继续调 API —— 那正是"全都要记"被违反的时刻。
        宁可拒绝调用并明确报错，也不要产生一次无据可查的判断。
        """
        if not self.ok:
            raise RuntimeError(
                f"审计日志不可用，拒绝发出无法记录的调用：{self.last_error} "
                f"(path={self.path})")

    def _last_seq(self) -> int:
        try:
            with open(self.path, "rb") as f:
                f.seek(0, 2)
                size = f.tell()
                if size == 0:
                    return 0
                window = min(size, TAIL_WINDOW_BYTES)
                f.seek(size - window)
                tail = f.read().decode("utf-8", "ignore")
            rows = [x for x in tail.splitlines() if x.strip()]
            for ln in reversed(rows):
                try:
                    return int(json.loads(ln).get("seq", 0))
                except Exception:
                    continue
            return 0
        except Exception:
            return 0

    @staticmethod
    def _seq_from_handle(f) -> int:
        """从**已打开且已持锁**的句柄里读出最后一条的 seq。

        必须在文件锁内调用 —— 它是一次读-改-写的读那半。
        读失败返回内存里记住的值（有进展优先于完美）。
        """
        try:
            f.seek(0, 2)
            size = f.tell()
            if size == 0:
                return 0
            # 尾窗必须能**覆盖至少一条完整记录**（单条上限 MAX_RECORD_BYTES）：
            # 真机实测曾出现中位行长 13KB > 8KB 的旧尾窗 → 每次都只读到半行、
            # 解析失败 → seq 恒为 1（240 行只有 22 个不同编号，103 行是 1）。
            # 测试用的小记录掩盖了这个 bug，只有真实数据才暴露。
            window = min(size, TAIL_WINDOW_BYTES)
            f.seek(size - window)
            tail = f.read().decode("utf-8", "ignore")
            for ln in reversed([x for x in tail.splitlines() if x.strip()]):
                try:
                    return int(json.loads(ln).get("seq", 0))
                except Exception:
                    continue
        except Exception:
            pass
        return 0

    # ---------------- 写入 ----------------
    def record(self, **fields) -> None:
        """写一条记录。

        编号（seq）必须在**文件锁内部**分配：它是一次读-改-写，
        只在进程内加锁的话，多进程会同时读到同一个尾部值再各自递增。
        实测过这个坑：4 进程各写 50 条 → 200 行只有 66 个不同编号。

        写失败**绝不静默**：打 stderr 并置 ok=False；调用方（JevClient）会在
        发下一个请求前用 assert_usable() 拦下，宁可不判断也不产生无据可查的记录。
        """
        with self._lock:
            rec = {
                "ts": time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime())
                      + f".{int(time.time() * 1000) % 1000:03d}",
                "thread": threading.current_thread().name,
                **fields,
            }
            try:
                with open(self.path, "a+b") as f:
                    locked = False
                    if _HAS_MSVCRT:
                        try:
                            f.seek(0)
                            msvcrt.locking(f.fileno(), msvcrt.LK_LOCK, 1)
                            locked = True
                        except OSError as le:
                            # 拿不到锁也继续写：宁可偶尔交织，也不要丢记录（显式 fail-open）
                            if not self._lock_warned:
                                self._lock_warned = True
                                print(f"[JEV_AUDIT_WARN] 跨进程锁不可用，"
                                      f"降级为无锁写入: {le}",
                                      file=sys.stderr, flush=True)
                    try:
                        # ---- 以下三件都在文件锁内，才是原子的 ----
                        self._seq = self._seq_from_handle(f) + 1
                        rec["seq"] = self._seq
                        line = json.dumps(rec, ensure_ascii=False)
                        raw = line.encode("utf-8")
                        if len(raw) > MAX_RECORD_BYTES:
                            # 显式标记，绝不静默截断
                            head = raw[:MAX_RECORD_BYTES].decode("utf-8", "ignore")
                            raw = (head + " ..." + json.dumps({
                                "_truncated": True,
                                "original_bytes": len(line.encode("utf-8")),
                                "kept_bytes": len(head.encode("utf-8")),
                            }, ensure_ascii=False)).encode("utf-8")
                        f.seek(0, 2)
                        f.write(raw + b"\n")
                        f.flush()
                        # fsync 按条数节流
                        self._since_fsync += 1
                        if self._since_fsync >= self.fsync_every:
                            os.fsync(f.fileno())
                            self._since_fsync = 0
                    finally:
                        if locked:
                            try:
                                f.seek(0)
                                msvcrt.locking(f.fileno(), msvcrt.LK_UNLCK, 1)
                            except OSError:
                                pass
                self.records += 1
                if not self.ok:
                    self.ok = True
                    self.last_error = ""
            except Exception as e:
                # 绝不静默：日志写不进去必须让人知道，否则会以为"没有调用"
                self.ok = False
                self.error_count += 1
                self.last_error = f"{type(e).__name__}: {e}"
                print(f"[JEV_AUDIT_ERROR] 审计日志写入失败: {self.last_error}",
                      file=sys.stderr, flush=True)

    # ---------------- 查询 / 维护 ----------------
    def size_bytes(self) -> int:
        try:
            return os.path.getsize(self.path)
        except OSError:
            return 0

    def size_human(self) -> str:
        n = self.size_bytes()
        for unit in ("B", "KB", "MB", "GB"):
            if n < 1024 or unit == "GB":
                return f"{n:.1f} {unit}" if unit != "B" else f"{n} B"
            n /= 1024.0
        return f"{n:.1f} GB"

    def tail(self, n: int = 1) -> list[dict]:
        """读最近 n 条，供界面自检与测试使用。"""
        try:
            with open(self.path, "r", encoding="utf-8", errors="replace") as f:
                lines = f.readlines()[-n:]
            out = []
            for ln in lines:
                ln = ln.strip()
                if ln:
                    try:
                        out.append(json.loads(ln))
                    except json.JSONDecodeError:
                        pass
            return out
        except OSError:
            return []

    def flush_now(self) -> None:
        """立刻把缓冲刷到盘（关闭前调用，保证最后几条不丢）。"""
        try:
            with open(self.path, "a+b") as f:
                f.flush()
                os.fsync(f.fileno())
            self._since_fsync = 0
        except Exception:
            pass

    def clear(self) -> tuple[bool, str]:
        """一键清空。日志含完整聊天原文，必须能一键清掉。

        必须与 record() 拿**同一把跨进程文件锁**（§6.6 复核）：HUD 原来自己
        `open(...,"wb")` 截断，不持任何锁 —— 与另一实例正在写的记录会交错出
        空洞/半行，而且 seq 不回退、清空后编号接着往上涨。
        """
        with self._lock:
            try:
                with open(self.path, "a+b") as f:
                    locked = False
                    if _HAS_MSVCRT:
                        try:
                            f.seek(0)
                            msvcrt.locking(f.fileno(), msvcrt.LK_LOCK, 1)
                            locked = True
                        except OSError as le:
                            if not self._lock_warned:
                                self._lock_warned = True
                                print(f"[JEV_AUDIT_WARN] 跨进程锁不可用，"
                                      f"清空降级为无锁: {le}",
                                      file=sys.stderr, flush=True)
                    try:
                        f.truncate(0)
                        f.flush()
                        os.fsync(f.fileno())
                    finally:
                        if locked:
                            try:
                                f.seek(0)
                                msvcrt.locking(f.fileno(), msvcrt.LK_UNLCK, 1)
                            except OSError:
                                pass
                self.records = 0
                self._seq = 0
                return True, "已清空"
            except Exception as e:
                return False, f"{type(e).__name__}: {e}"


def key_fingerprint(key: str) -> str:
    """只留指纹，绝不落明文。

    原来记的是 `key[:7]…key[-4:]` —— 那**不是指纹，是明文片段**：
    11 个真实字符 + 长度，落在最容易外传的文件里（§6.6 复核）。
    改成 sha256 前 12 位十六进制，保留"能比对是不是同一把 Key"的用途。
    """
    if not key:
        return ""
    return (f"sha256:{hashlib.sha256(key.encode('utf-8')).hexdigest()[:12]} "
            f"(len={len(key)})")
