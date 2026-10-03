"""Verifier protocol, function verifiers and the registry the reward service loads them into.

Verifiers run host side after input is frozen; they read extracted state and
hidden ground truth that never enters the VM.
"""

from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Any, Literal, Protocol

from rlworld.rewards.records import CheckResult, FinalAction, StateChange
from rlworld.specs.models import TaskSpec

VerifierTier = Literal["exact", "semantic"]


@dataclass(frozen=True)
class VerifierContext:
    task: TaskSpec
    initial_state: dict[str, Any]
    final_state: dict[str, Any]
    changes: tuple[StateChange, ...]
    ground_truth: dict[str, Any]
    final_action: FinalAction | None = None
    answer_text: str | None = None


class Verifier(Protocol):
    name: str
    version: str
    tier: VerifierTier

    def verify(self, context: VerifierContext) -> list[CheckResult]: ...


@dataclass(frozen=True)
class FunctionVerifier:
    name: str
    version: str
    tier: VerifierTier
    check_function: Callable[[VerifierContext], list[CheckResult]]

    def verify(self, context: VerifierContext) -> list[CheckResult]:
        return list(self.check_function(context))


class UnknownVerifierError(KeyError):
    """A task names a verifier that was never registered."""


class DuplicateVerifierError(ValueError):
    """Two different verifiers claim the same name."""


class VerifierRegistry:
    def __init__(self) -> None:
        self.verifiers: dict[str, Verifier] = {}

    def register(self, verifier: Verifier) -> Verifier:
        existing_verifier = self.verifiers.get(verifier.name)
        if existing_verifier is not None and existing_verifier is not verifier:
            raise DuplicateVerifierError(f"verifier {verifier.name} is already registered")
        self.verifiers[verifier.name] = verifier
        return verifier

    def resolve(self, names: Sequence[str], tier: VerifierTier) -> list[Verifier]:
        resolved_verifiers = []
        for name in names:
            verifier = self.verifiers.get(name)
            if verifier is None:
                raise UnknownVerifierError(name)
            if verifier.tier != tier:
                raise ValueError(f"verifier {name} is a {verifier.tier} verifier but the task lists it as {tier}")
            resolved_verifiers.append(verifier)
        return resolved_verifiers
