"""PROVER subsystem (plan §3): pre-cutoff model, frozen arXiv, no web search.

Backends are pluggable. ``MockProverBackend`` is the default (runnable +
testable offline); ``CodexProverBackend`` wraps ``codex exec`` for real runs.
"""

from .base import (
    BackendResponse,
    ProverBackend,
    ProverContext,
    get_backend,
    register_backend,
)
from .mock import MockProverBackend, MockProverConfig
from .runner import ProverRunner

__all__ = [
    "BackendResponse",
    "ProverBackend",
    "ProverContext",
    "get_backend",
    "register_backend",
    "MockProverBackend",
    "MockProverConfig",
    "ProverRunner",
]
