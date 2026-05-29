"""Frozen retrieval + contamination control (plan §8, defence lines 2-4)."""

from datetime import date

from breakthrough_eval.arxiv_frozen import InMemoryArxivSource
from breakthrough_eval.contamination import (
    audit_references,
    audit_transcript,
    evaluate_probe,
)
from breakthrough_eval.models import ContaminationProbe, ProbeKind, ToolCall

CUTOFF = date(2025, 1, 31)


def test_frozen_source_hides_post_cutoff_paper():
    src = InMemoryArxivSource(CUTOFF)
    hits = src.search("kakeya convex volume", max_results=20)
    ids = {p.arxiv_id for p in hits}
    assert "2502.17655" not in ids  # Wang–Zahl must be hidden
    assert all(p.submission_date <= CUTOFF for p in hits)


def test_frozen_source_returns_legal_pre_t_tools():
    src = InMemoryArxivSource(CUTOFF)
    hits = src.search("polynomial method incidence kakeya")
    assert hits  # Guth/Katz etc. are available


def test_post_cutoff_source_would_expose_wang_zahl():
    # A deliberately leaky source (cutoff = breakthrough date) returns it.
    src = InMemoryArxivSource(date(2025, 2, 25))
    hits = src.search("convex set volume kakeya")
    assert any(p.arxiv_id == "2502.17655" for p in hits)


def test_probe_detects_leak():
    probe = ContaminationProbe(
        id="p", kind=ProbeKind.DIRECT, prompt="?",
        leak_indicators=["convex set volume estimate", "grains 结构定理"],
    )
    leaked = evaluate_probe(probe, "关键在于 convex set volume estimate 的多尺度归纳。")
    clean = evaluate_probe(probe, "据我所知这是一个公开未解决的问题。")
    assert leaked.leaked and leaked.matched_indicators
    assert not clean.leaked


def test_audit_flags_post_cutoff_reference():
    src = InMemoryArxivSource(CUTOFF)
    refs = ["arXiv:2502.17655 (Wang, Zahl)", "arXiv:1409.1885 (Guth)"]
    flagged = audit_references(refs, src, CUTOFF)
    assert flagged == ["arXiv:2502.17655 (Wang, Zahl)"]


def test_audit_flags_out_of_window_network():
    calls = [
        ToolCall(tool="arxiv", host="arxiv-frozen-mcp.internal"),
        ToolCall(tool="web", host="google.com"),
    ]
    violations = audit_transcript(calls, {"arxiv-frozen-mcp.internal"}, CUTOFF)
    assert len(violations) == 1 and "google.com" in violations[0]
