"""Time-frozen arXiv retrieval — PROVER 唯一的外部信息源 (plan §3.2, §8.2).

In production this is a self-hosted MCP server that hard-filters every result by
``submission_date <= CUTOFF_DATE``. Here we provide:

  * ``ArxivFrozenSource`` — the abstract interface,
  * ``InMemoryArxivSource`` — a deterministic mock corpus used by tests and the
    mock prover. It enforces the same cutoff filter so the contamination
    guarantee is testable end-to-end.

The cutoff is enforced *inside* the source (defence in depth): even if a caller
asks for everything, post-cutoff papers are never returned.
"""

from __future__ import annotations

from datetime import date
from typing import Optional

from pydantic import BaseModel, Field


class ArxivPaper(BaseModel):
    arxiv_id: str
    title: str
    authors: list[str] = Field(default_factory=list)
    submission_date: date
    abstract: str = ""

    @property
    def citation(self) -> str:
        who = ", ".join(self.authors[:2]) + (" et al." if len(self.authors) > 2 else "")
        return f"arXiv:{self.arxiv_id} ({who})"


class ArxivFrozenSource:
    """Abstract frozen-arXiv source. Subclasses implement ``_all_matching``."""

    def __init__(self, cutoff: date):
        self.cutoff = cutoff

    def search(self, query: str, max_results: int = 10) -> list[ArxivPaper]:
        """Return papers matching ``query`` with submission_date <= cutoff."""
        hits = [
            p
            for p in self._all_matching(query)
            if p.submission_date <= self.cutoff  # hard cutoff filter (red line)
        ]
        return hits[:max_results]

    def get(self, arxiv_id: str) -> Optional[ArxivPaper]:
        for p in self._corpus():
            if p.arxiv_id == arxiv_id and p.submission_date <= self.cutoff:
                return p
        return None

    # Subclass hooks ----------------------------------------------------- #
    def _corpus(self) -> list[ArxivPaper]:  # pragma: no cover - abstract
        raise NotImplementedError

    def _all_matching(self, query: str) -> list[ArxivPaper]:
        q = query.lower()
        terms = [t for t in q.replace(",", " ").split() if t]
        scored: list[tuple[int, ArxivPaper]] = []
        for p in self._corpus():
            hay = f"{p.title} {p.abstract} {' '.join(p.authors)}".lower()
            score = sum(1 for t in terms if t in hay)
            if score:
                scored.append((score, p))
        scored.sort(key=lambda x: (-x[0], x[1].submission_date))
        return [p for _, p in scored]


# A small, realistic-ish corpus covering the *legal* pre-T Kakeya toolbox
# (plan §0 "frontier delta": Wolff/Bourgain/Katz/Łaba/Tao/Guth, polynomial
# method, bush/hairbrush) plus the *contaminating* post-T Wang–Zahl paper, which
# the cutoff filter must hide.
_KAKEYA_CORPUS: list[ArxivPaper] = [
    ArxivPaper(
        arxiv_id="math/9906121",
        title="On the Bochner-Riesz conjecture and Kakeya maximal functions",
        authors=["Wolff"],
        submission_date=date(1999, 6, 18),
        abstract="Kakeya maximal function bounds via the bush and hairbrush arguments.",
    ),
    ArxivPaper(
        arxiv_id="math/0204258",
        title="A new bound on partial sum-sets and Kakeya in finite fields",
        authors=["Bourgain", "Katz", "Tao"],
        submission_date=date(2002, 4, 20),
        abstract="Arithmetic combinatorics applied to the Kakeya problem; incidence estimates.",
    ),
    ArxivPaper(
        arxiv_id="0807.2256",
        title="Algebraic methods in discrete analogs of the Kakeya problem",
        authors=["Guth", "Katz"],
        submission_date=date(2008, 7, 14),
        abstract="The polynomial method and polynomial partitioning for incidence problems.",
    ),
    ArxivPaper(
        arxiv_id="1011.4105",
        title="The endpoint case of the Bennett-Carbery-Tao multilinear Kakeya conjecture",
        authors=["Guth"],
        submission_date=date(2010, 11, 18),
        abstract="Multilinear Kakeya via polynomial method; tube incidence.",
    ),
    ArxivPaper(
        arxiv_id="1409.1885",
        title="Polynomial partitioning and incidences",
        authors=["Guth"],
        submission_date=date(2014, 9, 5),
        abstract="Induction on scales and partitioning for incidence geometry of tubes.",
    ),
    # ---- POST-CUTOFF, CONTAMINATING: must never surface under the frozen cutoff.
    ArxivPaper(
        arxiv_id="2502.17655",
        title="Volume estimates for unions of convex sets, and the Kakeya conjecture",
        authors=["Wang", "Zahl"],
        submission_date=date(2025, 2, 25),
        abstract=(
            "We prove the three-dimensional Kakeya conjecture via a convex set "
            "volume estimate and a multi-scale induction on scales, resolving the "
            "Heisenberg group obstruction."
        ),
    ),
]


class InMemoryArxivSource(ArxivFrozenSource):
    """Deterministic mock corpus (default backing for tests and the mock prover)."""

    def __init__(self, cutoff: date, corpus: Optional[list[ArxivPaper]] = None):
        super().__init__(cutoff)
        self._papers = list(corpus if corpus is not None else _KAKEYA_CORPUS)

    def _corpus(self) -> list[ArxivPaper]:
        return self._papers
