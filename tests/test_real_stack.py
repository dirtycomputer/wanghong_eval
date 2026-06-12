"""mock→真实栈替换: ArxivApiSource / 源选择 / 默认评委与探针评审."""

from datetime import date

import pytest

from breakthrough_eval.arxiv_frozen import ArxivApiSource, InMemoryArxivSource
from breakthrough_eval.cli import (
    DEFAULT_PROBE_JUDGE,
    REAL_JUDGE_PANEL,
    _default_judges,
    _parse_probe_judge,
    _source_factory_for,
)
from breakthrough_eval.models import ModelEntry
from breakthrough_eval.registry import ModelRegistry

ATOM = """<?xml version="1.0" encoding="UTF-8"?>
<feed xmlns="http://www.w3.org/2005/Atom">
  <entry>
    <id>http://arxiv.org/abs/1409.1885v2</id>
    <title>Polynomial   partitioning\n and incidences</title>
    <published>2014-09-05T00:00:00Z</published>
    <summary> Induction on scales. </summary>
    <author><name>Guth</name></author>
  </entry>
  <entry>
    <id>http://arxiv.org/abs/2502.17655v1</id>
    <title>Volume estimates and Kakeya</title>
    <published>2025-02-25T00:00:00Z</published>
    <summary>post-cutoff</summary>
    <author><name>Wang</name></author><author><name>Zahl</name></author>
  </entry>
</feed>"""


def _api(cutoff="2025-01-31", capture=None):
    def transport(url):
        if capture is not None:
            capture.append(url)
        return ATOM.encode("utf-8")
    return ArxivApiSource(date.fromisoformat(cutoff), transport=transport)


def test_api_source_hard_filters_post_cutoff_even_if_server_leaks():
    # 服务端 (假装) 泄露了 post-cutoff 论文 → 客户端硬过滤必须兜住 (红线双保险)。
    hits = _api().search("kakeya polynomial method", max_results=10)
    assert [p.arxiv_id for p in hits] == ["1409.1885"]  # 版本号已剥
    assert hits[0].submission_date == date(2014, 9, 5)
    assert hits[0].title == "Polynomial partitioning and incidences"  # 空白归一


def test_api_source_query_embeds_cutoff_and_caps_terms():
    urls = []
    _api(capture=urls).search("the lattice sphere packing density lower bound superlinear")
    assert "submittedDate" in urls[0] and "202501312359" in urls[0]
    # 停用词剔除 + 最多 4 个词 (all:lattice ... all:bound 不应出现第 5 个)
    assert urls[0].count("all%3A") == 4


def test_api_source_get_respects_cutoff_but_resolve_any_does_not():
    src = _api()
    assert src.get("2502.17655") is None          # PROVER 视角: 冻结
    p = src.resolve_any("2502.17655")             # 审计员视角: 可见
    assert p is not None and p.submission_date == date(2025, 2, 25)


def test_in_memory_resolve_any_for_audit(task):
    src = InMemoryArxivSource(task.retrieval_cutoff)
    assert src.get("2502.17655") is None
    assert src.resolve_any("2502.17655").arxiv_id == "2502.17655"


# ---- CLI 默认真实栈 -------------------------------------------------------- #
class _Args:
    def __init__(self, mode):
        self.arxiv_source = mode


def _registry(*providers):
    return ModelRegistry([
        ModelEntry(name=f"m{i}", cutoff_date="2024-01-01", provider=p)
        for i, p in enumerate(providers)
    ])


def test_source_auto_uses_mock_only_for_pure_mock_registry():
    assert _source_factory_for(_Args("auto"), _registry("mock", "mock")) is None
    assert _source_factory_for(_Args("auto"), _registry("mock", "openrouter")) is ArxivApiSource
    assert _source_factory_for(_Args("arxiv"), _registry("mock")) is ArxivApiSource
    assert _source_factory_for(_Args("mock"), _registry("openrouter")) is None


def test_default_judges_are_real_panel():
    judges = _default_judges()
    assert [j.describe()["model"] for j in judges] == list(REAL_JUDGE_PANEL)
    assert [j.describe()["max_tokens"] for j in judges] == list(REAL_JUDGE_PANEL.values())
    assert all(j.describe()["temperature"] == 0.0 for j in judges)


def test_probe_judge_defaults_real_and_none_disables():
    pj = _parse_probe_judge(None)
    assert pj is not None and pj.describe()["model"] == DEFAULT_PROBE_JUDGE.split(":", 1)[1]
    assert _parse_probe_judge("none") is None
    with pytest.raises(SystemExit):
        _parse_probe_judge("badspec")


def test_api_source_relaxes_query_on_empty_results():
    # 4 词 AND 0 命中 → 降级 2 词 → 1 词; 短词 "3d" 和纯数字被过滤。
    urls = []

    def transport(url):
        urls.append(url)
        return (ATOM if len(urls) >= 2 else "<feed xmlns='http://www.w3.org/2005/Atom'></feed>").encode()

    src = ArxivApiSource(date(2025, 1, 31), transport=transport)
    hits = src.search("kakeya conjecture 3d 2024 dimension tubes")
    assert len(urls) == 2  # 第一次空 → 放宽一次即命中
    assert "all%3A3d" not in urls[0] and "all%3A2024" not in urls[0]
    assert [p.arxiv_id for p in hits] == ["1409.1885"]


def test_api_source_returns_empty_after_all_relaxations():
    calls = []

    def transport(url):
        calls.append(url)
        return b"<feed xmlns='http://www.w3.org/2005/Atom'></feed>"

    src = ArxivApiSource(date(2025, 1, 31), transport=transport)
    assert src.search("kakeya polynomial incidence tubes") == []
    assert len(calls) == 3  # 4词 → 2词 → 1词 三级都试过
