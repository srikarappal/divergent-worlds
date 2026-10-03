"""Review over recorded trajectories (DESIGN.md sections 6.11 and 6.14).

Cohorts filter a run's episodes by task, template, difficulty, policy version,
termination label or failure category and carry each episode's filmstrip of
frame digests, so 50 trajectories scrub side by side. Provenance drills from a
score to the checks, the judge rationale, the extracted states and the world
diff behind it. Failure analysis labels every failed episode, rolls the labels
up per template and policy version and files bug tickets into their queues.
Reports and comparisons set runs, or policy versions within one run, side by
side on the tasks they share. One click actions file review items into queues.
"""

import itertools
import threading
import time
from collections import defaultdict
from collections.abc import Callable, Mapping, Sequence
from dataclasses import asdict
from typing import Any, Literal

from pydantic import BaseModel, Field

from rlworld.analysis.failures import FailureLabel, LlmFailureLabeler, analyze_failures
from rlworld.rewards.diff import diff_states
from rlworld.rewards.records import RewardRecord, StateChange
from rlworld.services.experiments import UnknownRecordError
from rlworld.services.trajectory_views import TrajectoryViews
from rlworld.trajectories.exports import eval_report
from rlworld.trajectories.records import EpisodeRecord
from rlworld.trajectories.trajectory_store import check_identifier

ReviewAction = Literal["flag", "label", "verifier_bug", "task_draft", "audit_sample"]
QUEUE_BY_ACTION: dict[str, str] = {
    "flag": "review",
    "label": "labeling",
    "verifier_bug": "verifier",
    "task_draft": "factory",
    "audit_sample": "audit",
}


class ReviewError(ValueError):
    """A review action that is missing what it needs, such as a label without a verdict or category."""


class EpisodeFilter(BaseModel):
    termination: str | None = None
    task_id: str | None = None
    template: str | None = None
    difficulty_key: str | None = None
    category: str | None = None
    policy_version: int | None = None
    limit: int = Field(default=50, ge=1, le=500)


class CohortEpisode(BaseModel):
    episode: EpisodeRecord
    frames: list[str | None]
    category: str | None = None


class FailureAnalysisView(BaseModel):
    run_id: str
    analyzed_at: float
    labels: dict[str, FailureLabel]
    clusters: list[dict[str, Any]]
    tickets: list[dict[str, Any]]
    unlabeled_episode_ids: list[str]


class EpisodeProvenance(BaseModel):
    episode_id: str
    reward: RewardRecord | None
    initial_state: dict[str, Any]
    final_state: dict[str, Any] | None
    world_diff: list[StateChange]


class ReviewRequest(BaseModel):
    action: ReviewAction
    note: str = ""
    label: dict[str, Any] | None = None
    created_by: str = ""


class ReviewItem(BaseModel):
    item_id: str
    action: ReviewAction | Literal["bug_ticket"]
    queue: str
    run_id: str
    episode_ids: list[str]
    title: str
    note: str = ""
    label: dict[str, Any] | None = None
    payload: dict[str, Any] = Field(default_factory=dict)
    status: Literal["open", "resolved", "dismissed"] = "open"
    created_by: str = ""
    created_at: float
    source: Literal["console", "failure_analysis"] = "console"


class ComparisonRow(BaseModel):
    task_id: str
    template: str
    episodes: dict[str, int]
    success_rates: dict[str, float]


class Comparison(BaseModel):
    reports: dict[str, dict[str, Any]]
    shared_tasks: list[ComparisonRow]


def counted(episodes: Sequence[EpisodeRecord]) -> list[EpisodeRecord]:
    """Episodes that count toward statistics: scored, not env errors, not human intervened."""
    return [episode for episode in episodes
            if episode.reward is not None and episode.termination != "env_error" and not episode.human_intervened]


