"""Task validation gates, curriculum and task generation (DESIGN.md section 6.10)."""

from rlworld.tasks.curriculum import (
    CandidateTask,
    CurriculumPlanner,
    CurriculumSampler,
    GenerationRequest,
    PassRateTracker,
    filter_informative_groups,
    record_group_outcomes,
)
from rlworld.tasks.gates import (
    AttemptSummary,
    GateConfig,
    GateInputs,
    GateReport,
    GateResult,
    SolutionAttempt,
    TaskGateRunner,
)

__all__ = [
    "AttemptSummary",
    "CandidateTask",
    "CurriculumPlanner",
    "CurriculumSampler",
    "GateConfig",
    "GateInputs",
    "GateReport",
    "GateResult",
    "GenerationRequest",
    "PassRateTracker",
    "SolutionAttempt",
    "TaskGateRunner",
    "filter_informative_groups",
    "record_group_outcomes",
]
