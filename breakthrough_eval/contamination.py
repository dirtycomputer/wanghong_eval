"""Contamination control logic (plan §8) — the make-or-break layer.

Two of the four defence lines live here as reusable functions:

  * probe battery evaluation (defence line 3): does an *unhinted* response
    already spit out the breakthrough's key innovation?
  * post-hoc audit (defence line 4): are all cited references <= cutoff, and was
    there any out-of-window network call in the transcript?

(Line 1 = model cutoff, enforced by ``ModelEntry.eligible_for`` /
``Controller``; line 2 = frozen retrieval, enforced inside ``ArxivFrozenSource``.)
"""

from __future__ import annotations

import re
from datetime import date

from .arxiv_frozen import ArxivFrozenSource
from .models import (
    AuditResult,
    ContaminationProbe,
    ProbeResponse,
    ToolCall,
)


def _match_indicators(text: str, indicators: list[str]) -> list[str]:
    """Case-insensitive, word-ish containment match. Returns the matched ones."""
    low = text.lower()
    hits: list[str] = []
    for ind in indicators:
        if ind.lower() in low:
            hits.append(ind)
    return hits


def evaluate_probe(probe: ContaminationProbe, response_text: str) -> ProbeResponse:
    """Decide whether a single probe response leaks the key innovation (plan §8.3)."""
    matched = _match_indicators(response_text, probe.leak_indicators)
    leaked = len(matched) >= probe.threshold
    return ProbeResponse(
        probe_id=probe.id,
        kind=probe.kind,
        response_text=response_text,
        matched_indicators=matched,
        leaked=leaked,
    )


def any_leaked(probe_responses: list[ProbeResponse]) -> bool:
    """任一探针命中关键创新 → 判污染 (plan §8.3)."""
    return any(p.leaked for p in probe_responses)


# --------------------------------------------------------------------------- #
# Post-hoc audit (plan §8.4)
# --------------------------------------------------------------------------- #
_ARXIV_RE = re.compile(r"arxiv[:\s]*([0-9]{4}\.[0-9]{4,5}|[a-z\-]+/[0-9]{7})", re.I)


def _extract_arxiv_ids(reference: str) -> list[str]:
    return [m.group(1) for m in _ARXIV_RE.finditer(reference)]


def audit_references(
    cited_references: list[str],
    source: ArxivFrozenSource,
    cutoff: date,
) -> list[str]:
    """Return references that resolve to a paper submitted after the cutoff.

    A reference is flagged if it names an arXiv id whose true submission date is
    > cutoff (i.e. the model somehow cited post-T material). References that do
    not resolve in the corpus are left alone (can't prove they're out of window).
    """
    out_of_window: list[str] = []
    for ref in cited_references:
        for arxiv_id in _extract_arxiv_ids(ref):
            paper = _resolve_unfiltered(source, arxiv_id)
            if paper is not None and paper.submission_date > cutoff:
                out_of_window.append(ref)
                break
    return out_of_window


def _resolve_unfiltered(source: ArxivFrozenSource, arxiv_id: str):
    """Look a paper up ignoring the cutoff filter (auditors may see everything)."""
    try:
        for p in source._corpus():  # noqa: SLF001 - auditor needs full view
            if p.arxiv_id == arxiv_id:
                return p
    except NotImplementedError:
        return None
    return None


def audit_transcript(
    transcript: list[ToolCall],
    allowed_hosts: set[str],
    cutoff: date,
) -> list[str]:
    """Return descriptions of any out-of-window network access (plan §3.2, §8.4)."""
    violations: list[str] = []
    for call in transcript:
        if call.host and call.host not in allowed_hosts:
            violations.append(f"{call.tool} -> {call.host} (host not whitelisted)")
        if any(d > cutoff for d in call.returned_dates):
            violations.append(
                f"{call.tool} returned post-cutoff material for query '{call.query}'"
            )
    return violations


def audit_run(
    cited_references: list[str],
    transcript: list[ToolCall],
    source: ArxivFrozenSource,
    cutoff: date,
    allowed_hosts: set[str],
) -> AuditResult:
    return AuditResult(
        out_of_window_references=audit_references(cited_references, source, cutoff),
        out_of_window_network=audit_transcript(transcript, allowed_hosts, cutoff),
    )