def compare(groups: Mapping[str, Sequence[EpisodeRecord]]) -> Comparison:
    """Reports per group plus success rates per task on the tasks every group attempted."""
    per_task: dict[str, dict[str, list[EpisodeRecord]]] = defaultdict(dict)
    for group_name, episodes in groups.items():
        for episode in counted(episodes):
            per_task[episode.task_id].setdefault(group_name, []).append(episode)
    rows = []
    for task_id, by_group in sorted(per_task.items()):
        if len(by_group) != len(groups):
            continue
        rows.append(ComparisonRow(
            task_id=task_id,
            template=next(iter(by_group.values()))[0].task_template,
            episodes={name: len(attempts) for name, attempts in by_group.items()},
            success_rates={name: sum(attempt.termination == "success" for attempt in attempts) / len(attempts)
                           for name, attempts in by_group.items()},
        ))
    return Comparison(reports={name: asdict(eval_report(episodes)) for name, episodes in groups.items()},
                      shared_tasks=rows)


def matches(episode: EpisodeRecord, filters: EpisodeFilter, category: str | None) -> bool:
    return all((
        filters.termination is None or episode.termination == filters.termination,
        filters.task_id is None or episode.task_id == filters.task_id,
        filters.template is None or episode.task_template == filters.template,
        filters.difficulty_key is None or episode.difficulty_key == filters.difficulty_key,
        filters.policy_version is None or episode.key.policy_version == filters.policy_version,
        filters.category is None or category == filters.category,
    ))


def failed_check_names(episode: EpisodeRecord) -> list[str]:
    return [check.name for check in (episode.reward.checks if episode.reward else ()) if not check.passed]


def analysis_key(run_id: str) -> str:
    return f"runs/{check_identifier(run_id)}/analysis/failures.json"


