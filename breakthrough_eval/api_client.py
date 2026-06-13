"""Minimal OpenAI-compatible chat client — stdlib only.

Used by direct PROVER backends and real LLM-judge wiring. The HTTP transport is injectable
so the rest of the system stays testable offline.
"""

from __future__ import annotations

import json
import logging
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from typing import Callable, Optional

log = logging.getLogger(__name__)

DEFAULT_BASE_URL = "https://openrouter.ai/api/v1"

# transport(url, payload_dict, headers_dict) -> response_dict
Transport = Callable[[str, dict, dict], dict]


class APIError(RuntimeError):
    pass


@dataclass
class ChatResult:
    content: str
    tool_calls: list[dict] = field(default_factory=list)
    usage: dict = field(default_factory=dict)
    raw: dict = field(default_factory=dict)


class OpenAICompatibleClient:
    def __init__(
        self,
        model: str,
        api_key: Optional[str] = None,
        base_url: str = DEFAULT_BASE_URL,
        temperature: Optional[float] = None,
        max_tokens: int = 2048,
        timeout: int = 240,
        max_retries: int = 4,
        extra_headers: Optional[dict] = None,
        provider_prefs: Optional[dict] = None,
        transport: Optional[Transport] = None,
    ):
        self.model = model
        self.api_key = api_key or ""
        self.base_url = base_url.rstrip("/")
        self.temperature = temperature
        self.max_tokens = max_tokens
        self.timeout = timeout
        self.max_retries = max_retries
        # OpenAI-compatible provider routing (e.g. {"require_parameters": True} to only
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
        temperature: Optional[float] = None,
    ) -> ChatResult:
        if not self.api_key:
            raise APIError("缺少 API key: 请在 YAML 的 api.api_key 填写。")
        payload: dict = {
            "model": self.model,
            "messages": messages,
            "max_tokens": max_tokens or self.max_tokens,
        }
        temp = self.temperature if temperature is None else temperature
        if temp is not None:
            payload["temperature"] = temp
        if tools:
            payload["tools"] = tools
            payload["tool_choice"] = tool_choice or "auto"
        if response_format:
            payload["response_format"] = response_format
        if self.provider_prefs:
            payload["provider"] = self.provider_prefs

        log.debug(
            "chat → model=%s msgs=%d tools=%s max_tokens=%s",
            self.model, len(messages), bool(tools), payload["max_tokens"],
        )
        t0 = time.perf_counter()
        data = self._post_with_retries(payload)
        elapsed = time.perf_counter() - t0

        if "choices" not in data or not data["choices"]:
            # OpenAI-compatible returns errors as a 200 body with an "error" field.
            err = data.get("error")
            log.error("chat ✗ model=%s %.1fs 无 choices: %s", self.model, elapsed, err)
            raise APIError(f"OpenAI-compatible 无 choices: {err or str(data)[:300]}")
        msg = data["choices"][0]["message"]
        pt, ct = self.usage_tokens(data.get("usage") or {})
        log.info(
            "chat ✓ model=%s %.1fs provider=%s tok(in/out)=%d/%d tool_calls=%d",
            self.model, elapsed, data.get("provider", "?"), pt, ct,
            len(msg.get("tool_calls") or []),
        )
        return ChatResult(
            content=self._message_content(msg),
            tool_calls=msg.get("tool_calls") or [],
            usage=data.get("usage") or {},
            raw=data,
        )

    # ------------------------------------------------------------------ #
    def _post_with_retries(self, payload: dict) -> dict:
        last: Optional[Exception] = None
        for attempt in range(self.max_retries):
            try:
                return self._transport(self.chat_url, payload, self._headers())
            except Exception as exc:  # noqa: BLE001 - retry on any transport error
                last = exc
                if attempt < self.max_retries - 1:
                    backoff = 2 ** attempt
                    log.warning(
                        "OpenAI-compatible 调用失败 (model=%s, attempt %d/%d), %ds 后重试: %s",
                        self.model, attempt + 1, self.max_retries, backoff, str(exc)[:200],
                    )
                    time.sleep(backoff)
        raise APIError(f"OpenAI-compatible 调用失败 (重试 {self.max_retries} 次): {last}")

    def _headers(self) -> dict:
        h = {"Authorization": f"Bearer {self.api_key}", "Content-Type": "application/json"}
        h.update(self.extra_headers)
        return h

    @property
    def chat_url(self) -> str:
        if self.base_url.endswith("/chat/completions"):
            return self.base_url
        return self.base_url + "/chat/completions"

    def _http_post(self, url: str, payload: dict, headers: dict) -> dict:
        req = urllib.request.Request(
            url, data=json.dumps(payload).encode("utf-8"), headers=headers, method="POST"
        )
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", "replace")[:400]
            raise APIError(f"HTTP {exc.code}: {detail}") from exc
        except urllib.error.URLError as exc:
            raise APIError(f"网络错误: {exc.reason}") from exc

    @staticmethod
    def usage_tokens(usage: dict) -> tuple[int, int]:
        prompt = usage.get("prompt_tokens", usage.get("input_tokens", 0))
        completion = usage.get("completion_tokens", usage.get("output_tokens", 0))
        return int(prompt or 0), int(completion or 0)

    @staticmethod
    def _message_content(message: dict) -> str:
        content = message.get("content")
        if isinstance(content, str):
            return content
        if isinstance(content, list):
            parts: list[str] = []
            for item in content:
                if isinstance(item, str):
                    parts.append(item)
                elif isinstance(item, dict):
                    text = item.get("text") or item.get("content")
                    if isinstance(text, str):
                        parts.append(text)
            return "".join(parts)
        return ""
