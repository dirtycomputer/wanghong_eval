"""Concrete PROVER backends."""

from ...specification import ApiConfig, HarnessConfig
from ..base import ProverBackend
from .codex import CodexProverBackend
from .direct import DirectProverBackend
from .hermes import HermesProverBackend
from .opencode import OpenCodeProverBackend

BACKENDS = {
    "codex": CodexProverBackend,
    "direct": DirectProverBackend,
    "hermes": HermesProverBackend,
    "opencode": OpenCodeProverBackend,
}


def make_backend(api: ApiConfig | None, harness: HarnessConfig) -> ProverBackend:
    if api is None:
        raise ValueError(f"harness '{harness.type}' requires api config")
    try:
        backend_cls = BACKENDS[harness.type]
    except KeyError as exc:
        raise KeyError(f"未知 prover harness '{harness.type}'. 已支持: {sorted(BACKENDS)}") from exc
    return backend_cls(api=api, harness=harness)


__all__ = [
    "BACKENDS",
    "CodexProverBackend",
    "DirectProverBackend",
    "HermesProverBackend",
    "OpenCodeProverBackend",
    "make_backend",
]
