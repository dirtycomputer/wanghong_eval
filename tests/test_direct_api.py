"""OpenAI-compatible prover wiring, fully offline via an injected transport."""

import pytest

from breakthrough_eval.api_client import APIError, OpenAICompatibleClient
from breakthrough_eval.specification import ApiConfig, HarnessConfig, Job
from breakthrough_eval.prover.base import ProverContext
from breakthrough_eval.prover.backends.direct import DirectProverBackend


class FakeTransport:
    """Returns queued responses; optionally fails when tools are present."""

    def __init__(self, responses, fail_on_tools=False):
        self.responses = list(responses)
        self.calls = []
        self.fail_on_tools = fail_on_tools

    def __call__(self, url, payload, headers):
        self.calls.append(payload)
        if self.fail_on_tools and "tools" in payload:
            raise APIError("model does not support tools")
        return self.responses.pop(0)


def _msg(content="", tool_calls=None, pt=10, ct=5):
    return {
        "choices": [{"message": {"content": content, "tool_calls": tool_calls or []}}],
        "usage": {"prompt_tokens": pt, "completion_tokens": ct},
    }


def _client(transport):
    return OpenAICompatibleClient(model="x", api_key="k", transport=transport)


def _backend(transport):
    return DirectProverBackend(
        api=ApiConfig(base_url="https://example.test/v1", api_key="k", model="x"),
        harness=HarnessConfig(type="direct", scaffold_version="direct"),
        client=_client(transport),
    )


def test_client_requires_key(monkeypatch):
    c = OpenAICompatibleClient(model="x", api_key="")
    assert not c.available()
    with pytest.raises(APIError):
        c.chat([{"role": "user", "content": "hi"}])


def test_probe_phase_uses_yaml_temperature_policy(task):
    # temperature is optional because some OpenAI-compatible endpoints reject it.
    tr = FakeTransport([_msg(content="不知道"), _msg(content="proof")])
    backend = _backend(tr)
    from breakthrough_eval.specification import Job

    def ctx(phase):
        return ProverContext(
            task=task, job=Job(task_id=task.task_id, model="m", hint_level=0, trial=0),
            phase=phase, system="s", user="u",
        )

    backend.run(ctx("probe"))
    backend.run(ctx("prove"))
    assert "temperature" not in tr.calls[0]
    assert "temperature" not in tr.calls[1]


def test_client_parses_response():
    c = _client(FakeTransport([_msg(content="hello", pt=3, ct=4)]))
    res = c.chat([{"role": "user", "content": "hi"}])
    assert res.content == "hello"
    assert OpenAICompatibleClient.usage_tokens(res.usage) == (3, 4)


def test_direct_prover_never_sends_tools(task):
    transport = FakeTransport([_msg(content="# Claimed Proof\n## 1. 思路总览\nidea")])
    backend = _backend(transport)
    ctx = ProverContext(
        task=task,
        job=Job(task_id=task.task_id, model="x", hint_level=0, trial=0),
        phase="prove",
        system="sys",
        user="usr",
    )
    resp = backend.run(ctx)
    assert "Claimed Proof" in resp.text
    assert resp.tool_calls == []
    assert "tools" not in transport.calls[0]
    assert resp.usage.input_tokens > 0


def test_direct_prover_ignores_tool_rejection_transport_flag(task):
    transport = FakeTransport([_msg(content="final answer")], fail_on_tools=True)
    backend = _backend(transport)
    ctx = ProverContext(
        task=task,
        job=Job(task_id=task.task_id, model="x", hint_level=0, trial=0),
        phase="prove",
        system="sys",
        user="usr",
    )
    resp = backend.run(ctx)
    assert resp.text == "final answer"
