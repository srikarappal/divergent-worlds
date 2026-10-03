"""Alerts on runs (DESIGN.md sections 6.9 and 6.14).

A monitor checks every run's recorded episodes and every experiment's spend.
It raises an alert when a task's reward jumps above its own history, the
classic reward hacking signal; when the judge and the verifiers disagree on too
many graded episodes; when the env error rate jumps; when spend crosses a
budget threshold; and when a run ends. Each alert fires once per subject. It
lands in the run's event stream so the console shows it live, files the
episodes behind spikes and disagreements into the review queue and goes out
through the configured Slack and email channels. Delivery records only whether
a send worked, never a channel's secrets.
"""

import itertools
import logging
import smtplib
import threading
import time
from collections import defaultdict
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass
from email.message import EmailMessage
from operator import attrgetter
from statistics import fmean
from typing import Any, Literal, Protocol

import httpx
from pydantic import BaseModel, Field

from rlworld.rewards.records import RewardRecord
from rlworld.services.experiments import TERMINAL_STATES, ExperimentService, RunRecord, UnknownRecordError
from rlworld.services.review import FiledItem, ReviewService
from rlworld.services.trajectory_views import TrajectoryViews
from rlworld.trajectories.records import EpisodeRecord

AlertKind = Literal["reward_spike", "judge_verifier_disagreement", "error_rate_jump", "budget_threshold",
                    "run_completion"]
Severity = Literal["info", "warning", "critical"]
SEVERITY_RANK = {"info": 0, "warning": 1, "critical": 2}
REVIEWED_KINDS = frozenset({"reward_spike", "judge_verifier_disagreement"})
MAX_EPISODES_PER_ALERT = 50
LOGGER = logging.getLogger(__name__)


class AlertDeliveryError(RuntimeError):
    """A channel refused an alert."""


class Alert(BaseModel):
    alert_id: str
    kind: AlertKind
    severity: Severity
    experiment_id: str
    run_id: str
    tenant: str
    subject: str
    title: str
    detail: dict[str, Any] = Field(default_factory=dict)
    episode_ids: list[str] = Field(default_factory=list)
    review_item_id: str | None = None
    raised_at: float
    status: Literal["open", "acknowledged"] = "open"
    acknowledged_by: str = ""
    acknowledged_at: float | None = None
    deliveries: dict[str, str] = Field(default_factory=dict)


@dataclass(frozen=True)
class AlertSettings:
    reward_spike_jump: float = 0.3
    reward_spike_window: int = 10
    reward_spike_min_history: int = 10
    disagreement_rate: float = 0.2
    disagreement_min_episodes: int = 20
    error_rate_jump: float = 0.1
    error_rate_window: int = 20
    error_rate_min_history: int = 20
    budget_fractions: tuple[float, ...] = (0.5, 0.8, 1.0)


@dataclass(frozen=True)
class Finding:
    """What a detector saw; the monitor turns new findings into alerts."""

    kind: AlertKind
    severity: Severity
    subject: str
    title: str
    detail: dict[str, Any]
    episode_ids: tuple[str, ...] = ()


class AlertChannel(Protocol):
    name: str
    min_severity: Severity

    def send(self, alert: Alert, text: str) -> None: ...


def finished_in_order(episodes: Iterable[EpisodeRecord]) -> list[EpisodeRecord]:
    return sorted((episode for episode in episodes if episode.finished_at is not None and episode.reward is not None),
                  key=attrgetter("finished_at", "episode_id"))


