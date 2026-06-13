"""Small prover-runner helpers that do not require a live backend."""

from breakthrough_eval.prover.runner import parse_structured


def test_parse_structured_roundtrip():
    text = (
        "# Claimed Proof\n## 1. 思路总览\nidea\n## 2. 关键引理与论证\nlemmas\n"
        "## 3. 完整证明\nproof\n## 4. 引用文献\n- arXiv:1409.1885\n"
    )
    parsed = parse_structured(text)
    assert parsed.idea_overview == "idea"
    assert parsed.key_lemmas == "lemmas"
    assert parsed.full_proof == "proof"
    assert parsed.cited_references == ["arXiv:1409.1885"]
