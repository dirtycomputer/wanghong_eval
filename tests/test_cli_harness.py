"""CLI harness 后端 + restricted_search MCP."""

import json
import os
import stat
import subprocess
import sys

from breakthrough_eval.api_client import OpenAICompatibleClient
from breakthrough_eval.specification import ApiConfig, HarnessConfig, Job
from breakthrough_eval.prover.base import ProverContext
from breakthrough_eval.prover.backends.cli_utils import read_mcp_transcript
from breakthrough_eval.prover.backends.hermes import HermesProverBackend
from breakthrough_eval.prover.backends.opencode import OpenCodeProverBackend, extract_text_from_events


def _ctx(task, phase, model="m"):
    return ProverContext(
        task=task,
        job=Job(task_id=task.task_id, model=model, hint_level=0, trial=0),
        phase=phase,
        system="sys",
        user="user prompt",
    )


# --------------------------------------------------------------------------- #
# restricted_search MCP: stdio JSON-RPC + 红线过滤 + 审计 transcript
# --------------------------------------------------------------------------- #
def test_restricted_search_mcp_protocol_and_cutoff(tmp_path):
    transcript = tmp_path / "calls.jsonl"
    env = {
        **os.environ,
        "RESTRICTED_SEARCH_CUTOFF": "2025-01-31",
        "RESTRICTED_SEARCH_TRANSCRIPT": str(transcript),
    }
    reqs = [
        {"jsonrpc": "2.0", "id": 1, "method": "initialize",
         "params": {"protocolVersion": "1.0", "capabilities": {},
                    "clientInfo": {"name": "t", "version": "0"}}},
        {"jsonrpc": "2.0", "method": "notifications/initialized"},
        {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
        {"jsonrpc": "2.0", "id": 3, "method": "tools/call",
         "params": {"name": "restricted_search",
                    "arguments": {"query": "kakeya convex volume"}}},
    ]
    proc = subprocess.run(
        [sys.executable, "-m", "breakthrough_eval.prover.tools.restricted_search.mcp"],
        input="\n".join(json.dumps(r) for r in reqs) + "\n",
        capture_output=True, text=True, env=env, timeout=30,
    )
    out = [json.loads(l) for l in proc.stdout.splitlines() if l.strip()]
    assert len(out) == 3  # notification 不回包
    assert out[0]["result"]["serverInfo"]["name"] == "restricted-search-mcp"
    assert [t["name"] for t in out[1]["result"]["tools"]] == ["restricted_search"]
    results = json.loads(out[2]["result"]["content"][0]["text"])["results"]
    assert results
    assert all(r["published_date"] <= "2025-01-31" for r in results)
    # transcript 落盘且可回读为审计 ToolCall
    calls = read_mcp_transcript(transcript)
    assert len(calls) == 1 and calls[0].query == "kakeya convex volume"
    assert all(d.isoformat() <= "2025-01-31" for d in calls[0].returned_dates)


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
    return OpenAICompatibleClient(model="x", api_key="k", transport=transport)


def _api():
    return ApiConfig(
        base_url="https://openrouter.ai/api/v1",
        api_key="sk-test",
        model="google/gemma-4-31b-it",
    )


def test_probe_bypasses_cli_even_without_binary(task):
    for backend in (
        OpenCodeProverBackend(
            api=_api(),
            harness=HarnessConfig(type="opencode", binary="definitely-not-installed"),
            client=_fake_client(),
        ),
        HermesProverBackend(
            api=_api(),
            harness=HarnessConfig(type="hermes", binary="definitely-not-installed"),
            client=_fake_client(),
        ),
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
mcp = cfg["mcp"]["restricted_search"]
assert "breakthrough_eval.prover.tools.restricted_search.mcp" in mcp["command"]
assert mcp["environment"]["RESTRICTED_SEARCH_CUTOFF"] == "2025-01-31"
tr = mcp["environment"]["RESTRICTED_SEARCH_TRANSCRIPT"]
open(tr, "a").write(json.dumps({"tool": "restricted_search", "query": "q1",
    "returned": [{"arxiv_id": "0807.2256", "published_date": "2008-07-14"}]}) + "\\n")
assert "--model" in sys.argv and "openrouter/google/gemma-4-31b-it" in sys.argv
print(json.dumps({"type": "text", "text": "# Claimed Proof\\n## 1. 思路总览\\nfake"}))
"""


def test_opencode_prove_renders_config_and_audits(task, monkeypatch, tmp_path):
    _install_fake_bin(tmp_path, "opencode", FAKE_OPENCODE, monkeypatch)
    backend = OpenCodeProverBackend(
        api=_api(),
        harness=HarnessConfig(type="opencode", scaffold_version="opencode"),
        client=_fake_client(),
    )
    resp = backend.run(_ctx(task, "prove"))
    assert "Claimed Proof" in resp.text
    assert len(resp.tool_calls) == 1 and resp.tool_calls[0].query == "q1"
    assert resp.tool_calls[0].returned_dates[0].isoformat() == "2008-07-14"


FAKE_HERMES = """#!/usr/bin/env python3
import os, sys, json
home = os.environ["HOME"]
cfg = open(os.path.join(home, ".hermes", "config.yaml"), encoding="utf-8").read()
assert "platform_toolsets" in cfg and "cli: []" in cfg     # 红线: toolsets 清空
assert "restricted_search" in cfg
assert "RESTRICTED_SEARCH_CUTOFF" in cfg
assert "-z" in sys.argv and "--ignore-rules" in sys.argv
tr = [l.split(":", 1)[1].strip().strip('"') for l in cfg.splitlines()
      if "RESTRICTED_SEARCH_TRANSCRIPT" in l][0]
open(tr, "a").write(json.dumps({"tool": "restricted_search", "query": "q2",
    "returned": [{"arxiv_id": "1011.4105", "published_date": "2010-11-18"}]}) + "\\n")
print("\\x1b[1m# Claimed Proof\\x1b[0m via hermes")
"""


def test_hermes_prove_isolates_home_and_audits(task, monkeypatch, tmp_path):
    _install_fake_bin(tmp_path, "hermes", FAKE_HERMES, monkeypatch)
    backend = HermesProverBackend(
        api=_api(),
        harness=HarnessConfig(type="hermes", scaffold_version="hermes"),
        client=_fake_client(),
    )
    resp = backend.run(_ctx(task, "prove"))
    assert "# Claimed Proof via hermes" in resp.text  # ANSI 已剥
    assert len(resp.tool_calls) == 1 and resp.tool_calls[0].query == "q2"


def test_cli_backend_unavailable_raises_clearly(task):
    backend = OpenCodeProverBackend(
        api=_api(),
        harness=HarnessConfig(type="opencode", binary="definitely-not-installed"),
        client=_fake_client(),
    )
    try:
        backend.run(_ctx(task, "prove"))
    except RuntimeError as exc:
        assert "opencode" in str(exc)
    else:  # pragma: no cover
        raise AssertionError("expected RuntimeError")
