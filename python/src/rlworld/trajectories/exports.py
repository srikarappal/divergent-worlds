"""Data products from trajectories (DESIGN.md sections 6.11 and 6.12).

SFT rows come from trainable successes only. Preference pairs come from GRPO
groups, best against worst. Eval reports give Wilson confidence intervals and
the unbiased pass@k estimator, with breakdowns per template and difficulty.
"""

import math
from collections import Counter, defaultdict
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from typing import Any, Literal

from rlworld.trajectories.records import EpisodeRecord


@dataclass(frozen=True)
class SftRow:
    episode_id: str
    step_index: int
    prompt_ref: str | None
    completion_text: str
    tool_calls: tuple[dict[str, Any], ...]
    prompt_token_ids: tuple[int, ...] | None
    completion_token_ids: tuple[int, ...] | None


@dataclass(frozen=True)
class PreferencePair:
    group_id: str
    task_id: str
    chosen_episode_id: str
    rejected_episode_id: str
    chosen_total: float
    rejected_total: float


@dataclass(frozen=True)
class EvalBreakdown:
    episodes: int
    successes: int
    success_rate: float
    interval_low: float
    interval_high: float
    mean_total: float
    terminations: dict[str, int] = field(default_factory=dict)


@dataclass(frozen=True)
class EvalReport:
    overall: EvalBreakdown
    by_template: dict[str, EvalBreakdown]
    by_difficulty: dict[str, EvalBreakdown]
    pass_at_k: dict[int, float]
    excluded_env_errors: int
    excluded_human_intervened: int


def sft_rows(episodes: Iterable[EpisodeRecord]) -> list[SftRow]:
    rows = []
    for episode in episodes:
        if not episode.is_trainable_success:
            continue
        for step in episode.steps:
            token_trace = step.model_turn.token_trace
            rows.append(
                SftRow(
                    episode_id=episode.episode_id,
                    step_index=step.step_index,
                    prompt_ref=step.model_turn.prompt_ref,
                    completion_text=step.model_turn.output_text,
                    tool_calls=step.model_turn.tool_calls,
                    prompt_token_ids=token_trace.prompt_token_ids if token_trace else None,
                    completion_token_ids=token_trace.completion_token_ids if token_trace else None,
                )
            )
    return rows


def preference_pairs(episodes: Iterable[EpisodeRecord], max_pairs_per_group: int = 4) -> list[PreferencePair]:
    """Within each GRPO group, pair the best scoring episodes against the worst ones."""
    episodes_by_group: dict[str, list[EpisodeRecord]] = defaultdict(list)
    for episode in episodes:
        if episode.group_id and episode.reward and episode.reward.counts_for_training and not episode.human_intervened:
            episodes_by_group[episode.group_id].append(episode)
    pairs = []
    for group_id, group_episodes in sorted(episodes_by_group.items()):
        best_total = max(episode.reward.total for episode in group_episodes)
        worst_total = min(episode.reward.total for episode in group_episodes)
        if best_total == worst_total:
            continue
        winners = sorted((episode for episode in group_episodes if episode.reward.total == best_total),
                         key=episode_identifier)
        losers = sorted((episode for episode in group_episodes if episode.reward.total == worst_total),
                        key=episode_identifier)
        group_pairs = [
            PreferencePair(group_id, winner.task_id, winner.episode_id, loser.episode_id, best_total, worst_total)
            for winner in winners
            for loser in losers
        ]
        pairs.extend(group_pairs[:max_pairs_per_group])
    return pairs


def episode_identifier(episode: EpisodeRecord) -> str:
    return episode.episode_id


def wilson_interval(successes: int, trials: int, z_score: float = 1.96) -> tuple[float, float]:
    if trials == 0:
        return 0.0, 1.0
    success_rate = successes / trials
    denominator = 1 + z_score**2 / trials
    center = (success_rate + z_score**2 / (2 * trials)) / denominator
    margin = z_score * math.sqrt(success_rate * (1 - success_rate) / trials + z_score**2 / (4 * trials**2)) / denominator
    return max(0.0, center - margin), min(1.0, center + margin)


def pass_at_k(sample_count: int, correct_count: int, k: int) -> float:
    """Unbiased estimator: chance at least one of k samples drawn from n is correct."""
    if sample_count - correct_count < k:
        return 1.0
    return 1.0 - math.comb(sample_count - correct_count, k) / math.comb(sample_count, k)


def breakdown(episodes: Sequence[EpisodeRecord]) -> EvalBreakdown:
    successes = sum(1 for episode in episodes if episode.termination == "success")
    interval_low, interval_high = wilson_interval(successes, len(episodes))
    return EvalBreakdown(
        episodes=len(episodes),
        successes=successes,
        success_rate=successes / len(episodes) if episodes else 0.0,
        interval_low=interval_low,
        interval_high=interval_high,
        mean_total=sum(episode.reward.total for episode in episodes) / len(episodes) if episodes else 0.0,
        terminations=dict(Counter(episode.termination for episode in episodes)),
    )


def eval_report(episodes: Iterable[EpisodeRecord], k_values: Sequence[int] = (1, 4)) -> EvalReport:
    scored_episodes = [episode for episode in episodes if episode.reward is not None]
    env_errors = [episode for episode in scored_episodes if episode.termination == "env_error"]
    intervened = [episode for episode in scored_episodes
                  if episode.human_intervened and episode.termination != "env_error"]
    counted = [episode for episode in scored_episodes
               if episode.termination != "env_error" and not episode.human_intervened]
    by_template: dict[str, list[EpisodeRecord]] = defaultdict(list)
    by_difficulty: dict[str, list[EpisodeRecord]] = defaultdict(list)
    by_task: dict[str, list[EpisodeRecord]] = defaultdict(list)
    for episode in counted:
        by_template[episode.task_template].append(episode)
        by_difficulty[episode.difficulty_key].append(episode)
        by_task[episode.task_id].append(episode)
    pass_rates: dict[int, float] = {}
    for k in k_values:
        eligible_tasks = [task_episodes for task_episodes in by_task.values() if len(task_episodes) >= k]
        if eligible_tasks:
            pass_rates[k] = sum(
                pass_at_k(len(task_episodes),
                          sum(1 for episode in task_episodes if episode.termination == "success"), k)
                for task_episodes in eligible_tasks
            ) / len(eligible_tasks)
    return EvalReport(
        overall=breakdown(counted),
        by_template={name: breakdown(group) for name, group in sorted(by_template.items())},
        by_difficulty={name: breakdown(group) for name, group in sorted(by_difficulty.items())},
        pass_at_k=pass_rates,
        excluded_env_errors=len(env_errors),
        excluded_human_intervened=len(intervened),
    )


def retention_class(episode: EpisodeRecord, full_sample_rate: float, random_value: float) -> Literal["full", "thumbnail"]:
    """Full resolution for evals, failures, interventions and a sample; thumbnails for the rest."""
    keeps_full_resolution = (
        episode.run_type == "eval"
        or episode.termination != "success"
        or episode.human_intervened
        or random_value < full_sample_rate
    )
    return "full" if keeps_full_resolution else "thumbnail"
