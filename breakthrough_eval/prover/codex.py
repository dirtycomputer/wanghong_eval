"""``codex exec`` PROVER backend (plan §3.1, §3.2).

Wraps the Codex CLI as the agent loop. This is the *real* backend; it is not
exercised in this environment (no ``codex`` binary), so it degrades gracefully:
``available()`` returns False and ``run()`` raises a clear error instead of
failing at import time. The command/config it builds mirrors plan §3.2 exactly.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path

from ..models import ToolCall, UsageStats
from .base import BackendResponse, ProverBackend, ProverContext


@dataclass
class CodexProverConfig:
    model: str = "<pre-cutoff-model>"
    model_provider: str = "local-vllm"
    arxiv_mcp_command: str = "arxiv-frozen-mcp"
    scaffold_version: str = "codex-vanilla"
    timeout_seconds: int = 1800


def render_config_toml(cfg: CodexProverConfig, cutoff_iso: str) -> str:
    """Render ``prover/config.toml`` (plan §3.2), injecting the retrieval cutoff."""
    return (
        f'model = "{cfg.model}"\n'
        f'model_provider = "{cfg.model_provider}"\n'
        'web_search = "disabled"\n'  # red line: no web retrieval
        "[mcp_servers.arxiv_frozen]\n"
        f'command = "{cfg.arxiv_mcp_command}"\n'
        f'env = {{ CUTOFF_DATE = "{cutoff_iso}" }}\n'
        "required = true\n"  # fail loudly rather than run blind
    )


class CodexProverBackend(ProverBackend):
    name = "codex"

    def __init__(self, config: CodexProverConfig | None = None):
        self.config = config or CodexProverConfig()
        self.scaffold_version = self.config.scaffold_version

    def available(self) -> bool:
        return shutil.which("codex") is not None

    def run(self, ctx: ProverContext) -> BackendResponse:
        if not self.available():
            raise RuntimeError(
                "codex CLI 不可用: 请安装 Codex CLI 并配置 local-vllm 端点后再使用 "
                "CodexProverBackend (本环境请改用 mock backend)。"
            )
        cutoff_iso = ctx.task.retrieval_cutoff.isoformat()
        with tempfile.TemporaryDirectory() as tmp:
            workdir = Path(tmp)
            cfg_path = workdir / "config.toml"
            cfg_path.write_text(render_config_toml(self.config, cutoff_iso))
            prompt = ctx.system + "\n\n" + ctx.user

            # Mirrors plan §3.2's invocation.
            cmd = [
                "codex",
                "exec",
                "--sandbox", "workspace-write",
                "--ask-for-approval", "never",
                "--ignore-user-config",
                "--json",
                "-c", "web_search=disabled",
                "-c", f"config_file={cfg_path}",
                "--cd", str(workdir),
                prompt,
            ]
            proc = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=self.config.timeout_seconds,
            )
            return self._parse(proc.stdout, proc.stderr)

    # ------------------------------------------------------------------ #
    @staticmethod
    def _parse(stdout: str, stderr: str) -> BackendResponse:
        """Parse codex ``--json`` transcript: final text + tool calls."""
        text_parts: list[str] = []
        tool_calls: list[ToolCall] = []
        usage = UsageStats()
        for line in stdout.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                text_parts.append(line)
                continue
            etype = event.get("type", "")
            if etype in ("message", "agent_message", "assistant"):
                text_parts.append(event.get("text") or event.get("content", ""))
            elif "tool" in etype or etype == "function_call":
                tool_calls.append(
                    ToolCall(
                        tool=event.get("name", "tool"),
                        query=str(event.get("arguments", ""))[:500],
                        host=event.get("host", ""),
                    )
                )
            usage.input_tokens += int(event.get("input_tokens", 0) or 0)
            usage.output_tokens += int(event.get("output_tokens", 0) or 0)
        return BackendResponse(
            text="\n".join(p for p in text_parts if p).strip(),
            tool_calls=tool_calls,
            usage=usage,
        )


def _factory(**kwargs) -> CodexProverBackend:
    cfg = CodexProverConfig(
        model=kwargs.get("model", "<pre-cutoff-model>"),
        model_provider=kwargs.get("model_provider", "local-vllm"),
        scaffold_version=kwargs.get("scaffold_version", "codex-vanilla"),
    )
    return CodexProverBackend(cfg)
