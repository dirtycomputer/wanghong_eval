"""OpenCode CLI PROVER backend (plan §3.1 的可插拔 harness 之一).

把 ``opencode run`` 作为 agent loop。沙箱面收口 (plan §8 红线):

  * ``tools``: 一律禁用 webfetch / websearch / bash / edit / write / patch —— 联网与
    改文件都不该出现在一场「时间冻结的证明考试」里;
  * 唯一外部信息源是我们的 ``arxiv-frozen-mcp`` (CUTOFF_DATE 服务端硬过滤,
    每次调用写 JSONL transcript, 事后 §8.4 审计);
  * 探针阶段绕过 CLI 直连裸模型 (见 ``cli_common.probe_via_api``)。

每个 job 在独立 tempdir 里跑: 自带 opencode.json, 并用 XDG_* 把会话/全局配置也
隔离进去, 避免跨 trial 状态泄漏。
"""

from __future__ import annotations

import json
import logging
import os
import re
import shutil
import subprocess
import tempfile
import threading
from pathlib import Path

from ..llm_client import OpenRouterClient
from ..models import UsageStats
from .base import BackendResponse, ProverBackend, ProverContext
from .cli_common import probe_via_api, read_mcp_transcript

log = logging.getLogger(__name__)

_ANSI_RE = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]")

# 评测红线: 禁用一切联网/落盘类内置工具 (tools.<name>=false 即从工具列表移除)。
DISABLED_TOOLS = ["webfetch", "websearch", "bash", "edit", "write", "patch"]


def render_opencode_config(cutoff_iso: str, transcript_path: str) -> dict:
    return {
        "$schema": "https://opencode.ai/config.json",
        "share": "disabled",
        "autoupdate": False,
        "mcp": {
            "arxiv_frozen": {
                "type": "local",
                "command": ["arxiv-frozen-mcp"],
                "environment": {
                    "CUTOFF_DATE": cutoff_iso,
                    "ARXIV_MCP_TRANSCRIPT": transcript_path,
                    "ARXIV_MCP_SOURCE": "arxiv",  # 真实 harness 用真实冻结 arXiv
                },
                "enabled": True,
            }
        },
        "tools": {name: False for name in DISABLED_TOOLS},
    }


def extract_text_from_events(stdout: str) -> str:
    """从 ``--format json`` 的事件流里抽出助手文本 (容错: 解析不出就退回原文)。

    事件形态随版本演化, 这里只依赖一个稳定不变量: 文本部件是
    ``{"type": "text", "text": ...}`` 形状的对象 (可能嵌套、可能按 part 重复
    更新)。同一 part 取最后一次的完整文本。
    """
    parts: dict[str, str] = {}
    order: list[str] = []

    def walk(node, fallback_key: str):
        if isinstance(node, dict):
            if node.get("type") == "text" and isinstance(node.get("text"), str):
                key = str(node.get("id") or fallback_key)
                if key not in parts:
                    order.append(key)
                parts[key] = node["text"]
            for k, v in node.items():
                walk(v, fallback_key + "." + str(k))
        elif isinstance(node, list):
            for i, v in enumerate(node):
                walk(v, f"{fallback_key}[{i}]")

    for i, line in enumerate(stdout.splitlines()):
        line = line.strip()
        if not line or not line.startswith(("{", "[")):
            continue
        try:
            walk(json.loads(line), f"line{i}")
        except json.JSONDecodeError:
            continue
    if parts:
        return "\n".join(parts[k] for k in order).strip()
    return _ANSI_RE.sub("", stdout).strip()


class _BootHang(RuntimeError):
    """opencode 偶发启动死锁: 进程存活但永远不产出任何输出 (实测呈波次出现)。"""


