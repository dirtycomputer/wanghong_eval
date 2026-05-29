"""Real PROVER backend over OpenRouter (OpenAI-compatible) — plan §3.

An agent loop that exposes a single tool, ``search_arxiv``, backed by the
time-frozen ``ArxivFrozenSource`` (so the cutoff is enforced at the tool layer).
Every tool call is recorded into the transcript with the returned submission
dates, which the §8.4 audit then checks.

Web search is never offered as a tool (red line). The probe phase is run
*without* tools — we want the model's unaided answer (plan §3.3).
"""

from __future__ import annotations

import json

from ..llm_client import LLMError, OpenRouterClient
from ..models import ToolCall, UsageStats
from .base import BackendResponse, ProverBackend, ProverContext
from .mock import ARXIV_MCP_HOST

ARXIV_TOOL = {
    "type": "function",
    "function": {
        "name": "search_arxiv",
        "description": (
            "检索时间冻结的 arXiv 快照。只会返回截止日期之前提交的论文。"
            "这是你唯一的外部信息源 (不得使用任何联网搜索)。"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "检索关键词"},
                "max_results": {"type": "integer", "description": "返回条数 (默认 5)"},
            },
            "required": ["query"],
        },
    },
}


class OpenRouterProverBackend(ProverBackend):
    name = "openrouter"

    def __init__(
        self,
        model: str,
        scaffold_version: str = "openrouter",
        max_tool_calls: int = 3,
        temperature: float = 0.3,
        max_tokens: int = 1536,
        probe_max_tokens: int = 400,
        client: OpenRouterClient | None = None,
    ):
        self.model = model
        self.scaffold_version = scaffold_version
        self.max_tool_calls = max_tool_calls
        self.probe_max_tokens = probe_max_tokens
        self.client = client or OpenRouterClient(
            model=model,
            temperature=temperature,
            max_tokens=max_tokens,
            # Only route to providers that actually support tools (otherwise the
            # tool-result follow-up intermittently fails); plan §3.2 wants real
            # retrieval, so this keeps the search tool usable.
            provider_prefs={"require_parameters": True},
        )

    def available(self) -> bool:
        return self.client.available()

    def harness_fingerprint(self, model: str) -> str:
        # Use the served model id so the fingerprint reflects what actually ran.
        return f"{model}+{self.name}-{self.scaffold_version}"

    # ------------------------------------------------------------------ #
    def run(self, ctx: ProverContext) -> BackendResponse:
        messages = [
            {"role": "system", "content": ctx.system},
            {"role": "user", "content": ctx.user},
        ]
        usage = UsageStats()
        recorded: list[ToolCall] = []
        if ctx.phase == "probe":
            # Probe: no tools, short answer (we want the unaided answer, fast).
            return self._loop(ctx, messages, None, usage, recorded, self.probe_max_tokens)

        tools = [ARXIV_TOOL]
        try:
            return self._loop(ctx, messages, tools, usage, recorded, None)
        except LLMError:
            # Some models reject tool schemas; fall back to a no-tool answer.
            return self._loop(ctx, messages, None, usage, recorded, None)

    def _loop(self, ctx, messages, tools, usage, recorded, max_tokens) -> BackendResponse:
        for _ in range(self.max_tool_calls):
            res = self.client.chat(messages, tools=tools, max_tokens=max_tokens)
            self._add_usage(usage, res.usage)
            if tools and res.tool_calls:
                messages.append(
                    {"role": "assistant", "content": res.content or "", "tool_calls": res.tool_calls}
                )
                for tc in res.tool_calls:
                    out, calls = self._exec_tool(ctx, tc)
                    recorded.extend(calls)
                    messages.append(
                        {"role": "tool", "tool_call_id": tc.get("id", ""), "content": out}
                    )
                continue
            return BackendResponse(text=res.content, tool_calls=recorded, usage=usage)

        # Tool budget exhausted: force a final answer with no tools.
        res = self.client.chat(messages, tools=None, max_tokens=max_tokens)
        self._add_usage(usage, res.usage)
        return BackendResponse(text=res.content, tool_calls=recorded, usage=usage)

    # ------------------------------------------------------------------ #
    def _exec_tool(self, ctx: ProverContext, tc: dict):
        fn = tc.get("function", {})
        if fn.get("name") != "search_arxiv":
            return json.dumps({"error": "unknown tool"}), []
        try:
            args = json.loads(fn.get("arguments") or "{}")
        except json.JSONDecodeError:
            args = {}
        query = str(args.get("query", "")).strip()
        try:
            n = int(args.get("max_results", 5) or 5)
        except (TypeError, ValueError):
            n = 5
        papers = ctx.source.search(query, max_results=n)
        results = [
            {
                "arxiv_id": p.arxiv_id,
                "title": p.title,
                "authors": p.authors,
                "submission_date": p.submission_date.isoformat(),
                "abstract": p.abstract,
            }
            for p in papers
        ]
        call = ToolCall(
            tool="search_arxiv",
            query=query,
            host=ARXIV_MCP_HOST,
            returned_dates=[p.submission_date for p in papers],
        )
        return json.dumps({"results": results}, ensure_ascii=False), [call]

    @staticmethod
    def _add_usage(usage: UsageStats, raw: dict) -> None:
        pt, ct = OpenRouterClient.usage_tokens(raw)
        usage.input_tokens += pt
        usage.output_tokens += ct


def _factory(**kwargs) -> OpenRouterProverBackend:
    if "model" not in kwargs:
        raise KeyError("openrouter prover 需要 backend_kwargs.model (e.g. google/gemma-4-31b-it)")
    return OpenRouterProverBackend(
        model=kwargs["model"],
        scaffold_version=kwargs.get("scaffold_version", "openrouter"),
        max_tool_calls=kwargs.get("max_tool_calls", 3),
        temperature=kwargs.get("temperature", 0.3),
        max_tokens=kwargs.get("max_tokens", 1536),
        probe_max_tokens=kwargs.get("probe_max_tokens", 400),
    )
