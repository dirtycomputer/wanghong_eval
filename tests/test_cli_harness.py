"""CLI harness 后端 (opencode / hermes) + arxiv-frozen-mcp 协议 (plan §3.1/§3.2/§8)."""

import json
import os
import stat
import subprocess
import sys

from breakthrough_eval.arxiv_frozen import InMemoryArxivSource
from breakthrough_eval.llm_client import OpenRouterClient
from breakthrough_eval.models import Job
from breakthrough_eval.prover.base import ProverContext
from breakthrough_eval.prover.cli_common import read_mcp_transcript
from breakthrough_eval.prover.hermes import HermesProverBackend
from breakthrough_eval.prover.opencode import OpenCodeProverBackend, extract_text_from_events


def _ctx(task, phase, model="m"):
    return ProverContext(
        task=task,
        job=Job(task_id=task.task_id, model=model, hint_level=0, trial=0),
        phase=phase,
        system="sys",
        user="user prompt",
        source=InMemoryArxivSource(task.retrieval_cutoff),
    )


# --------------------------------------------------------------------------- #
# arxiv-frozen-mcp: stdio JSON-RPC + 红线过滤 + 审计 transcript
# --------------------------------------------------------------------------- #
def test_arxiv_mcp_protocol_and_cutoff(tmp_path):
    transcript = tmp_path / "calls.jsonl"
    env = {**os.environ, "CUTOFF_DATE": "2025-01-31",
           "ARXIV_MCP_TRANSCRIPT": str(transcript)}
    reqs = [
        {"jsonrpc": "2.0", "id": 1, "method": "initialize",
         "params": {"protocolVersion": "2025-06-18", "capabilities": {},
                    "clientInfo": {"name": "t", "version": "0"}}},
        {"jsonrpc": "2.0", "method": "notifications/initialized"},
        {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
        {"jsonrpc": "2.0", "id": 3, "method": "tools/call",
         "params": {"name": "search_arxiv",
                    "arguments": {"query": "kakeya convex volume", "max_results": 10}}},
    ]
    proc = subprocess.run(
        [sys.executable, "-m", "breakthrough_eval.arxiv_mcp"],
        input="\n".join(json.dumps(r) for r in reqs) + "\n",
        capture_output=True, text=True, env=env, timeout=30,
    )
    out = [json.loads(l) for l in proc.stdout.splitlines() if l.strip()]
    assert len(out) == 3  # notification 不回包
    assert out[0]["result"]["serverInfo"]["name"] == "arxiv-frozen-mcp"
    assert [t["name"] for t in out[1]["result"]["tools"]] == ["search_arxiv"]
    results = json.loads(out[2]["result"]["content"][0]["text"])["results"]
    assert results, "应返回 pre-cutoff 论文"
    assert all(r["submission_date"] <= "2025-01-31" for r in results)
    assert not any(r["arxiv_id"] == "2502.17655" for r in results)  # Wang–Zahl 必须被冻结
    # transcript 落盘且可回读为审计 ToolCall
    calls = read_mcp_transcript(transcript)
    assert len(calls) == 1 and calls[0].query == "kakeya convex volume"
    assert all(d.isoformat() <= "2025-01-31" for d in calls[0].returned_dates)


def test_arxiv_mcp_requires_cutoff_env():
    env = {k: v for k, v in os.environ.items() if k != "CUTOFF_DATE"}
    proc = subprocess.run(
        [sys.executable, "-m", "breakthrough_eval.arxiv_mcp"],
        input="", capture_output=True, text=True, env=env, timeout=30,
    )
    assert proc.returncode == 2
    assert "CUTOFF_DATE" in proc.stderr


# --------------------------------------------------------------------------- #
# 输出解析
# --------------------------------------------------------------------------- #
def test_extract_text_from_events_dedupes_part_updates():
    lines = [
        json.dumps({"type": "message.part.updated",
                    "properties": {"part": {"id": "p1", "type": "text", "text": "你好"}}}),
        json.dumps({"type": "message.part.updated",
                    "properties": {"part": {"id": "p1", "type": "text", "text": "你好, 世界"}}}),
        "not json at all",
    ]
    assert extract_text_from_events("\n".join(lines)) == "你好, 世界"


def test_extract_text_falls_back_to_raw():
    raw = "\x1b[32mplain\x1b[0m text answer"
    assert extract_text_from_events(raw) == "plain text answer"


# --------------------------------------------------------------------------- #
# 探针直连 (不碰 CLI)
# --------------------------------------------------------------------------- #
def _fake_client(reply="我不知道。"):
    def transport(url, payload, headers):
        return {"choices": [{"message": {"content": reply, "tool_calls": []}}],
                "usage": {"prompt_tokens": 7, "completion_tokens": 3}}
    return OpenRouterClient(model="x", api_key="k", transport=transport)


def test_probe_bypasses_cli_even_without_binary(task):
    for backend in (
        OpenCodeProverBackend(model="x", opencode_bin="definitely-not-installed",
                              client=_fake_client()),
        HermesProverBackend(model="x", hermes_bin="definitely-not-installed",
                            client=_fake_client()),
    ):
        resp = backend.run(_ctx(task, "probe"))
        assert resp.text == "我不知道。"
        assert resp.usage.input_tokens == 7 and resp.usage.output_tokens == 3


# --------------------------------------------------------------------------- #
# prove 阶段: 假 CLI 验证配置渲染 / 红线 / transcript 审计链路
# --------------------------------------------------------------------------- #
def _install_fake_bin(tmp_path, name, script, monkeypatch):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(exist_ok=True)
    p = bin_dir / name
    p.write_text(script, encoding="utf-8")
    p.chmod(p.stat().st_mode | stat.S_IEXEC)
    monkeypatch.setenv("PATH", f"{bin_dir}:{os.environ['PATH']}")
    return p


FAKE_OPENCODE = """#!/usr/bin/env python3
import json, os, sys
cfg = json.load(open("opencode.json"))           # 后端必须在 cwd 写好配置
assert cfg["tools"]["webfetch"] is False         # 红线: 禁 web
assert cfg["tools"]["bash"] is False
mcp = cfg["mcp"]["arxiv_frozen"]
assert mcp["command"] == ["arxiv-frozen-mcp"]
tr = mcp["environment"]["ARXIV_MCP_TRANSCRIPT"]
open(tr, "a").write(json.dumps({"tool": "search_arxiv", "query": "q1",
    "returned": [{"arxiv_id": "0807.2256", "submission_date": "2008-07-14"}]}) + "\\n")
assert "--model" in sys.argv and "openrouter/google/gemma-4-31b-it" in sys.argv
assert "--pure" in sys.argv                      # 红线+稳定性: 跳过外部插件
print(json.dumps({"type": "text", "text": "# Claimed Proof\\n## 1. 思路总览\\nfake"}))
"""


def test_opencode_prove_renders_config_and_audits(task, monkeypatch, tmp_path):
    _install_fake_bin(tmp_path, "opencode", FAKE_OPENCODE, monkeypatch)
    backend = OpenCodeProverBackend(model="google/gemma-4-31b-it", client=_fake_client())
    resp = backend.run(_ctx(task, "prove"))
    assert "Claimed Proof" in resp.text
    assert len(resp.tool_calls) == 1 and resp.tool_calls[0].query == "q1"
    assert resp.tool_calls[0].returned_dates[0].isoformat() == "2008-07-14"


FAKE_HERMES = """#!/usr/bin/env python3
import os, sys, json
home = os.environ["HOME"]
cfg = open(os.path.join(home, ".hermes", "config.yaml"), encoding="utf-8").read()
assert "platform_toolsets" in cfg and "cli: []" in cfg     # 红线: toolsets 清空
assert "arxiv-frozen-mcp" in cfg
assert "-z" in sys.argv and "--ignore-rules" in sys.argv
tr = [l.split(":", 1)[1].strip().strip('"') for l in cfg.splitlines()
      if "ARXIV_MCP_TRANSCRIPT" in l][0]
open(tr, "a").write(json.dumps({"tool": "search_arxiv", "query": "q2",
    "returned": [{"arxiv_id": "1011.4105", "submission_date": "2010-11-18"}]}) + "\\n")
print("\\x1b[1m# Claimed Proof\\x1b[0m via hermes")
"""


def test_hermes_prove_isolates_home_and_audits(task, monkeypatch, tmp_path):
    _install_fake_bin(tmp_path, "hermes", FAKE_HERMES, monkeypatch)
    backend = HermesProverBackend(model="google/gemma-4-31b-it", client=_fake_client())
    resp = backend.run(_ctx(task, "prove"))
    assert "# Claimed Proof via hermes" in resp.text  # ANSI 已剥
    assert len(resp.tool_calls) == 1 and resp.tool_calls[0].query == "q2"


def test_cli_backend_unavailable_raises_clearly(task):
    backend = OpenCodeProverBackend(model="x", opencode_bin="definitely-not-installed",
                                    client=_fake_client())
    try:
        backend.run(_ctx(task, "prove"))
    except RuntimeError as exc:
        assert "opencode" in str(exc)
    else:  # pragma: no cover
        raise AssertionError("expected RuntimeError")


FAKE_OPENCODE_BOOTHANG = """#!/usr/bin/env python3
import os, sys, time, json
marker = os.environ["HANG_MARKER"]
if not os.path.exists(marker):           # 第一次: 模拟启动死锁 (零输出地睡)
    open(marker, "w").write("hung once")
    time.sleep(60)
    sys.exit(1)
print(json.dumps({"type": "text", "text": "# Claimed Proof\\nrecovered"}))
"""


def test_opencode_boot_hang_detected_and_retried(task, monkeypatch, tmp_path):
    # 启动死锁 (零输出) 必须在 boot_timeout 内被看门狗识别并重试成功。
    _install_fake_bin(tmp_path, "opencode", FAKE_OPENCODE_BOOTHANG, monkeypatch)
    monkeypatch.setenv("HANG_MARKER", str(tmp_path / "hang.marker"))
    backend = OpenCodeProverBackend(
        model="google/gemma-4-31b-it", client=_fake_client(),
        boot_timeout_seconds=2, timeout_seconds=30, max_boot_retries=1,
    )
    import time
    t0 = time.time()
    resp = backend.run(_ctx(task, "prove"))
    assert "recovered" in resp.text
    assert time.time() - t0 < 25  # 死锁被 2s 看门狗掐掉, 而不是烧满 60s


def test_opencode_boot_hang_gives_up_after_retries(task, monkeypatch, tmp_path):
    _install_fake_bin(tmp_path, "opencode",
                      "#!/usr/bin/env python3\nimport time\ntime.sleep(60)\n", monkeypatch)
    backend = OpenCodeProverBackend(
        model="x", client=_fake_client(),
        boot_timeout_seconds=1, timeout_seconds=10, max_boot_retries=1,
    )
    try:
        backend.run(_ctx(task, "prove"))
    except RuntimeError as exc:
        assert "启动死锁" in str(exc)
    else:  # pragma: no cover
        raise AssertionError("expected RuntimeError")