class OpenCodeProverBackend(ProverBackend):
    name = "opencode"

    def __init__(
        self,
        model: str,
        scaffold_version: str = "opencode",
        probe_max_tokens: int = 400,
        timeout_seconds: int = 1800,
        boot_timeout_seconds: int = 90,
        max_boot_retries: int = 2,
        opencode_bin: str = "opencode",
        client: OpenRouterClient | None = None,
    ):
        self.model = model
        self.scaffold_version = scaffold_version
        self.probe_max_tokens = probe_max_tokens
        self.timeout_seconds = timeout_seconds
        # 健康启动几秒内就有 stdout banner; 超过 boot_timeout 零输出 = 启动死锁,
        # 杀掉重试 (把 15min 超时烧损降到 90s 检测)。
        self.boot_timeout_seconds = boot_timeout_seconds
        self.max_boot_retries = max_boot_retries
        self.opencode_bin = opencode_bin
        # 探针直连用; CLI 自己的推理走它内置的 openrouter provider。
        self.client = client or OpenRouterClient(model=model, temperature=0.3)

    def available(self) -> bool:
        return shutil.which(self.opencode_bin) is not None and self.client.available()

    # ------------------------------------------------------------------ #
    def run(self, ctx: ProverContext) -> BackendResponse:
        if ctx.phase == "probe":
            # 污染是模型属性: 无援、无 harness、短回答 (plan §3.3)。
            return probe_via_api(ctx, self.client, self.probe_max_tokens)
        if shutil.which(self.opencode_bin) is None:
            raise RuntimeError(
                "opencode CLI 不可用: 请先 `npm i -g opencode-ai` (或改用其它 provider)。"
            )
        return self._prove(ctx)

    def _exec_with_watchdog(self, cmd: list[str], cwd: Path, env: dict) -> str:
        """运行 opencode, 带启动看门狗。返回 stdout; 启动死锁抛 _BootHang。"""
        proc = subprocess.Popen(
            cmd, cwd=cwd, env=env, text=True,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        )
        out_buf: list[str] = []
        err_buf: list[str] = []
        first_output = threading.Event()

        def pump(stream, buf):
            for line in stream:
                buf.append(line)
                first_output.set()

        threads = [
            threading.Thread(target=pump, args=(proc.stdout, out_buf), daemon=True),
            threading.Thread(target=pump, args=(proc.stderr, err_buf), daemon=True),
        ]
        for t in threads:
            t.start()
        try:
            if not first_output.wait(self.boot_timeout_seconds):
                raise _BootHang(
                    f"opencode {self.boot_timeout_seconds}s 内零输出 (启动死锁)"
                )
            rc = proc.wait(timeout=self.timeout_seconds)
        except (_BootHang, subprocess.TimeoutExpired):
            proc.kill()
            proc.wait()
            raise
        finally:
            for t in threads:
                t.join(timeout=5)
        if rc != 0:
            raise RuntimeError(
                f"opencode run 退出码 {rc}: {(''.join(err_buf) or ''.join(out_buf))[-400:]}"
            )
        return "".join(out_buf)

    def _prove(self, ctx: ProverContext) -> BackendResponse:
        cutoff_iso = ctx.task.retrieval_cutoff.isoformat()
        with tempfile.TemporaryDirectory(prefix="be-opencode-") as tmp:
            work = Path(tmp)
            transcript_path = work / "arxiv_calls.jsonl"
            (work / "opencode.json").write_text(
                json.dumps(render_opencode_config(cutoff_iso, str(transcript_path)),
                           ensure_ascii=False, indent=1),
                encoding="utf-8",
            )
            env = dict(os.environ)
            env["OPENROUTER_API_KEY"] = env.get("OPENROUTER_API_KEY") or env.get("OPENROUTER_KEY", "")
            # 会话/全局状态全部隔离进 tempdir (fresh harness per job)。
            env["XDG_DATA_HOME"] = str(work / "xdg-data")
            env["XDG_CONFIG_HOME"] = str(work / "xdg-config")
            env["XDG_STATE_HOME"] = str(work / "xdg-state")
            env["XDG_CACHE_HOME"] = str(work / "xdg-cache")

            prompt = ctx.system + "\n\n" + ctx.user
            cmd = [
                self.opencode_bin, "run",
                "--pure",  # 跳过外部插件: 其启动期无超时拉取会经代理间歇性死锁 (实测根因)
                "--model", f"openrouter/{self.model}",
                "--format", "json",
                prompt,
            ]
            stdout = ""
            for attempt in range(self.max_boot_retries + 1):
                log.debug("opencode 启动 (attempt %d): cwd=%s", attempt + 1, work)
                try:
                    stdout = self._exec_with_watchdog(cmd, work, env)
                    break
                except _BootHang as exc:
                    if attempt >= self.max_boot_retries:
                        raise RuntimeError(
                            f"opencode 启动死锁, 重试 {self.max_boot_retries} 次仍失败: {exc}"
                        ) from exc
                    log.warning("job %s: %s, 重试 (%d/%d)", ctx.job.job_id, exc,
                                attempt + 1, self.max_boot_retries)
            text = extract_text_from_events(stdout)
            tool_calls = read_mcp_transcript(transcript_path)
            log.info("opencode 完成: %d 字, MCP 检索 %d 次", len(text), len(tool_calls))
            return BackendResponse(text=text, tool_calls=tool_calls, usage=UsageStats())


def _factory(**kwargs) -> OpenCodeProverBackend:
    if "model" not in kwargs:
        raise KeyError("opencode prover 需要 backend_kwargs.model (e.g. google/gemma-4-31b-it)")
    return OpenCodeProverBackend(
        model=kwargs["model"],
        scaffold_version=kwargs.get("scaffold_version", "opencode"),
        probe_max_tokens=kwargs.get("probe_max_tokens", 400),
        timeout_seconds=kwargs.get("timeout_seconds", 1800),
        opencode_bin=kwargs.get("opencode_bin", "opencode"),
    )
