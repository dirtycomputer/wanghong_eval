"""JSON-on-disk storage for run + eval artifacts (plan §6.4: 产物入对象存储)."""

from __future__ import annotations

import json
from pathlib import Path

from .specification import EvalResult, ProverRunResult


class ResultStore:
    def __init__(self, root: str | Path):
        self.root = Path(root).resolve()
        self.prover_dir = self.root / "prover"
        self.eval_dir = self.root / "eval"
        self.prover_dir.mkdir(parents=True, exist_ok=True)
        self.eval_dir.mkdir(parents=True, exist_ok=True)

    def save_prover(self, result: ProverRunResult) -> Path:
        path = self.prover_dir / "result.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(result.model_dump_json(indent=2), encoding="utf-8")
        return path

    def save_eval(self, result: EvalResult) -> Path:
        path = self.eval_dir / "output" / "result.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(result.model_dump_json(indent=2), encoding="utf-8")
        return path

    # ---- run metadata (运行配置, 供 leaderboard/前端展示) ------------------ #
    def save_run_meta(self, meta: dict) -> Path:
        path = self.root / "run_meta.json"
        path.write_text(
            json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        return path

    def load_run_meta(self) -> dict | None:
        path = self.root / "run_meta.json"
        if not path.exists():
            return None
        return json.loads(path.read_text(encoding="utf-8"))

    def load_prover_all(self) -> list[ProverRunResult]:
        path = self.prover_dir / "result.json"
        if not path.exists():
            return []
        return [ProverRunResult.model_validate_json(path.read_text(encoding="utf-8"))]

    def load_eval_all(self) -> list[EvalResult]:
        path = self.eval_dir / "output" / "result.json"
        if not path.exists():
            return []
        return [EvalResult.model_validate_json(path.read_text(encoding="utf-8"))]
