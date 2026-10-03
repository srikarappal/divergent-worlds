"""Reward core: diffs, side effects, check helpers, verifiers and the scoring pipeline (DESIGN.md section 6.9)."""

from rlworld.rewards.diff import diff_states, path_matches, side_effect_violations
from rlworld.rewards.pipeline import (
    JudgeOutcome,
    JudgeTier,
    RewardConfigurationError,
    compose_reward,
    evaluate_episode,
)
from rlworld.rewards.records import (
    CheckResult,
    EpisodeOutcome,
    Provenance,
    RewardPolicy,
    RewardRecord,
    StateChange,
)
from rlworld.rewards.verifiers import (
    DuplicateVerifierError,
    FunctionVerifier,
    UnknownVerifierError,
    Verifier,
    VerifierContext,
    VerifierRegistry,
)

__all__ = [
    "CheckResult",
    "DuplicateVerifierError",
    "EpisodeOutcome",
    "FunctionVerifier",
    "JudgeOutcome",
    "JudgeTier",
    "Provenance",
    "RewardConfigurationError",
    "RewardPolicy",
    "RewardRecord",
    "StateChange",
    "UnknownVerifierError",
    "Verifier",
    "VerifierContext",
    "VerifierRegistry",
    "compose_reward",
    "diff_states",
    "evaluate_episode",
    "path_matches",
    "side_effect_violations",
]
