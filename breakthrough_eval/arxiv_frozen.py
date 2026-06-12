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

import re as _re
import threading as _threading
import time as _time
import urllib.request as _urlreq
import xml.etree.ElementTree as _ET
from datetime import date
from typing import Optional
from urllib.parse import quote as _quote

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

    def resolve_any(self, arxiv_id: str) -> Optional[ArxivPaper]:
        """审计员视角: 无视 cutoff 解析论文 (只给 §8.4 audit 用, 绝不给 PROVER)。"""
        try:
            for p in self._corpus():
                if p.arxiv_id == arxiv_id:
                    return p
        except NotImplementedError:
            return None
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


# Sphere-packing slice for the Klartag task: legal pre-T toolbox (Cohn–Elkies,
# Viazovska, Venkatesh, CJMS) plus the contaminating post-T Klartag paper.
_PACKING_CORPUS: list[ArxivPaper] = [
    ArxivPaper(
        arxiv_id="math/0110009",
        title="New upper bounds on sphere packings I",
        authors=["Cohn", "Elkies"],
        submission_date=date(2001, 10, 30),
        abstract="Linear programming bounds for sphere packing densities in all dimensions.",
    ),
    ArxivPaper(
        arxiv_id="1212.6184",
        title="A note on sphere packings in high dimension",
        authors=["Venkatesh"],
        submission_date=date(2012, 12, 26),
        abstract="Improved lower bounds for lattice sphere packings in special dimensions via cyclotomic structure.",
    ),
    ArxivPaper(
        arxiv_id="1603.04246",
        title="The sphere packing problem in dimension 8",
        authors=["Viazovska"],
        submission_date=date(2016, 3, 14),
        abstract="Modular forms construct the magic function solving sphere packing in dimension eight.",
    ),
    ArxivPaper(
        arxiv_id="2312.10026",
        title="A new lower bound for sphere packing",
        authors=["Campos", "Jenssen", "Michelen", "Sahasrabudhe"],
        submission_date=date(2023, 12, 15),
        abstract="Amorphous sphere packings of density c n log n / 2^n via graph theoretic methods.",
    ),
    # ---- POST-CUTOFF, CONTAMINATING (Klartag task): must stay frozen out.
    ArxivPaper(
        arxiv_id="2504.05042",
        title="Lattice packing of spheres in high dimensions using a stochastically evolving ellipsoid",
        authors=["Klartag"],
        submission_date=date(2025, 4, 7),
        abstract=(
            "A stochastically evolving ellipsoid constructs lattice sphere packings "
            "of density c n^2 / 2^n, the first superlinear improvement over Rogers."
        ),
    ),
]


class InMemoryArxivSource(ArxivFrozenSource):
    """Deterministic mock corpus (default backing for tests and the mock prover)."""

    def __init__(self, cutoff: date, corpus: Optional[list[ArxivPaper]] = None):
        super().__init__(cutoff)
        self._papers = list(corpus if corpus is not None
                            else _KAKEYA_CORPUS + _PACKING_CORPUS)

    def _corpus(self) -> list[ArxivPaper]:
        return self._papers


# --------------------------------------------------------------------------- #
# 真实 arXiv API 的时间冻结源 (plan §3.2 的生产形态)
# --------------------------------------------------------------------------- #
_ATOM_NS = {"a": "http://www.w3.org/2005/Atom"}
_VERSION_RE = _re.compile(r"v\d+$")
_TERM_RE = _re.compile(r"[\w\-]+")
_STOPWORDS = {"the", "a", "an", "of", "in", "for", "and", "or", "on", "to", "with"}

# arXiv API 礼貌间隔 (ToS 建议 3s); 模块级节流, 多线程共享。
_API_LOCK = _threading.Lock()
_LAST_CALL = [0.0]
_MIN_INTERVAL = 3.0


def _polite_wait() -> None:
    with _API_LOCK:
        delta = _time.time() - _LAST_CALL[0]
        if delta < _MIN_INTERVAL:
            _time.sleep(_MIN_INTERVAL - delta)
        _LAST_CALL[0] = _time.time()


