"""Minimal OpenRouter (OpenAI-compatible) chat client — stdlib only.

Used by the real PROVER backend (`prover/openrouter.py`) and the real
LLM-judge wiring (`eval/openrouter_judge.py`). The HTTP transport is injectable
so the rest of the system stays testable offline.

The API key is read from the ``OPENROUTER_KEY`` environment variable and is
*never* written to disk or logs.
"""

from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from typing import Callable, Optional

DEFAULT_BASE_URL = "https://openrouter.ai/api/v1/chat/completions"
DEFAULT_KEY_ENV = "OPENROUTER_KEY"

# transport(url, payload_dict, headers_dict) -> response_dict
Transport = Callable[[str, dict, dict], dict]


class LLMError(RuntimeError):
    pass


@dataclass
class ChatResult:
    content: str
    tool_calls: list[dict] = field(default_factory=list)
    usage: dict = field(default_factory=dict)
    raw: dict = field(default_factory=dict)


class OpenRouterClient:
    def __init__(
        self,
        model: str,
        api_key: Optional[str] = None,
        api_key_env: str = DEFAULT_KEY_ENV,
        base_url: str = DEFAULT_BASE_URL,
        temperature: float = 0.3,
        max_tokens: int = 2048,
        timeout: int = 240,
        max_retries: int = 4,
        extra_headers: Optional[dict] = None,
        provider_prefs: Optional[dict] = None,
        transport: Optional[Transport] = None,
    ):
        self.model = model
        self.api_key = api_key if api_key is not None else os.environ.get(api_key_env)
        self.base_url = base_url
        self.temperature = temperature
        self.max_tokens = max_tokens
        self.timeout = timeout
        self.max_retries = max_retries
        # OpenRouter provider routing (e.g. {"require_parameters": True} to only
        # route to providers that actually support tools / response_format).
        self.provider_prefs = provider_prefs
        self.extra_headers = extra_headers or {
            "HTTP-Referer": "https://github.com/dirtycomputer/wanghong_eval",
            "X-Title": "breakthrough-eval",
        }
        self._transport = transport or self._http_post

    def available(self) -> bool:
        return bool(self.api_key)

    # ------------------------------------------------------------------ #
    def chat(
        self,
        messages: list[dict],
        tools: Optional[list[dict]] = None,
        tool_choice: Optional[str] = None,
        response_format: Optional[dict] = None,
        max_tokens: Optional[int] = None,
    ) -> ChatResult:
        if not self.api_key:
            raise LLMError(
                f"缺少 API key: 请设置环境变量 {DEFAULT_KEY_ENV} (OpenRouter)。"
            )
        payload: dict = {
            "model": self.model,
            "messages": messages,
            "temperature": self.temperature,
            "max_tokens": max_tokens or self.max_tokens,
        }
        if tools:
            payload["tools"] = tools
            payload["tool_choice"] = tool_choice or "auto"
        if response_format:
            payload["response_format"] = response_format
        if self.provider_prefs:
            payload["provider"] = self.provider_prefs

        data = self._post_with_retries(payload)
        if "choices" not in data or not data["choices"]:
            # OpenRouter returns errors as a 200 body with an "error" field.
            err = data.get("error")
            raise LLMError(f"OpenRouter 无 choices: {err or str(data)[:300]}")
        msg = data["choices"][0]["message"]
        return ChatResult(
            content=msg.get("content") or "",
            tool_calls=msg.get("tool_calls") or [],
            usage=data.get("usage") or {},
            raw=data,
        )

    # ------------------------------------------------------------------ #
    def _post_with_retries(self, payload: dict) -> dict:
        last: Optional[Exception] = None
        for attempt in range(self.max_retries):
            try:
                return self._transport(self.base_url, payload, self._headers())
            except Exception as exc:  # noqa: BLE001 - retry on any transport error
                last = exc
                if attempt < self.max_retries - 1:
                    time.sleep(2 ** attempt)
        raise LLMError(f"OpenRouter 调用失败 (重试 {self.max_retries} 次): {last}")

    def _headers(self) -> dict:
        h = {"Authorization": f"Bearer {self.api_key}", "Content-Type": "application/json"}
        h.update(self.extra_headers)
        return h

    def _http_post(self, url: str, payload: dict, headers: dict) -> dict:
        req = urllib.request.Request(
            url, data=json.dumps(payload).encode("utf-8"), headers=headers, method="POST"
        )
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", "replace")[:400]
            raise LLMError(f"HTTP {exc.code}: {detail}") from exc
        except urllib.error.URLError as exc:
            raise LLMError(f"网络错误: {exc.reason}") from exc

    @staticmethod
    def usage_tokens(usage: dict) -> tuple[int, int]:
        return int(usage.get("prompt_tokens", 0) or 0), int(
            usage.get("completion_tokens", 0) or 0
        )
