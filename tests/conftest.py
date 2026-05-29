import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from breakthrough_eval.registry import ModelRegistry  # noqa: E402
from breakthrough_eval.taskspec import load_all_tasks  # noqa: E402


@pytest.fixture
def tasks():
    return load_all_tasks(ROOT / "tasks")


@pytest.fixture
def task(tasks):
    return tasks["kakeya_3d_wang_zahl"]


@pytest.fixture
def registry():
    return ModelRegistry.load(ROOT / "models_registry.yaml")
