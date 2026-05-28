"""Model Registry (plan §6.2).

``{model, 训练数据 cutoff(带置信度), 是否开源, 能力档位, provider}``. The
Controller filters to ``cutoff < retrieval_cutoff`` per task. Vendor-claimed
cutoffs are "soft", so each entry carries a confidence; the §8 probes are the
final word.
"""

from __future__ import annotations

from pathlib import Path

import yaml

from .models import ModelEntry, TaskSpec


class ModelRegistry:
    def __init__(self, entries: list[ModelEntry] | None = None):
        self.entries: dict[str, ModelEntry] = {}
        for e in entries or []:
            self.entries[e.name] = e

    def add(self, entry: ModelEntry) -> None:
        self.entries[entry.name] = entry

    def get(self, name: str) -> ModelEntry:
        if name not in self.entries:
            raise KeyError(f"model registry 里没有 '{name}'")
        return self.entries[name]

    def eligible_models(self, task: TaskSpec) -> list[ModelEntry]:
        """cutoff < retrieval_cutoff 的候选 (plan §6.2)。"""
        return [e for e in self.entries.values() if e.eligible_for(task)]

    @classmethod
    def load(cls, path: str | Path) -> "ModelRegistry":
        data = yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {}
        models = data.get("models", data if isinstance(data, list) else [])
        return cls([ModelEntry(**m) for m in models])
