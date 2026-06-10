"""OpenRouter prover/judge wiring — fully offline via an injected transport."""

import json

import pytest

from breakthrough_eval.arxiv_frozen import InMemoryArxivSource
from breakthrough_eval.eval.openrouter_judge import openrouter_judge
from breakthrough_eval.llm_client import LLMError, OpenRouterClient
from breakthrough_eval.models import Job
from breakthrough_eval.prover.base import ProverContext
from breakthrough_eval.prover.openrouter import OpenRouterProverBackend


class FakeTransport:
    """Returns queued responses; optionally fails when tools are present."""

    def __init__(self, responses, fail_on_tools=False):
        self.responses = list(responses)
        self.calls = []
        self.fail_on_tools = fail_on_tools

    def __call__(self, url, payload, headers):
        self.calls.append(payload)
        if self.fail_on_tools and "tools" in payload:
            raise LLMError("model does not support tools")
        return self.responses.pop(0)


def _msg(content="", tool_calls=None, pt=10, ct=5):
    return {
        "choices": [{"message": {"content": content, "tool_calls": tool_calls or []}}],
        "usage": {"prompt_tokens": pt, "completion_tokens": ct},
    }


def _client(transport):
    return OpenRouterClient(model="x", api_key="k", transport=transport)


def test_client_requires_key(monkeypatch):
    # api_key=None 会回退读环境变量; 删掉它, 测试才与运行环境无关。
    monkeypatch.delenv("OPENROUTER_KEY", raising=False)
    c = OpenRouterClient(model="x", api_key=None)
    assert not c.available()
    with pytest.raises(LLMError):
        c.chat([{"role": "user", "content": "hi"}])


def test_client_parses_response():
    c = _client(FakeTransport([_msg(content="hello", pt=3, ct=4)]))
    res = c.chat([{"role": "user", "content": "hi"}])
    assert res.content == "hello"
    assert OpenRouterClient.usage_tokens(res.usage) == (3, 4)


def test_prover_tool_loop_records_frozen_dates(task):
    tool_call = {
        "id": "c1",
        "type": "function",
        "function": {"name": "search_arxiv", "arguments": json.dumps({"query": "kakeya"})},
    }
    transport = FakeTransport(
        [
            _msg(content="", tool_calls=[tool_call]),  # ask for retrieval
            _msg(content="# Claimed Proof\n## 1. 思路总览\nidea"),  # final
        ]
    )
    backend = OpenRouterProverBackend(model="x", client=_client(transport))
    ctx = ProverContext(
        task=task,
        job=Job(task_id=task.task_id, model="x", hint_level=0, trial=0),
        phase="prove",
        system="sys",
        user="usr",
        source=InMemoryArxivSource(task.retrieval_cutoff),
    )
    resp = backend.run(ctx)
    assert "Claimed Proof" in resp.text
    assert resp.tool_calls and resp.tool_calls[0].tool == "search_arxiv"
    # frozen guarantee: every returned date is within the cutoff
    for tc in resp.tool_calls:
        assert all(d <= task.retrieval_cutoff for d in tc.returned_dates)
    assert resp.usage.input_tokens > 0


def test_prover_falls_back_when_tools_rejected(task):
    transport = FakeTransport([_msg(content="final answer")], fail_on_tools=True)
    backend = OpenRouterProverBackend(model="x", client=_client(transport))
    ctx = ProverContext(
        task=task,
        job=Job(task_id=task.task_id, model="x", hint_level=0, trial=0),
        phase="prove",
        system="sys",
        user="usr",
        source=InMemoryArxivSource(task.retrieval_cutoff),
    )
    resp = backend.run(ctx)
    assert resp.text == "final answer"


def test_openrouter_judge_parses_json(task):
    payload = {
        "item_verdicts": [
            {"item_id": r.id, "passed": True, "cited_lines": [1], "confidence": 0.9}
            for r in task.rubric
        ],
        "overall_valid": True,
        "alternative_valid": False,
        "notes": "ok",
    }
    judge = openrouter_judge("deepseek/deepseek-v4-pro")
    # swap in a fake transport on the judge's underlying client
    judge._complete.client._transport = FakeTransport(  # type: ignore[attr-defined]
        [_msg(content=json.dumps(payload))]
    )
    judge._complete.client.api_key = "k"  # type: ignore[attr-defined]
    verdict = judge.judge(task, "proof text", task.golden_proof)
    assert verdict.overall_valid
    assert len(verdict.item_verdicts) == len(task.rubric)
