"""Jev API 客户端 —— 只保留"怎么发请求"这件事。

问题集与阈值全部在 `qset.py`（官方 skill 要求常量集中到单一文件供人审），
本文件只负责：认证、重试、以及**把每次尝试完整写进审计日志**。

审计日志是必填而不是可选的，原因见 `jev_log.py`：只要它是可选的，
就一定会有一条调用路径漏记，而漏记的那次往往正是事后要看的那次。
"""
from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.request
from typing import Any

from .jev_log import JevAudit, key_fingerprint

API_URL = "https://api.typesafe.ai/v1/systemone"
MODEL = "jev-1.13.0"  # 固定版本号，不用别名（别名漂移会让调好的阈值失效）

# 重试策略：429/529 必须退避重试（实测上游 529 可持续 5–7 秒）
RETRY_STATUS = {429, 502, 503, 529}
MAX_ATTEMPTS = 5


def load_api_key(env_path: str) -> str:
    """从 .env 读取 jevkey。不打印、不落日志。"""
    if os.path.exists(env_path):
        with open(env_path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line.startswith("jevkey="):
                    return line.split("=", 1)[1].strip().strip('"').strip("'")
    key = os.environ.get("JEVKEY")
    if key:
        return key
    raise RuntimeError("未找到 jevkey：请检查 .env 或设置环境变量 JEVKEY")


class JevClient:
    """System One 客户端。审计日志为必填项。

    caller 用于区分来源（hud / eval / prove），便于事后按来源筛日志。
    """

    def __init__(self, api_key: str, audit_path: str, caller: str = "unknown",
                 verbose: bool = True):
        self.api_key = api_key
        self.verbose = verbose
        self.caller = caller
        self.audit = JevAudit(audit_path)
        self.calls = 0
        self.total_input_tokens = 0
        self.total_output_tokens = 0
        self.total_latency = 0.0

    @property
    def cost_usd(self) -> float:
        """按官方价：输入 $0.042/Mtok，输出免费。"""
        return self.total_input_tokens / 1_000_000 * 0.042

    def _log(self, *, attempt: int, status=None, status_text: str = "",
             request_raw: str = "", response_raw: str = "",
             response=None, parse_error: str = "", error: str = "",
             elapsed_s: float = 0.0, retry_slept_s: float = 0.0,
             meta: dict | None = None) -> None:
        self.audit.record(
            caller=self.caller,
            attempt=attempt,
            max_attempts=MAX_ATTEMPTS,
            endpoint=API_URL,
            model_requested=MODEL,
            key_fingerprint=key_fingerprint(self.api_key),
            http_status=status,
            status_text=status_text,
            request_bytes=len(request_raw.encode("utf-8")),
            request_raw=request_raw,
            response_bytes=len(response_raw.encode("utf-8")),
            response_raw=response_raw,
            response=response,
            parse_error=parse_error,
            elapsed_s=round(elapsed_s, 3),
            retry_slept_s=retry_slept_s,
            error=error,
            meta=meta,
        )

    def system_one(self, state: Any, questions: dict,
                   meta: dict | None = None) -> dict:
        # 先确认日志可用再发请求：宁可拒绝调用，也不要产生一次无据可查的判断。
        self.audit.assert_usable()
        req_text = json.dumps(
            {"state": state, "model": MODEL, "questions": questions},
            ensure_ascii=False,
        )
        payload = req_text.encode("utf-8")

        last_err = None
        for attempt in range(1, MAX_ATTEMPTS + 1):
            req = urllib.request.Request(
                API_URL,
                data=payload,
                headers={
                    "Authorization": f"Bearer {self.api_key}",
                    "Content-Type": "application/json",
                },
                method="POST",
            )
            t0 = time.time()
            try:
                with urllib.request.urlopen(req, timeout=90) as resp:
                    raw = resp.read().decode("utf-8", "replace")
                    status = resp.status
                    status_text = getattr(resp, "reason", "") or ""
                dt = time.time() - t0
                try:
                    body = json.loads(raw)
                    parse_error = ""
                except json.JSONDecodeError as je:
                    body, parse_error = None, f"{type(je).__name__}: {je}"

                self.calls += 1
                self.total_latency += dt
                usage = (body or {}).get("usage", {}) or {}
                self.total_input_tokens += usage.get("input_tokens", 0)
                self.total_output_tokens += usage.get("output_tokens", 0)
                if self.verbose:
                    print(f"    [API] {status} {dt:.2f}s  "
                          f"in={usage.get('input_tokens')} "
                          f"out={usage.get('output_tokens')}  "
                          f"model={(body or {}).get('model')}")
                self._log(attempt=attempt, status=status,
                          status_text=status_text, request_raw=req_text,
                          response_raw=raw, response=body,
                          parse_error=parse_error, elapsed_s=dt, meta=meta)
                if body is None:
                    raise RuntimeError(f"响应无法解析为 JSON: {parse_error}")
                return body

            except urllib.error.HTTPError as e:
                last_err = e
                # 非 2xx 的完整 body 也要留（原先截到 500 字，真因常在第 501 字）
                err_raw = e.read().decode("utf-8", "replace")
                dt = time.time() - t0
                if e.code in RETRY_STATUS and attempt < MAX_ATTEMPTS:
                    wait = 2 ** (attempt - 1)
                    if self.verbose:
                        print(f"    [API] {e.code} 退避 {wait}s（第 {attempt} 次）")
                    # 退避的这一次也成条，否则"重试了几次、每次多久"无从谈起
                    self._log(attempt=attempt, status=e.code,
                              status_text=str(getattr(e, "reason", "")),
                              request_raw=req_text, response_raw=err_raw,
                              error=err_raw, elapsed_s=dt, retry_slept_s=wait)
                    time.sleep(wait)
                    continue
                self._log(attempt=attempt, status=e.code,
                          status_text=str(getattr(e, "reason", "")),
                          request_raw=req_text, response_raw=err_raw,
                          error=err_raw, elapsed_s=dt)
                raise RuntimeError(f"HTTP {e.code}: {err_raw[:500]}") from e

            except (urllib.error.URLError, TimeoutError) as e:
                last_err = e
                dt = time.time() - t0
                if attempt < MAX_ATTEMPTS:
                    wait = 2 ** (attempt - 1)
                    if self.verbose:
                        print(f"    [API] 网络错误 退避 {wait}s: {e}")
                    self._log(attempt=attempt, status=None,
                              status_text="network-error", request_raw=req_text,
                              error=f"{type(e).__name__}: {e}",
                              elapsed_s=dt, retry_slept_s=wait)
                    time.sleep(wait)
                    continue
                self._log(attempt=attempt, status=None,
                          status_text="network-error", request_raw=req_text,
                          error=f"{type(e).__name__}: {e}", elapsed_s=dt)
                raise
        raise RuntimeError(f"重试 {MAX_ATTEMPTS} 次仍失败: {last_err}")
