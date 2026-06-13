"""Logging setup."""

import logging

import pytest

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
    setup_logging(verbosity=2)
    assert len(managed()) == 1
    assert lg.level <= logging.DEBUG
