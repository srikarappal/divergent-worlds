"""Judge calibration against human labels (DESIGN.md section 6.9).

A judge is trusted for a rubric once Cohen's kappa against a few hundred human
labels reaches the threshold. False passes (the judge passes what a human
fails) are reported apart because they are the reward hacking direction.
"""

from collections import defaultdict
from collections.abc import Sequence
from dataclasses import dataclass

OVERALL_KEY = "overall"


@dataclass(frozen=True)
class LabeledVerdict:
    episode_id: str
    item_id: str
    judge_passed: bool
    human_passed: bool


@dataclass(frozen=True)
class AgreementStats:
    label_count: int
    accuracy: float
    kappa: float
    false_passes: int
    false_fails: int


def cohen_kappa(label_pairs: Sequence[tuple[bool, bool]]) -> float:
    """Agreement beyond chance for (judge, human) boolean pairs."""
    if not label_pairs:
        raise ValueError("kappa needs at least one labeled pair")
    pair_count = len(label_pairs)
    observed_agreement = sum(1 for judge_label, human_label in label_pairs if judge_label == human_label) / pair_count
    judge_pass_rate = sum(1 for judge_label, _ in label_pairs if judge_label) / pair_count
    human_pass_rate = sum(1 for _, human_label in label_pairs if human_label) / pair_count
    chance_agreement = judge_pass_rate * human_pass_rate + (1 - judge_pass_rate) * (1 - human_pass_rate)
    if chance_agreement == 1.0:
        return 1.0 if observed_agreement == 1.0 else 0.0
    return (observed_agreement - chance_agreement) / (1 - chance_agreement)


def agreement_stats(labels: Sequence[LabeledVerdict]) -> AgreementStats:
    label_pairs = [(label.judge_passed, label.human_passed) for label in labels]
    return AgreementStats(
        label_count=len(labels),
        accuracy=sum(1 for judge_label, human_label in label_pairs if judge_label == human_label) / len(labels),
        kappa=cohen_kappa(label_pairs),
        false_passes=sum(1 for label in labels if label.judge_passed and not label.human_passed),
        false_fails=sum(1 for label in labels if label.human_passed and not label.judge_passed),
    )


def agreement_report(labels: Sequence[LabeledVerdict]) -> dict[str, AgreementStats]:
    """Stats per rubric item plus an overall row."""
    labels_by_item: dict[str, list[LabeledVerdict]] = defaultdict(list)
    for label in labels:
        labels_by_item[label.item_id].append(label)
    report = {item_id: agreement_stats(item_labels) for item_id, item_labels in sorted(labels_by_item.items())}
    report[OVERALL_KEY] = agreement_stats(labels)
    return report


def calibration_problems(
    report: dict[str, AgreementStats],
    minimum_kappa: float = 0.8,
    minimum_episodes: int = 300,
    episode_count: int | None = None,
) -> list[str]:
    """Empty when the judge may be trusted for this rubric."""
    problems = []
    if episode_count is not None and episode_count < minimum_episodes:
        problems.append(f"only {episode_count} labeled episodes, need {minimum_episodes}")
    for item_id, stats in report.items():
        if stats.kappa < minimum_kappa:
            problems.append(f"{item_id}: kappa {stats.kappa:.2f} is below {minimum_kappa}")
    return problems
