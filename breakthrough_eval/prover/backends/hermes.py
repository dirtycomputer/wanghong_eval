"""Hermes Agent (NousResearch) PROVER backend (plan §3.1 的可插拔 harness 之一).

把 ``hermes -z`` (one-shot headless) 作为 agent loop。沙箱面收口 (plan §8 红线):

  * ``platform_toolsets.cli: []`` —— 关掉全部内置 toolset (web_search / browser /
    terminal / file / memory ...), 联网面归零;
  * 唯一外部信息源是 ``restricted_search`` MCP (MCP 工具独立于 toolsets 注册,
    yaml 配 provider/key/参数, cutoff 由当前 task 传入, transcript 供 §8.4 审计);
  * 每个 job 用独立 HOME 跑 —— Hermes 的卖点是跨会话记忆/自我成长, 但 benchmark
    里那是 trial 间的状态泄漏, 必须 fresh;
  * 探针阶段绕过 CLI 直连裸模型 (见 ``cli_utils.probe_via_api``)。
"""

from __future__ import annotations

import logging
import os
import re
import shutil
import subprocess
import tempfile
from pathlib import Path

from ...api_client import OpenAICompatibleClient
from ...specification import ApiConfig, HarnessConfig, UsageStats
from ..base import BackendResponse, ProverBackend, ProverContext
from .cli_utils import probe_via_api, read_mcp_transcript

log = logging.getLogger(__name__)

_ANSI_RE = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]")
REPO_ROOT = Path(__file__).resolve().parents[3]


def render_hermes_config(
    harness: HarnessConfig,
    transcript_path: str,
    model: str,
    cutoff_iso: str,
) -> str:
    """生成评测专用的 ~/.hermes/config.yaml (写进每个 job 的隔离 HOME)。"""
    config_path = REPO_ROOT / harness.search_config_path
    return (
        "# breakthrough-eval 生成: 时间冻结证明考试的最小工具面\n"
        "model:\n"
        f"  default: \"{model}\"\n"
        "  provider: \"openrouter\"\n"
        "\n"
        "# 红线: 关掉全部内置 toolset (web/browser/terminal/file/memory...)。\n"
        "# 唯一信息源是下面的 restricted_search MCP。\n"
        "platform_toolsets:\n"
        "  cli: []\n"
        "\n"
        "mcp_servers:\n"
        "  restricted_search:\n"
        f"    command: {os.sys.executable}\n"
        f"    args: [\"-m\", \"{harness.search_mcp_module}\"]\n"
        "    env:\n"
        f"      RESTRICTED_SEARCH_CONFIG: \"{config_path}\"\n"
        f"      RESTRICTED_SEARCH_CUTOFF: \"{cutoff_iso}\"\n"
        f"      RESTRICTED_SEARCH_TRANSCRIPT: \"{transcript_path}\"\n"
        f"      PYTHONPATH: \"{REPO_ROOT}\"\n"
    )


class HermesProverBackend(ProverBackend):
    name = "hermes"

    def __init__(
        self,
        api: ApiConfig,
        harness: HarnessConfig,
        client: OpenAICompatibleClient | None = None,
    ):
        self.api = api
        self.harness = harness
        self.model = api.model
        self.scaffold_version = harness.scaffold_version
        self.probe_max_tokens = harness.probe_max_tokens
        self.timeout_seconds = harness.timeout_seconds
        self.hermes_bin = harness.binary or "hermes"
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
        return shutil.which(self.hermes_bin) is not None and self.client.available()

    # ------------------------------------------------------------------ #
    def run(self, ctx: ProverContext) -> BackendResponse:
        if ctx.phase == "probe":
            return probe_via_api(ctx, self.client, self.probe_max_tokens)
        if shutil.which(self.hermes_bin) is None:
            raise RuntimeError(
                "hermes CLI 不可用: 请先运行官方 install.sh (hermes-agent.nousresearch.com)。"
            )
        return self._prove(ctx)

    def _prove(self, ctx: ProverContext) -> BackendResponse:
        cutoff_iso = ctx.task.retrieval_cutoff.isoformat()
        with tempfile.TemporaryDirectory(prefix="be-hermes-") as tmp:
            home = Path(tmp)
            hermes_dir = home / ".hermes"
            hermes_dir.mkdir()
            transcript_path = home / "restricted_search_calls.jsonl"
            (hermes_dir / "config.yaml").write_text(
                render_hermes_config(self.harness, str(transcript_path), self.model, cutoff_iso),
                encoding="utf-8",
            )
            api_key = self.api.api_key
            (hermes_dir / ".env").write_text(
                f"OPENROUTER_API_KEY={api_key}\n", encoding="utf-8"
            )
            env = dict(os.environ)
            env["HOME"] = str(home)  # fresh agent: 记忆/技能/会话全部隔离
            env["OPENROUTER_API_KEY"] = api_key

            prompt = ctx.system + "\n\n" + ctx.user
            cmd = [
                self.hermes_bin, "-z", prompt,
                "--provider", "openrouter",
                "--model", self.model,
                "--ignore-rules",  # 不注入 AGENTS.md 等工作区规则, 保持考场干净
            ]
            log.debug("hermes 启动: HOME=%s model=%s", home, self.model)
            proc = subprocess.run(
                cmd, cwd=home, env=env, capture_output=True, text=True,
                timeout=self.timeout_seconds,
            )
            if proc.returncode != 0:
                raise RuntimeError(
                    f"hermes -z 退出码 {proc.returncode}: "
                    f"{(proc.stderr or proc.stdout)[-400:]}"
                )
            text = _ANSI_RE.sub("", proc.stdout).strip()
            tool_calls = read_mcp_transcript(transcript_path)
            log.info("hermes 完成: %d 字, MCP 检索 %d 次", len(text), len(tool_calls))
            return BackendResponse(text=text, tool_calls=tool_calls, usage=UsageStats())
