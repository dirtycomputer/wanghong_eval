"""JSON-on-disk storage for run + eval artifacts (plan §6.4: 产物入对象存储)."""

from __future__ import annotations

import json
from pathlib import Path

from .models import EvalResult, ProverRunResult


class ResultStore:
    def __init__(self, root: str | Path):
        self.root = Path(root)
        self.prover_dir = self.root / "prover"
        self.eval_dir = self.root / "eval"
        self.prover_dir.mkdir(parents=True, exist_ok=True)
        self.eval_dir.mkdir(parents=True, exist_ok=True)

    @staticmethod
    def _safe(job_id: str) -> str:
        return job_id.replace("::", "__").replace("/", "_")

    def save_prover(self, result: ProverRunResult) -> Path:
        path = self.prover_dir / f"{self._safe(result.job_id)}.json"
        path.write_text(result.model_dump_json(indent=2), encoding="utf-8")
        return path

    def save_eval(self, result: EvalResult) -> Path:
        path = self.eval_dir / f"{self._safe(result.job_id)}.json"
        path.write_text(result.model_dump_json(indent=2), encoding="utf-8")
        return path

    def load_prover_all(self) -> list[ProverRunResult]:
        return [
            ProverRunResult.model_validate_json(p.read_text(encoding="utf-8"))
            for p in sorted(self.prover_dir.glob("*.json"))
        ]

    def load_eval_all(self) -> list[EvalResult]:
        return [
            EvalResult.model_validate_json(p.read_text(encoding="utf-8"))
            for p in sorted(self.eval_dir.glob("*.json"))
        ]
