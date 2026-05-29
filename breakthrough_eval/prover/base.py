"""PROVER backend interface + registry (plan §3.1).

A backend is anything that, given a system+user prompt (and, for simulation, the
frozen retrieval source), returns text + a transcript of tool calls + usage. The
*runner* (``runner.py``) orchestrates the probe-then-prove protocol on top.

Real backends (codex) only ever read ``ctx.system`` / ``ctx.user``. The mock
backend is allowed to peek at ``ctx.task`` / ``ctx.job`` because it is a
simulator, not a measured system.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable, Literal, Optional

from ..arxiv_frozen import ArxivFrozenSource
from ..models import (
    Job,
    ProverStructuredOutput,
    TaskSpec,
    ToolCall,
    UsageStats,
)

Phase = Literal["probe", "prove"]


@dataclass
class ProverContext:
    task: TaskSpec
    job: Job
    phase: Phase
    system: str
    user: str
    source: ArxivFrozenSource
    allowed_hosts: set[str] = field(default_factory=set)
    probe_id: Optional[str] = None


@dataclass
class BackendResponse:
    text: str
    tool_calls: list[ToolCall] = field(default_factory=list)
    usage: UsageStats = field(default_factory=UsageStats)
    # Mock backends may hand the runner a pre-parsed structured output; real
    # backends leave this None and let the runner parse ``text``.
    structured: Optional[ProverStructuredOutput] = None


class ProverBackend:
    """Abstract PROVER backend."""

    #: short stable name used in the harness fingerprint (plan §7)
    name: str = "abstract"
    #: scaffold version — same model + different scaffold == different harness
    scaffold_version: str = "v0"

    def available(self) -> bool:
        """Whether this backend can actually run in the current environment."""
        return True

    def run(self, ctx: ProverContext) -> BackendResponse:  # pragma: no cover
        raise NotImplementedError

    # Fingerprint -------------------------------------------------------- #
    def harness_fingerprint(self, model: str) -> str:
        """``model + scaffold + toolset`` (plan §7)."""
        return f"{model}+{self.name}-{self.scaffold_version}"


# --------------------------------------------------------------------------- #
# Backend registry (provider -> factory)
# --------------------------------------------------------------------------- #
_BACKENDS: dict[str, Callable[..., ProverBackend]] = {}


def register_backend(provider: str, factory: Callable[..., ProverBackend]) -> None:
    _BACKENDS[provider] = factory


def get_backend(provider: str, **kwargs) -> ProverBackend:
    if provider not in _BACKENDS:
        raise KeyError(
            f"未知 prover provider '{provider}'. 已注册: {sorted(_BACKENDS)}"
        )
    return _BACKENDS[provider](**kwargs)
