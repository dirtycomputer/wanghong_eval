"""跨 task 聚合 (plan §7 的缺口): 合格集不齐时的总排名.

多 task 后不能直接平均 hint-AUC: 每个 task 的合格模型集由时间锚决定 (不齐),
任务间难度也不可校准。这里用比赛制:

  * 对每一对 harness, 只在**双方都有非除名行**的 task 交集上两两比较
    (指标字典序: hint-AUC > earned 峰值 > cov-AUC; 全同为平局记 0.5);
  * 胜负矩阵用 Bradley–Terry (MM 迭代) 拟合全局实力分;
  * **比较图不连通时按连通分量分「联赛」**, 绝不跨组排名 —— 没有共同任务的
    新旧模型之间不存在合法的比较路径 (持续上新任务正是为了架桥)。
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field

from .leaderboard import LeaderboardRow


def _score_tuple(row: LeaderboardRow) -> tuple:
    """单 task 上的比较指标 (字典序): 解出 > 自己挣到的覆盖 > 总覆盖."""
    earned = row.peak_earned_coverage if row.peak_earned_coverage is not None else -1.0
    return (round(row.hint_auc, 6), round(earned, 6), round(row.hint_auc_coverage, 6))


@dataclass
class AggregateRow:
    player: str  # harness
    component: int  # 联赛分组 (比较图连通分量)
    bt_score: float  # 组内归一: 组首 = 100
    wins: float  # 平局各记 0.5
    games: int
    tasks: list[str] = field(default_factory=list)


class AggregateBoard:
    def __init__(self, rows: list[AggregateRow]):
        self.rows = rows

    # ------------------------------------------------------------------ #
    @classmethod
    def build(cls, leaderboard_rows: list[LeaderboardRow],
              iterations: int = 200) -> "AggregateBoard":
        by_player: dict[str, dict[str, LeaderboardRow]] = defaultdict(dict)
        for r in leaderboard_rows:
            if not r.removed:  # 污染除名不参赛
                by_player[r.harness][r.task_id] = r
        players = sorted(by_player)
        n = len(players)
        idx = {p: i for i, p in enumerate(players)}

        # 两两比较 (仅共同任务)。
        wins = [[0.0] * n for _ in range(n)]
        games = [[0] * n for _ in range(n)]
        for i, a in enumerate(players):
            for j, b in enumerate(players):
                if j <= i:
                    continue
                common = set(by_player[a]) & set(by_player[b])
                for tid in common:
                    sa, sb = _score_tuple(by_player[a][tid]), _score_tuple(by_player[b][tid])
                    games[i][j] += 1
                    games[j][i] += 1
                    if sa > sb:
                        wins[i][j] += 1
                    elif sb > sa:
                        wins[j][i] += 1
                    else:
                        wins[i][j] += 0.5
                        wins[j][i] += 0.5

        # 连通分量 (有共同任务才有边)。
        comp = list(range(n))

        def find(x):
            while comp[x] != x:
                comp[x] = comp[comp[x]]
                x = comp[x]
            return x

        for i in range(n):
            for j in range(i + 1, n):
                if games[i][j] > 0:
                    comp[find(i)] = find(j)
        groups: dict[int, list[int]] = defaultdict(list)
        for i in range(n):
            groups[find(i)].append(i)
        # 大组在前、组内按选手名稳定。
        ordered = sorted(groups.values(), key=lambda g: (-len(g), players[g[0]]))

        rows: list[AggregateRow] = []
        for cid, members in enumerate(ordered):
            scores = cls._bradley_terry(members, wins, games, iterations)
            top = max(scores.values()) or 1.0
            for i in members:
                rows.append(AggregateRow(
                    player=players[i],
                    component=cid,
                    bt_score=100.0 * (scores[i] / top),
                    wins=sum(wins[i]),
                    games=sum(games[i]),
                    tasks=sorted(by_player[players[i]]),
                ))
        rows.sort(key=lambda r: (r.component, -r.bt_score, r.player))
        return cls(rows)

    @staticmethod
    def _bradley_terry(members: list[int], wins, games, iterations: int) -> dict[int, float]:
        """组内 BT 实力分, 标准 MM 迭代; 全败者得分趋零 (clamp 防除零)."""
        p = {i: 1.0 for i in members}
        if len(members) == 1:
            return p
        eps = 1e-9
        for _ in range(iterations):
            new = {}
            for i in members:
                w_i = sum(wins[i][j] for j in members if j != i)
                denom = sum(
                    games[i][j] / (p[i] + p[j])
                    for j in members if j != i and games[i][j] > 0
                )
                new[i] = max(eps, w_i / denom) if denom > 0 else p[i]
            mean = sum(new.values()) / len(new)
            p = {i: v / mean for i, v in new.items()}  # 归一防漂移
        return p

    # ------------------------------------------------------------------ #
    def render_table(self) -> str:
        header = "| 联赛 | Rank | Harness | BT 分 | 胜 (平=0.5) | 场次 | 参赛任务 |"
        lines = [header, "|" + "---|" * 7]
        rank = 0
        last_comp = None
        for r in self.rows:
            if r.component != last_comp:
                rank, last_comp = 1, r.component
            else:
                rank += 1
            lines.append(
                f"| {chr(ord('A') + r.component)} | {rank} | {r.player} | "
                f"{r.bt_score:.1f} | {r.wins:g} | {r.games} | {', '.join(r.tasks)} |"
            )
        return "\n".join(lines)
