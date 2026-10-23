"""LLM 客户端：OpenAI 兼容 chat/completions，带 usage 捕获、重试与限流。

HTTP 模式沿用工作目录 nl2ke.py（urllib + LLMError），新增：
- chat_with_usage 返回 token 用量与耗时（P2 指标）
- 429/5xx/网络错误指数退避重试，4xx 不重试
- 生成与 judge 共用同一客户端（不同 model）
"""
from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Sequence

from nl2ke import LLMError

DEFAULT_BASE_URL = "https://api.deepseek.com/v1"

RETRIABLE_STATUS = {408, 409, 425, 429, 500, 502, 503, 504}


@dataclass
class ChatResult:
    content: str
    prompt_tokens: int | None
    completion_tokens: int | None
    latency_ms: int
    attempts: int


@dataclass
class ClientConfig:
    base_url: str
    api_key: str
    model: str
    temperature: float = 0.0
    timeout: float = 120.0
    max_retries: int = 3


def resolve_api(
    model: str,
    *,
    base_url: str | None = None,
    api_key: str | None = None,
    temperature: float = 0.0,
    timeout: float = 120.0,
    max_retries: int = 3,
) -> ClientConfig:
    """解析 API 配置。优先级：显式参数 > configs/api.json 设置文件 > 环境变量 > 内置默认。"""
    from .settings import load_api_settings

    saved = load_api_settings()
    base_url = (
        base_url
        or saved.get("base_url")
        or os.getenv("NL2KE_BASE_URL")
        or os.getenv("OPENAI_BASE_URL")
        or DEFAULT_BASE_URL
    )
    api_key = (
        api_key
        or saved.get("api_key")
        or os.getenv("NL2KE_API_KEY")
        or os.getenv("DEEPSEEK_API_KEY")
        or os.getenv("OPENAI_API_KEY")
    )
    if not api_key:
        raise LLMError(
            "未找到 API key：到看板「设置」页配置，或设置环境变量 NL2KE_API_KEY"
            "（也支持 DEEPSEEK_API_KEY / OPENAI_API_KEY / configs/api.json）。"
        )
    return ClientConfig(
        base_url=base_url.rstrip("/"),
        api_key=api_key,
        model=model,
        temperature=temperature,
        timeout=timeout,
        max_retries=max_retries,
    )


def _post_once(cfg: ClientConfig, messages: Sequence[dict[str, str]]) -> tuple[str, dict]:
    url = cfg.base_url + "/chat/completions"
    payload = {
        "model": cfg.model,
        "messages": list(messages),
        "temperature": cfg.temperature,
        "stream": False,
    }
    request = urllib.request.Request(
        url,
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers={
            "Content-Type": "application/json",
            "Accept": "application/json",
            "Authorization": f"Bearer {cfg.api_key}",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=cfg.timeout) as response:
            data = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        try:
            message = json.loads(body)["error"]["message"]
        except (ValueError, KeyError, TypeError):
            message = body or str(exc)
        raise _HttpError(exc.code, f"API 请求失败（HTTP {exc.code}）：{message}") from exc
    except urllib.error.URLError as exc:
        raise _RetriableError(f"网络错误：{exc.reason}") from exc
    except TimeoutError as exc:
        raise _RetriableError(f"请求超时（{cfg.timeout:g} 秒）。") from exc

    try:
        content = data["choices"][0]["message"]["content"]
    except (KeyError, IndexError, TypeError) as exc:
        preview = json.dumps(data, ensure_ascii=False)[:500]
        raise LLMError(f"无法解析 API 响应：{preview}") from exc
    if not isinstance(content, str) or not content.strip():
        raise LLMError(f"API 返回了空内容：{json.dumps(data, ensure_ascii=False)[:500]}")
    usage = data.get("usage") or {}
    return content, usage


class _HttpError(LLMError):
    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status


class _RetriableError(LLMError):
    pass


def chat_with_usage(cfg: ClientConfig, messages: Sequence[dict[str, str]]) -> ChatResult:
    """带重试的 chat 调用。可重试错误指数退避（1s/2s/4s…），其余立即抛出。"""
    attempts = 0
    started = time.perf_counter()
    while True:
        attempts += 1
        try:
            content, usage = _post_once(cfg, messages)
            latency_ms = int((time.perf_counter() - started) * 1000)
            return ChatResult(
                content=content,
                prompt_tokens=_as_int(usage.get("prompt_tokens")),
                completion_tokens=_as_int(usage.get("completion_tokens")),
                latency_ms=latency_ms,
                attempts=attempts,
            )
        except _HttpError as exc:
            if exc.status not in RETRIABLE_STATUS or attempts > cfg.max_retries:
                raise LLMError(str(exc)) from exc
        except _RetriableError as exc:
            if attempts > cfg.max_retries:
                raise LLMError(str(exc)) from exc
        time.sleep(min(2 ** (attempts - 1), 8))


def _as_int(value) -> int | None:
    try:
        return int(value) if value is not None else None
    except (TypeError, ValueError):
        return None