def reward_spikes(episodes: Sequence[EpisodeRecord], settings: AlertSettings) -> list[Finding]:
    """Per task, the latest window of rewards against everything before it; env errors never count."""
    by_task: dict[str, list[EpisodeRecord]] = defaultdict(list)
    for episode in finished_in_order(episodes):
        if episode.termination != "env_error":
            by_task[episode.task_id].append(episode)
    findings = []
    for task_id, task_episodes in sorted(by_task.items()):
        history, recent = task_episodes[:-settings.reward_spike_window], task_episodes[-settings.reward_spike_window:]
        if len(history) < settings.reward_spike_min_history or len(recent) < settings.reward_spike_window:
            continue
        baseline = fmean(episode.reward.total for episode in history)
        latest = fmean(episode.reward.total for episode in recent)
        if latest - baseline < settings.reward_spike_jump:
            continue
        findings.append(Finding(
            kind="reward_spike", severity="warning", subject=task_id,
            title=f"reward on {task_id} jumped from {baseline:.2f} to {latest:.2f}",
            detail={"task_id": task_id, "baseline_mean": round(baseline, 4), "recent_mean": round(latest, 4),
                    "history_episodes": len(history), "window": settings.reward_spike_window},
            episode_ids=tuple(episode.episode_id for episode in recent if episode.reward.total > baseline)))
    return findings


def verdicts(reward: RewardRecord) -> tuple[bool, bool] | None:
    """Whether the verifiers and the judge passed, for episodes both graded."""
    verifier_checks = [check for check in reward.checks if check.tier != "judge"]
    judge_checks = [check for check in reward.checks if check.tier == "judge"]
    if not verifier_checks or not judge_checks:
        return None
    return all(check.passed for check in verifier_checks), all(check.passed for check in judge_checks)


def judge_verifier_disagreement(episodes: Sequence[EpisodeRecord], settings: AlertSettings) -> list[Finding]:
    graded = [(episode, verdict) for episode in finished_in_order(episodes)
              if (verdict := verdicts(episode.reward)) is not None]
    if len(graded) < settings.disagreement_min_episodes:
        return []
    disagreeing = [episode for episode, (verifiers_passed, judge_passed) in graded if verifiers_passed != judge_passed]
    rate = len(disagreeing) / len(graded)
    if rate < settings.disagreement_rate:
        return []
    return [Finding(
        kind="judge_verifier_disagreement", severity="warning", subject="run",
        title=f"judge and verifiers disagree on {rate:.0%} of {len(graded)} graded episodes",
        detail={"rate": round(rate, 4), "graded_episodes": len(graded), "disagreeing_episodes": len(disagreeing)},
        episode_ids=tuple(episode.episode_id for episode in disagreeing[:MAX_EPISODES_PER_ALERT]))]


def error_rate_jump(episodes: Sequence[EpisodeRecord], settings: AlertSettings) -> list[Finding]:
    ordered = finished_in_order(episodes)
    history, recent = ordered[:-settings.error_rate_window], ordered[-settings.error_rate_window:]
    if len(history) < settings.error_rate_min_history or len(recent) < settings.error_rate_window:
        return []
    baseline = sum(episode.termination == "env_error" for episode in history) / len(history)
    latest = sum(episode.termination == "env_error" for episode in recent) / len(recent)
    if latest - baseline < settings.error_rate_jump:
        return []
    return [Finding(
        kind="error_rate_jump", severity="critical", subject="run",
        title=f"env error rate jumped from {baseline:.0%} to {latest:.0%}",
        detail={"baseline_rate": round(baseline, 4), "recent_rate": round(latest, 4), "window": len(recent)},
        episode_ids=tuple(episode.episode_id for episode in recent if episode.termination == "env_error"))]


def budget_findings(spent_usd: float, budget_usd: float, settings: AlertSettings) -> list[Finding]:
    if budget_usd <= 0:
        return []
    findings = []
    for fraction in sorted(settings.budget_fractions):
        if spent_usd >= fraction * budget_usd:
            severity: Severity = "critical" if fraction >= 1.0 else "warning" if fraction >= 0.8 else "info"
            findings.append(Finding(
                kind="budget_threshold", severity=severity, subject=f"{fraction:g}",
                title=f"spend reached {fraction:.0%} of the ${budget_usd:,.2f} budget",
                detail={"fraction": fraction, "spent_usd": round(spent_usd, 2), "budget_usd": budget_usd}))
    return findings


