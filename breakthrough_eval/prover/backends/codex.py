"""``codex exec`` PROVER backend (plan §3.1, §3.2).

Wraps the Codex CLI as the agent loop. This is the *real* backend; it is not
exercised in this environment (no ``codex`` binary), so it degrades gracefully:
``available()`` returns False and ``run()`` raises a clear error instead of
failing at import time. The command/config it builds mirrors plan §3.2 exactly.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path

from ...specification import ApiConfig, HarnessConfig, ToolCall, UsageStats
from ..base import BackendResponse, ProverBackend, ProverContext

REPO_ROOT = Path(__file__).resolve().parents[3]


@dataclass
class CodexProverConfig:
    api: ApiConfig
    harness: HarnessConfig


def render_config_toml(
    cfg: CodexProverConfig,
    cutoff_iso: str,
    workdir: Path | None = None,
) -> str:
    """Render Codex config.toml from YAML config."""
    provider = cfg.api.model_provider
    config_path = REPO_ROOT / cfg.harness.search_config_path
    module = cfg.harness.search_mcp_module
    transcript_env = ""
    if workdir is not None:
        transcript_env = f'RESTRICTED_SEARCH_TRANSCRIPT = "{workdir / "restricted_search_calls.jsonl"}", '
    trusted_projects = (
        f'[projects."{REPO_ROOT}"]\n'
        'trust_level = "trusted"\n'
    )
    if workdir is not None and workdir != REPO_ROOT:
        trusted_projects += (
            "\n"
            f'[projects."{workdir}"]\n'
            'trust_level = "trusted"\n'
        )
    return (
        f'model_provider = "{provider}"\n'
        f'model = "{cfg.api.model}"\n'
        f'review_model = "{cfg.api.model}"\n'
        'disable_response_storage = true\n'
        'network_access = "enabled"\n'
        'approvals_reviewer = "user"\n'
        "\n"
        f"[model_providers.{provider}]\n"
        f'name = "{provider}"\n'
        f'base_url = "{cfg.api.base_url.rstrip("/")}"\n'
        f'wire_api = "{cfg.api.wire_api}"\n'
        'requires_openai_auth = true\n'
        "\n"
        "[features]\n"
        "goals = true\n"
        "\n"
        f"{trusted_projects}"
        "\n"
        "[mcp_servers.restricted_search]\n"
        f'command = "{sys.executable}"\n'
        f'args = ["-m", "{module}"]\n'
        "env = { "
        f'RESTRICTED_SEARCH_CONFIG = "{config_path}", '
        f"{transcript_env}"
        f'PYTHONPATH = "{REPO_ROOT}" '
        "}\n"
        "required = true\n"
        'default_tools_approval_mode = "approve"\n'
        'enabled_tools = ["restricted_search"]\n'
        'env_vars = ["RESTRICTED_SEARCH_CUTOFF"]\n'
        "tool_timeout_sec = 1200\n"
    )


def render_auth_json(cfg: CodexProverConfig) -> str:
    return json.dumps({"OPENAI_API_KEY": cfg.api.api_key}, ensure_ascii=False, indent=2) + "\n"


class CodexProverBackend(ProverBackend):
    name = "codex"

    def __init__(self, api: ApiConfig, harness: HarnessConfig):
        self.config = CodexProverConfig(api=api, harness=harness)
        self.scaffold_version = self.config.harness.scaffold_version

    def available(self) -> bool:
        return shutil.which("codex") is not None

    def run(self, ctx: ProverContext) -> BackendResponse:
        if not self.available():
            raise RuntimeError(
                "codex CLI 不可用: 请安装 Codex CLI 后再使用 CodexProverBackend "
                "(模型 API 配置来自 models_registry.yaml 的 api 块)。"
            )
        cutoff_iso = ctx.task.retrieval_cutoff.isoformat()
        with tempfile.TemporaryDirectory() as tmp:
            workdir = Path(tmp)
            codex_home = Path(self.config.harness.codex_home_dir)
            if not codex_home.is_absolute():
                codex_home = REPO_ROOT / codex_home
            codex_home.mkdir(parents=True, exist_ok=True)
            cfg_path = codex_home / "config.toml"
            cfg_path.write_text(render_config_toml(self.config, cutoff_iso, workdir))
            (codex_home / "auth.json").write_text(render_auth_json(self.config), encoding="utf-8")
            last_message_path = workdir / "last_message.txt"
            prompt = ctx.system + "\n\n" + ctx.user
            env = dict(os.environ)
            env["CODEX_HOME"] = str(codex_home)
            env["RESTRICTED_SEARCH_CUTOFF"] = cutoff_iso

            cmd = [
                "codex",
                "exec",
                "--sandbox", "workspace-write",
                "--ephemeral",
                "--ignore-rules",
                "--skip-git-repo-check",
                "--json",
                "-c", 'approval_policy="never"',
                "-c", "web_search=disabled",
                "-o", str(last_message_path),
                "--cd", str(workdir),
                "-",
            ]
            proc = subprocess.run(
                cmd,
                input=prompt,
                capture_output=True,
                text=True,
                env=env,
                timeout=self.config.harness.timeout_seconds,
            )
            _write_trace(ctx, proc.stdout, proc.stderr)
            if proc.returncode != 0:
                raise RuntimeError(
                    f"codex exec 退出码 {proc.returncode}: "
                    f"{(proc.stderr or proc.stdout)[-500:]}"
                )
            resp = self._parse(proc.stdout, proc.stderr)
            if not resp.text.strip() and last_message_path.exists():
                resp.text = last_message_path.read_text(encoding="utf-8").strip()
            from .cli_utils import read_mcp_transcript

            resp.tool_calls.extend(read_mcp_transcript(workdir / "restricted_search_calls.jsonl"))
            return resp

    # ------------------------------------------------------------------ #
    @staticmethod
    def _parse(stdout: str, stderr: str) -> BackendResponse:
        """Parse codex ``--json`` transcript: final text + tool calls."""
        text_parts: list[str] = []
        tool_calls: list[ToolCall] = []
        usage = UsageStats()

        def collect_text(node) -> list[str]:
            if isinstance(node, str):
                return [node]
            if isinstance(node, list):
                out: list[str] = []
                for child in node:
                    out.extend(collect_text(child))
                return out
            if isinstance(node, dict):
                if isinstance(node.get("text"), str):
                    return [node["text"]]
                if isinstance(node.get("content"), str):
                    return [node["content"]]
                if isinstance(node.get("content"), list):
                    return collect_text(node["content"])
            return []

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
            elif etype in ("item.completed", "item.updated"):
                item = event.get("item") or {}
                if item.get("type") == "message" and item.get("role") == "assistant":
                    text_parts.extend(collect_text(item.get("content")))
                elif item.get("type") in ("function_call", "tool_call"):
                    tool_calls.append(
                        ToolCall(
                            tool=item.get("name", "tool"),
                            query=str(item.get("arguments", ""))[:500],
                            host=item.get("host", ""),
                        )
                    )
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
            if etype == "token_count":
                token_usage = ((event.get("info") or {}).get("total_token_usage") or {})
                usage.input_tokens += int(token_usage.get("input_tokens", 0) or 0)
                usage.output_tokens += int(token_usage.get("output_tokens", 0) or 0)
        return BackendResponse(
            text="\n".join(p for p in text_parts if p).strip(),
            tool_calls=tool_calls,
            usage=usage,
        )


def _write_trace(ctx: ProverContext, stdout: str, stderr: str) -> None:
    if ctx.log_dir is None:
        return
    ctx.log_dir.mkdir(parents=True, exist_ok=True)
    prefix = ctx.probe_id or ctx.phase
    content = stdout
    if stderr.strip():
        content += json.dumps(
            {"type": "process.stderr", "text": stderr},
            ensure_ascii=False,
        ) + "\n"
    if ctx.phase == "prove":
        (ctx.log_dir / "trace.jsonl").write_text(content, encoding="utf-8")
    else:
        (ctx.log_dir / f"{prefix}_trace.jsonl").write_text(content, encoding="utf-8")