class ArxivApiSource(ArxivFrozenSource):
    """真实 arXiv (export.arxiv.org) 的时间冻结检索。

    红线双保险:
      * 服务端: ``search_query`` 附带 ``submittedDate:[... TO cutoff]`` 过滤;
      * 客户端: 返回条目再按 ``<published>`` (v1 提交时间) 硬过滤一次 ——
        即便 API 行为变化, post-cutoff 论文也绝不返回 (defence in depth)。

    ``transport`` 可注入 (callable(url)->bytes), 测试离线。
    """

    BASE_URL = "https://export.arxiv.org/api/query"

    def __init__(self, cutoff: date, timeout: int = 30, max_terms: int = 4,
                 transport=None):
        super().__init__(cutoff)
        self.timeout = timeout
        self.max_terms = max_terms
        self._transport = transport or self._http_get

    # -- HTTP / parsing ---------------------------------------------------- #
    def _http_get(self, url: str) -> bytes:
        _polite_wait()
        req = _urlreq.Request(url, headers={"User-Agent": "breakthrough-eval/0.1"})
        with _urlreq.urlopen(req, timeout=self.timeout) as resp:
            return resp.read()

    def _parse_feed(self, raw: bytes) -> list[ArxivPaper]:
        root = _ET.fromstring(raw)
        papers: list[ArxivPaper] = []
        for entry in root.findall("a:entry", _ATOM_NS):
            id_url = (entry.findtext("a:id", "", _ATOM_NS) or "").strip()
            if "/abs/" not in id_url:
                continue  # API 错误条目等
            arxiv_id = _VERSION_RE.sub("", id_url.split("/abs/")[-1])
            published = (entry.findtext("a:published", "", _ATOM_NS) or "")[:10]
            if not published:
                continue
            papers.append(ArxivPaper(
                arxiv_id=arxiv_id,
                title=" ".join((entry.findtext("a:title", "", _ATOM_NS) or "").split()),
                authors=[(a.findtext("a:name", "", _ATOM_NS) or "").strip()
                         for a in entry.findall("a:author", _ATOM_NS)],
                submission_date=date.fromisoformat(published),
                abstract=" ".join((entry.findtext("a:summary", "", _ATOM_NS) or "").split()),
            ))
        return papers

    def _terms(self, query: str) -> list[str]:
        # 过滤停用词、纯数字和 <3 字符的短词 ("3d" 这类会把 AND 查询杀成 0 命中)。
        return [t for t in _TERM_RE.findall(query.lower())
                if t not in _STOPWORDS and len(t) >= 3 and not t.isdigit()]

    def _query_url(self, terms: list[str], max_results: int) -> str:
        terms = terms or ["mathematics"]
        date_clause = f"submittedDate:[199101010000 TO {self.cutoff:%Y%m%d}2359]"
        search = " AND ".join(f"all:{t}" for t in terms) + f" AND {date_clause}"
        return (f"{self.BASE_URL}?search_query={_quote(search)}"
                f"&sortBy=relevance&max_results={max_results}")

    # -- ArxivFrozenSource 接口 -------------------------------------------- #
    def search(self, query: str, max_results: int = 10) -> list[ArxivPaper]:
        """AND 查询命中为空时逐级放宽 (max_terms → 2 → 1 个词) 再试。"""
        terms = self._terms(query)
        tried: set[int] = set()
        for k in (self.max_terms, 2, 1):
            k = min(k, len(terms)) if terms else 0
            if k <= 0 or k in tried:
                continue
            tried.add(k)
            raw = self._transport(self._query_url(terms[:k], max_results))
            hits = [p for p in self._parse_feed(raw)
                    if p.submission_date <= self.cutoff]  # 客户端硬过滤 (红线)
            if hits:
                return hits[:max_results]
        return []

    def _lookup(self, arxiv_id: str) -> Optional[ArxivPaper]:
        raw = self._transport(f"{self.BASE_URL}?id_list={_quote(arxiv_id)}")
        for p in self._parse_feed(raw):
            if p.arxiv_id == arxiv_id:  # 必须核对 id (API 错误条目/意外返回不作数)
                return p
        return None

    def get(self, arxiv_id: str) -> Optional[ArxivPaper]:
        p = self._lookup(arxiv_id)
        return p if p is not None and p.submission_date <= self.cutoff else None

    def resolve_any(self, arxiv_id: str) -> Optional[ArxivPaper]:
        return self._lookup(arxiv_id)  # 审计员视角, 无视 cutoff
