"""Direct OpenAI-compatible PROVER backend."""

from __future__ import annotations

from ...api_client import OpenAICompatibleClient
from ...specification import ApiConfig, HarnessConfig, UsageStats
from ..base import BackendResponse, ProverBackend, ProverContext

NO_TOOL_INSTRUCTION = (
    "本次 direct harness 不提供任何检索工具。不要调用工具, 不要输出工具参数; "
    "请直接基于已有知识作答, 并严格按要求的证明结构输出。"
    "不要只回答这是未解决问题或你不能证明; 必须给出证明尝试、关键估计和未闭合缺口。"
)


class DirectProverBackend(ProverBackend):
    name = "direct"

    def __init__(
        self,
        api: ApiConfig,
        harness: HarnessConfig,
        client: OpenAICompatibleClient | None = None,
    ):
        self.api = api
        self.harness = harness
        self.scaffold_version = harness.scaffold_version
        self.max_tool_calls = harness.max_tool_calls
        self.probe_max_tokens = harness.probe_max_tokens
        self.client = client or OpenAICompatibleClient(
            model=api.model,
            api_key=api.api_key,
            base_url=api.base_url,
            temperature=api.temperature,
            max_tokens=api.max_tokens,
            timeout=api.timeout,
            max_retries=api.max_retries,
        )

    def available(self) -> bool:
        return self.client.available()

    def harness_fingerprint(self, model: str) -> str:
        # Use the served model id so the fingerprint reflects what actually ran.
        return f"{model}+{self.name}-{self.scaffold_version}"

    # ------------------------------------------------------------------ #
    def run(self, ctx: ProverContext) -> BackendResponse:
        system = ctx.system
        user = ctx.user
        if ctx.phase == "prove":
            user = user + "\n\n# 本次运行约束\n" + NO_TOOL_INSTRUCTION
        messages = [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ]
        usage = UsageStats()
        max_tokens = self.probe_max_tokens if ctx.phase == "probe" else None
        res = self.client.chat(messages, tools=None, max_tokens=max_tokens)
        self._add_usage(usage, res.usage)
        return BackendResponse(text=res.content, tool_calls=[], usage=usage)

    @staticmethod
    def _add_usage(usage: UsageStats, raw: dict) -> None:
        pt, ct = OpenAICompatibleClient.usage_tokens(raw)
        usage.input_tokens += pt
        usage.output_tokens += ct
