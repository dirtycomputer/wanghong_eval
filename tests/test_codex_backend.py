"""Codex backend renders the plan §3.2 config and degrades gracefully."""

from breakthrough_eval.prover.backends.codex import (
    CodexProverBackend,
    CodexProverConfig,
    render_auth_json,
    render_config_toml,
)
from breakthrough_eval.specification import ApiConfig, HarnessConfig


def _config():
    return CodexProverConfig(
        api=ApiConfig(
            base_url="https://example.test/v1",
            api_key="sk-test",
            model="m",
        ),
        harness=HarnessConfig(type="codex", scaffold_version="codex"),
    )


def test_config_toml_matches_plan():
    toml = render_config_toml(_config(), "2025-01-31")
    auth = render_auth_json(_config())
    assert 'model = "m"' in toml
    assert 'review_model = "m"' in toml
    assert 'model_provider = "OpenAI"' in toml
    assert 'base_url = "https://example.test/v1"' in toml
    assert 'wire_api = "responses"' in toml
    assert "requires_openai_auth = true" in toml
    assert "sk-test" not in toml
    assert '"OPENAI_API_KEY": "sk-test"' in auth
    assert "sk-" not in toml
    assert "[mcp_servers.restricted_search]" in toml
    assert "breakthrough_eval.prover.tools.restricted_search.mcp" in toml
    assert "RESTRICTED_SEARCH_CONFIG" in toml
    assert "2025-01-31" not in toml
    assert 'env_vars = ["RESTRICTED_SEARCH_CUTOFF"]' in toml
    assert "required = true" in toml
    assert 'default_tools_approval_mode = "approve"' in toml
    assert 'enabled_tools = ["restricted_search"]' in toml


def test_unavailable_backend_raises_clearly(task):
    cfg = _config()
    backend = CodexProverBackend(cfg.api, cfg.harness)
    if backend.available():
        return  # codex actually installed; skip
    from breakthrough_eval.specification import Job
    from breakthrough_eval.prover.base import ProverContext

    ctx = ProverContext(
        task=task,
        job=Job(task_id=task.task_id, model="m", hint_level=0, trial=0),
        phase="prove",
        system="s",
        user="u",
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
            '{"type": "function_call", "name": "restricted_search", "arguments": "kakeya"}',
            '{"type": "agent_message", "text": "final proof", "output_tokens": 12}',
        ]
    )
    resp = CodexProverBackend._parse(stdout, "")
    assert resp.text == "final proof"
    assert resp.tool_calls and resp.tool_calls[0].tool == "restricted_search"
    assert resp.usage.output_tokens == 12


def test_parse_current_codex_message_shape():
    stdout = "\n".join(
        [
            '{"type":"item.completed","item":{"type":"message","role":"assistant","content":[{"type":"output_text","text":"new final"}]}}',
            '{"type":"token_count","info":{"total_token_usage":{"input_tokens":3,"output_tokens":4}}}',
        ]
    )
    resp = CodexProverBackend._parse(stdout, "")
    assert resp.text == "new final"
    assert resp.usage.input_tokens == 3
    assert resp.usage.output_tokens == 4
