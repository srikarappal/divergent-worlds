"""Clone fidelity: agent success on an app clone against success on the real app (DESIGN.md section 6.4).

For a fixed task set two eval runs differ only in where the app lives, the
clone or the real app. The success rate of each run averages its per task
success over the tasks both runs scored; the gap is the clone's rate minus the
real app's and fidelity is one minus its size, the number catalogs show. A task
counts as solved when at least half of its episodes succeeded; tasks solved on
one side only are the discordant pairs an exact McNemar test weighs, so a gap
is called large only when it is wide and more than chance.
"""

import math
from collections.abc import Sequence

from pydantic import BaseModel, ConfigDict

DEFAULT_MAX_GAP = 0.1
DEFAULT_ALPHA = 0.05
SOLVED_RATE = 0.5


class FidelityError(ValueError):
    """The runs cannot be compared, such as when they share no scored task."""


class CloneFidelity(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    app: str
    clone_run_id: str
    real_run_id: str
    tasks: int
    clone_success_rate: float
    real_success_rate: float
    gap: float
    fidelity: float
    agreement: float
    clone_only: int
    real_only: int
    p_value: float
    large_gap: bool


def mcnemar_p_value(clone_only: int, real_only: int) -> float:
    """Exact two sided McNemar test: on a faithful clone the discordant tasks split like fair coin flips."""
    discordant = clone_only + real_only
    if discordant == 0:
        return 1.0
    tail = sum(math.comb(discordant, solved) for solved in range(min(clone_only, real_only) + 1))
    return min(1.0, 2 * tail / 2 ** discordant)


def clone_fidelity(app: str, clone_run_id: str, real_run_id: str, task_rates: Sequence[tuple[float, float]],
                   max_gap: float = DEFAULT_MAX_GAP, alpha: float = DEFAULT_ALPHA) -> CloneFidelity:
    """task_rates holds each shared task's success rate on the clone, then on the real app."""
    if not task_rates:
        raise FidelityError(f"{clone_run_id} and {real_run_id} share no scored task")
    clone_rate = sum(clone for clone, _ in task_rates) / len(task_rates)
    real_rate = sum(real for _, real in task_rates) / len(task_rates)
    solved = [(clone >= SOLVED_RATE, real >= SOLVED_RATE) for clone, real in task_rates]
    clone_only = sum(clone and not real for clone, real in solved)
    real_only = sum(real and not clone for clone, real in solved)
    gap = clone_rate - real_rate
    p_value = mcnemar_p_value(clone_only, real_only)
    return CloneFidelity(
        app=app, clone_run_id=clone_run_id, real_run_id=real_run_id, tasks=len(task_rates),
        clone_success_rate=clone_rate, real_success_rate=real_rate, gap=gap, fidelity=1.0 - abs(gap),
        agreement=1.0 - (clone_only + real_only) / len(task_rates), clone_only=clone_only, real_only=real_only,
        p_value=p_value, large_gap=abs(gap) > max_gap and p_value < alpha,
    )
