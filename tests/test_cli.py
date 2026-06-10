"""CLI behaviours: early-stop skip reporting + probe-only `probe` command."""

import logging
from pathlib import Path

import pytest

from breakthrough_eval.cli import main

ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture(autouse=True)
def _restore_package_logger():
    # main() calls setup_logging (handlers + propagate=False); undo per test so
    # caplog-based tests elsewhere keep seeing package records.
    lg = logging.getLogger("breakthrough_eval")
    saved = (list(lg.handlers), lg.level, lg.propagate)
    yield
    lg.handlers[:] = saved[0]
    lg.setLevel(saved[1])
    lg.propagate = saved[2]


def _argv(tmp_path, *rest):
    return [
        "--tasks-dir", str(ROOT / "tasks"),
        "--registry", str(ROOT / "models_registry.yaml"),
        "--results-dir", str(tmp_path / "results"),
        *rest,
    ]


def test_run_reports_early_stop_skips(tmp_path, capsys):
    rc = main(_argv(
        tmp_path, "run", "--task", "kakeya_3d_wang_zahl",
        "--models", "leaky-eligible-by-date", "--trials", "2", "--workers", "1",
    ))
    out = capsys.readouterr().out
    assert rc == 0
    # 污染早停期间追加的 skip 必须汇报给用户, 而不只是 -v 日志。
    assert "运行中早停跳过" in out
    assert "早停: 已判污染除名" in out


def test_run_summary_includes_error_count(tmp_path, capsys):
    rc = main(_argv(
        tmp_path, "run", "--task", "kakeya_3d_wang_zahl",
        "--models", "open-precutoff-weak", "--hints", "0", "--trials", "1",
    ))
    out = capsys.readouterr().out
    assert rc == 0
    assert "0 个基础设施错误" in out


def test_probe_command_is_probe_only(tmp_path, capsys, monkeypatch):
    # probe 子命令只允许走 run_probes(); 完整 run() (会进证明阶段) 被禁止。
    from breakthrough_eval.prover.runner import ProverRunner

    def _forbidden(self, *a, **kw):  # pragma: no cover - 只在违规时触发
        raise AssertionError("probe 子命令不应触发完整 run() (含证明阶段)")

    monkeypatch.setattr(ProverRunner, "run", _forbidden)
    rc = main(_argv(
        tmp_path, "probe", "--task", "kakeya_3d_wang_zahl",
        "--model", "open-precutoff-strong",
    ))
    out = capsys.readouterr().out
    assert rc == 0
    assert "探针洁净" in out


def test_probe_command_reports_contamination(tmp_path, capsys):
    rc = main(_argv(
        tmp_path, "probe", "--task", "kakeya_3d_wang_zahl",
        "--model", "leaky-eligible-by-date",
    ))
    out = capsys.readouterr().out
    assert rc == 0
    assert "污染除名" in out