def completion_finding(run: RunRecord) -> Finding:
    return Finding(kind="run_completion", severity="critical" if run.state == "failed" else "info", subject=run.state,
                   title=f"run {run.run_name} {run.state}", detail={"state": run.state, "run_name": run.run_name})


def alert_text(alert: Alert, console_url: str) -> str:
    lines = [f"[{alert.severity}] {alert.title}", f"experiment {alert.experiment_id}"]
    if alert.run_id:
        lines.append(f"run {alert.run_id}")
        if console_url:
            lines.append(f"{console_url.rstrip('/')}/runs/{alert.run_id}")
    if alert.episode_ids:
        lines.append(f"{len(alert.episode_ids)} episodes filed for review")
    return "\n".join(lines)


class SlackChannel:
    """Posts to a Slack incoming webhook; the webhook URL is the secret, so errors never repeat it."""

    def __init__(self, webhook_url: str, http_client: httpx.Client | None = None, name: str = "slack",
                 min_severity: Severity = "info") -> None:
        self.webhook_url = webhook_url
        self.http_client = http_client or httpx.Client(timeout=10.0)
        self.name = name
        self.min_severity = min_severity

    def send(self, alert: Alert, text: str) -> None:
        response = self.http_client.post(self.webhook_url, json={"text": text})
        if response.status_code >= 300:
            raise AlertDeliveryError(f"slack answered {response.status_code}")


class EmailChannel:
    def __init__(self, smtp_host: str, sender: str, recipients: Sequence[str], smtp_port: int = 587,
                 username: str | None = None, password: str | None = None, use_tls: bool = True,
                 connect: Callable[..., smtplib.SMTP] = smtplib.SMTP, name: str = "email",
                 min_severity: Severity = "warning") -> None:
        if not recipients:
            raise ValueError("an email channel needs recipients")
        self.smtp_host = smtp_host
        self.smtp_port = smtp_port
        self.sender = sender
        self.recipients = tuple(recipients)
        self.username = username
        self.password = password
        self.use_tls = use_tls
        self.connect = connect
        self.name = name
        self.min_severity = min_severity

    def send(self, alert: Alert, text: str) -> None:
        message = EmailMessage()
        message["Subject"] = f"[rlworld {alert.severity}] {alert.title}"
        message["From"] = self.sender
        message["To"] = ", ".join(self.recipients)
        message.set_content(text)
        with self.connect(self.smtp_host, self.smtp_port, timeout=10) as smtp:
            if self.use_tls:
                smtp.starttls()
            if self.username:
                smtp.login(self.username, self.password or "")
            smtp.send_message(message)