class ReviewService:
    def __init__(self, views: TrajectoryViews, llm_labeler: LlmFailureLabeler | None = None,
                 clock: Callable[[], float] = time.time) -> None:
        self.views = views
        self.llm_labeler = llm_labeler
        self.clock = clock
        self.items: dict[str, ReviewItem] = {}
        self.item_numbers = itertools.count(1)
        self.lock = threading.Lock()

    @property
    def object_store(self) -> Any:
        return self.views.trajectory_store.object_store

    def analysis(self, run_id: str) -> FailureAnalysisView | None:
        key = analysis_key(run_id)
        if not self.object_store.exists(key):
            return None
        return FailureAnalysisView.model_validate_json(self.object_store.get_bytes(key))

    def analyze(self, run_id: str) -> FailureAnalysisView:
        """Labels every finished failure, stores the rollup and files its bug tickets once."""
        failures = [self.views.episode(run_id, episode.episode_id)
                    for episode in self.views.episodes(run_id, "finished") if episode.termination != "success"]
        result = analyze_failures(failures, llm_labeler=self.llm_labeler)
        view = FailureAnalysisView(run_id=run_id, analyzed_at=self.clock(), labels=result.labels,
                                   clusters=[asdict(cluster) for cluster in result.clusters],
                                   tickets=[asdict(ticket) for ticket in result.tickets],
                                   unlabeled_episode_ids=list(result.unlabeled_episode_ids))
        self.object_store.put_bytes(analysis_key(run_id), view.model_dump_json().encode("utf-8"))
        for ticket in result.tickets:
            self.file_ticket(run_id, ticket.queue, ticket.title, list(ticket.episode_ids), list(ticket.evidence))
        return view

    def file_ticket(self, run_id: str, queue: str, title: str, episode_ids: list[str], evidence: list[str]) -> None:
        with self.lock:
            already_filed = any(item.run_id == run_id and item.queue == queue and item.title == title
                                for item in self.items.values())
        if not already_filed:
            self.add_item(action="bug_ticket", queue=queue, run_id=run_id, episode_ids=episode_ids, title=title,
                          payload={"evidence": evidence}, source="failure_analysis")

    def cohort(self, run_id: str, filters: EpisodeFilter) -> list[CohortEpisode]:
        analysis = self.analysis(run_id)
        labels = analysis.labels if analysis is not None else {}
        cohort = []
        for episode in self.views.episodes(run_id, "finished"):
            label = labels.get(episode.episode_id)
            category = label.category if label is not None else None
            if not matches(episode, filters, category):
                continue
            steps = self.views.trajectory_store.load_steps(run_id, episode.episode_id)
            frames = [step.observation.screenshot.sha256 if step.observation.screenshot else None for step in steps]
            cohort.append(CohortEpisode(episode=episode, frames=frames, category=category))
            if len(cohort) == filters.limit:
                break
        return cohort

    def provenance(self, run_id: str, episode_id: str) -> EpisodeProvenance:
        episode = self.views.episode(run_id, episode_id)
        store = self.views.trajectory_store
        initial_state = store.load_state(episode.initial_state_ref) if episode.initial_state_ref else {}
        final_state = store.load_state(episode.final_state_ref) if episode.final_state_ref else None
        return EpisodeProvenance(episode_id=episode_id, reward=episode.reward, initial_state=initial_state,
                                 final_state=final_state,
                                 world_diff=diff_states(initial_state, final_state) if final_state is not None else [])

    def prompt(self, run_id: str, episode_id: str, step_index: int) -> list[dict[str, Any]]:
        """The exact model input of one step, as the gateway sent it."""
        steps = self.views.trajectory_store.load_steps(run_id, episode_id, step_index)
        if not steps or steps[0].step_index != step_index or steps[0].model_turn.prompt_ref is None:
            raise UnknownRecordError(f"{run_id}/{episode_id} step {step_index} prompt")
        return self.views.trajectory_store.load_json_blob(steps[0].model_turn.prompt_ref)

    def report(self, run_id: str) -> dict[str, Any]:
        return asdict(eval_report(self.views.episodes(run_id, "finished")))

    def compare_runs(self, run_ids: Sequence[str]) -> Comparison:
        return compare({run_id: self.views.episodes(run_id, "finished") for run_id in run_ids})

    def compare_policy_versions(self, run_id: str) -> Comparison:
        by_version: dict[str, list[EpisodeRecord]] = defaultdict(list)
        for episode in self.views.episodes(run_id, "finished"):
            by_version[f"policy_version_{episode.key.policy_version}"].append(episode)
        return compare(dict(sorted(by_version.items())))

    def add_item(self, **fields: Any) -> ReviewItem:
        with self.lock:
            item = ReviewItem(item_id=f"review_{next(self.item_numbers):06d}", created_at=self.clock(), **fields)
            self.items[item.item_id] = item
        return item

    def act(self, run_id: str, episode_id: str, request: ReviewRequest) -> ReviewItem:
        """One click actions on an episode: flag, label, verifier bug, task draft or audit sample."""
        episode = self.views.episode(run_id, episode_id)
        if request.action == "label" and not request.label:
            raise ReviewError("a label needs a verdict or a failure category")
        failed_checks = failed_check_names(episode)
        titles = {
            "flag": f"flagged {episode.task_id}",
            "label": f"label {episode.task_id}",
            "verifier_bug": f"verifier false negative suspected on {episode.task_id}: {', '.join(failed_checks)}",
            "task_draft": f"task draft from a {episode.termination} on {episode.task_id}",
            "audit_sample": f"audit {episode.task_id}",
        }
        payload: dict[str, Any] = {"failed_checks": failed_checks}
        if request.action == "task_draft":
            payload.update(template=episode.task_template, instruction=episode.instruction,
                           difficulty_key=episode.difficulty_key, task_digest=episode.key.task_digest,
                           world_digest=episode.key.world_digest, seed=episode.key.seed)
        return self.add_item(action=request.action, queue=QUEUE_BY_ACTION[request.action], run_id=run_id,
                             episode_ids=[episode_id], title=titles[request.action], note=request.note,
                             label=request.label, payload=payload, created_by=request.created_by)

    def list_items(self, queue: str | None = None, status: str | None = None, run_id: str | None = None,
                   episode_id: str | None = None) -> list[ReviewItem]:
        with self.lock:
            items = list(self.items.values())
        return [item for item in items
                if (queue is None or item.queue == queue) and (status is None or item.status == status)
                and (run_id is None or item.run_id == run_id)
                and (episode_id is None or episode_id in item.episode_ids)]
