"""CLI behaviours that do not require running a model backend."""

import logging
from pathlib import Path

import pytest

from breakthrough_eval.cli import main

ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture(autouse=True)
def _restore_package_logger():
    lg = logging.getLogger("breakthrough_eval")
    saved = (list(lg.handlers), lg.level, lg.propagate)
    yield
    lg.handlers[:] = saved[0]
    lg.setLevel(saved[1])
    lg.propagate = saved[2]


def _argv(tmp_path, *rest):
    return [
        "--tasks-dir",
        str(ROOT / "tasks"),
        "--registry",
        str(ROOT / "models_registry.yaml"),
        "--results-dir",
        str(tmp_path / "results"),
        *rest,
    ]


def test_list_models_uses_current_registry(tmp_path, capsys):
    rc = main(_argv(tmp_path, "list-models", "--task", "kakeya_3d_wang_zahl"))
    out = capsys.readouterr().out
    assert rc == 0
    assert "gemma-4-31b-direct" in out
    assert "gemma-4-31b-codex" in out
    assert "harness=direct" in out
    assert "harness=codex" in out


def test_export_web_empty_results_fails_cleanly(tmp_path, capsys):
    rc = main(_argv(tmp_path, "export-web", "--out", str(tmp_path / "data.js")))
    assert rc == 1
    assert "为空" in capsys.readouterr().out