class AlertMonitor:
    def __init__(self, experiment_service: ExperimentService, trajectory_views: TrajectoryViews,
                 review_service: ReviewService | None = None, channels: Sequence[AlertChannel] = (),
                 settings: AlertSettings | None = None, console_url: str = "",
                 clock: Callable[[], float] = time.time) -> None:
        self.experiment_service = experiment_service
        self.trajectory_views = trajectory_views
        self.review_service = review_service
        self.channels = tuple(channels)
        self.settings = settings or AlertSettings()
        self.console_url = console_url
        self.clock = clock
        self.alerts_by_id: dict[str, Alert] = {}
        self.raised_keys: set[tuple[str, str, str]] = set()
        self.finished_runs: set[str] = set()
        self.alert_numbers = itertools.count(1)
        self.lock = threading.Lock()

    def episode_findings(self, run_id: str) -> list[Finding]:
        try:
            episodes = self.trajectory_views.episodes(run_id, "finished")
        except UnknownRecordError:
            return []
        return [*reward_spikes(episodes, self.settings), *judge_verifier_disagreement(episodes, self.settings),
                *error_rate_jump(episodes, self.settings)]

    def check(self) -> list[Alert]:
        """Evaluates every experiment's spend and every live run, plus each run that just ended once more."""
        raised = []
        for experiment in self.experiment_service.list_experiments():
            for finding in budget_findings(experiment.spent_usd, experiment.budget_usd, self.settings):
                raised += self.raise_alert(finding, experiment.experiment_id, "", experiment.tenant,
                                           experiment.run_ids)
        for run in self.experiment_service.list_runs():
            if run.run_id in self.finished_runs:
                continue
            findings = self.episode_findings(run.run_id)
            if run.state in TERMINAL_STATES:
                findings.append(completion_finding(run))
                self.finished_runs.add(run.run_id)
            for finding in findings:
                raised += self.raise_alert(finding, run.experiment_id, run.run_id, run.tenant, [run.run_id])
        return raised

    def raise_alert(self, finding: Finding, experiment_id: str, run_id: str, tenant: str,
                    event_run_ids: Sequence[str]) -> list[Alert]:
        """Each subject alerts once: spend per experiment, everything else per run."""
        key = (run_id or experiment_id, finding.kind, finding.subject)
        with self.lock:
            if key in self.raised_keys:
                return []
            self.raised_keys.add(key)
            alert = Alert(alert_id=f"alert_{next(self.alert_numbers):06d}", kind=finding.kind,
                          severity=finding.severity, experiment_id=experiment_id, run_id=run_id, tenant=tenant,
                          subject=finding.subject, title=finding.title, detail=finding.detail,
                          episode_ids=list(finding.episode_ids), raised_at=self.clock())
        if self.review_service is not None and finding.kind in REVIEWED_KINDS and finding.episode_ids:
            item = self.review_service.file_item(FiledItem(
                source="alert", queue="review", run_id=run_id, episode_ids=list(finding.episode_ids),
                title=finding.title, payload={"alert_id": alert.alert_id, "kind": finding.kind}), actor="alerts")
            alert = alert.model_copy(update={"review_item_id": item.item_id})
        alert = alert.model_copy(update={"deliveries": self.deliver(alert)})
        with self.lock:
            self.alerts_by_id[alert.alert_id] = alert
        summary = alert.model_dump(mode="json", include={"alert_id", "kind", "severity", "subject", "title"})
        for event_run_id in event_run_ids:
            self.experiment_service.record_event(event_run_id, "alert", summary)
        return [alert]

    def deliver(self, alert: Alert) -> dict[str, str]:
        deliveries = {}
        text = alert_text(alert, self.console_url)
        for channel in self.channels:
            if SEVERITY_RANK[alert.severity] < SEVERITY_RANK[channel.min_severity]:
                continue
            try:
                channel.send(alert, text)
            except (AlertDeliveryError, httpx.HTTPError, smtplib.SMTPException, OSError) as error:
                deliveries[channel.name] = f"failed: {type(error).__name__}"
            else:
                deliveries[channel.name] = "sent"
        return deliveries

    def alerts(self, run_id: str | None = None, experiment_id: str | None = None,
               status: str | None = None) -> list[Alert]:
        with self.lock:
            alerts = list(self.alerts_by_id.values())
        return [alert for alert in alerts
                if (run_id is None or alert.run_id == run_id)
                and (experiment_id is None or alert.experiment_id == experiment_id)
                and (status is None or alert.status == status)]

    def acknowledge(self, alert_id: str, actor: str) -> Alert:
        with self.lock:
            if alert_id not in self.alerts_by_id:
                raise UnknownRecordError(alert_id)
            alert = self.alerts_by_id[alert_id].model_copy(update={
                "status": "acknowledged", "acknowledged_by": actor, "acknowledged_at": self.clock()})
            self.alerts_by_id[alert_id] = alert
        return alert

    def run_forever(self, stop: threading.Event, interval_seconds: float = 30.0) -> None:
        """The server's background loop; a failing check is logged and the next one runs on schedule."""
        while not stop.wait(interval_seconds):
            try:
                self.check()
            except Exception:  # one bad run or store hiccup must not end alerting for every run
                LOGGER.exception("alert check failed")
