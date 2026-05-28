"""Codex backend renders the plan §3.2 config and degrades gracefully."""

from breakthrough_eval.prover.codex import (
    CodexProverBackend,
    CodexProverConfig,
    render_config_toml,
)


def test_config_toml_matches_plan():
    toml = render_config_toml(CodexProverConfig(model="m", model_provider="local-vllm"), "2025-01-31")
    assert 'web_search = "disabled"' in toml  # red line
    assert "[mcp_servers.arxiv_frozen]" in toml
    assert 'CUTOFF_DATE = "2025-01-31"' in toml
    assert "required = true" in toml


def test_unavailable_backend_raises_clearly(task):
    backend = CodexProverBackend()
    if backend.available():
        return  # codex actually installed; skip
    from breakthrough_eval.arxiv_frozen import InMemoryArxivSource
    from breakthrough_eval.models import Job
    from breakthrough_eval.prover.base import ProverContext

    ctx = ProverContext(
        task=task,
        job=Job(task_id=task.task_id, model="m", hint_level=0, trial=0),
        phase="prove",
        system="s",
        user="u",
        source=InMemoryArxivSource(task.retrieval_cutoff),
    )
    try:
        backend.run(ctx)
    except RuntimeError as exc:
        assert "codex" in str(exc).lower()
    else:  # pragma: no cover
        raise AssertionError("expected RuntimeError when codex missing")


def test_parse_json_transcript():
    stdout = "\n".join(
        [
            '{"type": "function_call", "name": "arxiv_frozen.search", "arguments": "kakeya"}',
            '{"type": "agent_message", "text": "final proof", "output_tokens": 12}',
        ]
    )
    resp = CodexProverBackend._parse(stdout, "")
    assert resp.text == "final proof"
    assert resp.tool_calls and resp.tool_calls[0].tool == "arxiv_frozen.search"
    assert resp.usage.output_tokens == 12
