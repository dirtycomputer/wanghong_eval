"""跨 task Bradley–Terry 聚合 + draft-task 起草工位."""

from pathlib import Path

from breakthrough_eval.aggregate import AggregateBoard
from breakthrough_eval.draft import draft_task, save_draft
from breakthrough_eval.leaderboard import LeaderboardRow
from breakthrough_eval.llm_client import OpenRouterClient

ROOT = Path(__file__).resolve().parent.parent


def _row(harness, task_id, auc=0.0, earned=None, cov=0.0, removed=False):
    return LeaderboardRow(
        harness=harness, task_id=task_id, hint_auc=auc,
        hint_auc_coverage=cov, peak_earned_coverage=earned, removed=removed,
    )


def test_bt_ranks_dominant_player_first():
    rows = [
        _row("A", "t1", earned=0.5), _row("B", "t1", earned=0.2),
        _row("A", "t2", earned=0.4), _row("B", "t2", earned=0.1),
    ]
    board = AggregateBoard.build(rows)
    assert [r.player for r in board.rows] == ["A", "B"]
    assert board.rows[0].bt_score == 100.0
    assert board.rows[0].wins == 2 and board.rows[1].wins == 0
    assert board.rows[0].component == board.rows[1].component == 0


def test_ties_split_half_point():
    rows = [_row("A", "t1", earned=0.3), _row("B", "t1", earned=0.3)]
    board = AggregateBoard.build(rows)
    assert all(r.wins == 0.5 for r in board.rows)


def test_disconnected_players_form_separate_leagues():
    # A/B 只在 t1, C/D 只在 t2: 无比较路径 → 两个联赛, 绝不跨组排名。
    rows = [
        _row("A", "t1", earned=0.5), _row("B", "t1", earned=0.1),
        _row("C", "t2", earned=0.9), _row("D", "t2", earned=0.2),
    ]
    board = AggregateBoard.build(rows)
    comps = {r.player: r.component for r in board.rows}
    assert comps["A"] == comps["B"] and comps["C"] == comps["D"]
    assert comps["A"] != comps["C"]
    assert "联赛" in board.render_table()


def test_removed_rows_do_not_compete():
    rows = [
        _row("A", "t1", earned=0.5), _row("B", "t1", earned=0.9, removed=True),
        _row("B", "t2", earned=0.9), _row("A", "t2", earned=0.1),
    ]
    board = AggregateBoard.build(rows)
    # t1 上 B 除名 → 只有 t2 一场: B 胜。
    b = next(r for r in board.rows if r.player == "B")
    assert b.games == 1 and b.wins == 1 and b.tasks == ["t2"]


def test_lexicographic_score_uses_earned_before_cov():
    # hint-AUC 同为 0: earned 高者胜, 即便 cov 低。
    rows = [
        _row("A", "t1", earned=0.3, cov=0.1), _row("B", "t1", earned=0.1, cov=0.9),
    ]
    board = AggregateBoard.build(rows)
    assert board.rows[0].player == "A"


# --------------------------------------------------------------------------- #
def _fake_client(reply):
    def transport(url, payload, headers):
        return {"choices": [{"message": {"content": reply, "tool_calls": []}}],
                "usage": {}}
    return OpenRouterClient(model="x", api_key="k", transport=transport)


def _kwargs():
    return dict(title="T", golden_arxiv="0000.00000", breakthrough_date="2025-04-07",
                retrieval_cutoff="2025-03-31", context_notes="notes",
                example_yaml="task_id: ex")


def test_draft_task_valid_yaml_passes(tmp_path):
    example = (ROOT / "tasks" / "kakeya_3d_wang_zahl.yaml").read_text(encoding="utf-8")
    text, issues = draft_task(
        **{**_kwargs(), "example_yaml": example},
        client=_fake_client("```yaml\n" + example + "\n```"),
    )
    assert issues == []  # 红线即时校验通过
    out = save_draft(text, tmp_path / "drafts" / "d.yaml")
    assert out.exists() and "kakeya" in out.read_text(encoding="utf-8")


def test_draft_task_keeps_invalid_draft_with_issues(tmp_path):
    text, issues = draft_task(**_kwargs(), client=_fake_client("task_id: broken"))
    assert issues and "broken" in text  # 草稿保留, 问题说清
