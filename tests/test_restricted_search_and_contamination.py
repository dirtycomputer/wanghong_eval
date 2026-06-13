"""Restricted search MCP + contamination audit."""

from datetime import date

from breakthrough_eval.specification import ContaminationProbe, ProbeKind, ToolCall
from breakthrough_eval.prover.contamination import audit_transcript, evaluate_probe
from breakthrough_eval.prover.tools.restricted_search import RESTRICTED_SEARCH_HOST
from breakthrough_eval.prover.tools.restricted_search.search import restricted_search

CUTOFF = date(2025, 1, 31)


def test_restricted_search_default_config_returns_only_cutoff_results():
    data = restricted_search("Kakeya polynomial partitioning", cutoff=CUTOFF)
    assert set(data) == {"results"}
    assert data["results"]
    assert all(item["published_date"] <= CUTOFF.isoformat() for item in data["results"])


def test_probe_detects_leak():
    probe = ContaminationProbe(
        id="p", kind=ProbeKind.DIRECT, prompt="?",
        leak_indicators=["convex set volume estimate", "grains 结构定理"],
    )
    leaked = evaluate_probe(probe, "关键在于 convex set volume estimate 的多尺度归纳。")
    clean = evaluate_probe(probe, "据我所知这是一个公开未解决的问题。")
    assert leaked.leaked and leaked.matched_indicators
    assert not clean.leaked


def test_audit_flags_out_of_window_network():
    calls = [
        ToolCall(tool="restricted_search", host=RESTRICTED_SEARCH_HOST),
        ToolCall(tool="web", host="google.com"),
    ]
    violations = audit_transcript(calls, {RESTRICTED_SEARCH_HOST}, CUTOFF)
    assert len(violations) == 1 and "google.com" in violations[0]
