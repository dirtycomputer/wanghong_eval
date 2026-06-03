"""Logging setup + that a run emits useful progress records."""

import logging

import pytest

from breakthrough_eval.controller import Controller
from breakthrough_eval.eval.evaluator import Evaluator
from breakthrough_eval.eval.mock import MockJudge
from breakthrough_eval.logging_util import setup_logging


@pytest.fixture(autouse=True)
def _restore_package_logger():
    lg = logging.getLogger("breakthrough_eval")
    saved = (list(lg.handlers), lg.level, lg.propagate)
    yield
    lg.handlers[:] = saved[0]
    lg.setLevel(saved[1])
    lg.propagate = saved[2]


def test_setup_logging_idempotent():
    lg = setup_logging(verbosity=1)
    managed = lambda: [h for h in lg.handlers if getattr(h, "_be_managed", False)]
    assert len(managed()) == 1
    setup_logging(verbosity=2)  # second call must not duplicate handlers
    assert len(managed()) == 1
    assert lg.level <= logging.DEBUG


def test_run_emits_progress_logs(tasks, registry, task, caplog):
    ctrl = Controller(tasks, registry, Evaluator([MockJudge()]))
    with caplog.at_level(logging.INFO, logger="breakthrough_eval"):
        matrix = ctrl.expand_job_matrix(
            [task.task_id], model_names=["open-precutoff-strong"], hint_levels=[0, 1], trials=1
        )
        ctrl.run(matrix)
    text = caplog.text
    assert "Phase 1" in text          # controller phase logging
    assert "探针阶段" in text          # probe phase
    assert "证明完成" in text          # prove phase with timing
    assert "eval" in text             # evaluator outcome


def test_contamination_logged_as_warning(tasks, registry, task, caplog):
    ctrl = Controller(tasks, registry, Evaluator([MockJudge()]))
    with caplog.at_level(logging.WARNING, logger="breakthrough_eval"):
        matrix = ctrl.expand_job_matrix(
            [task.task_id], model_names=["leaky-eligible-by-date"], hint_levels=[0], trials=1
        )
        ctrl.run(matrix)
    assert "污染" in caplog.text
