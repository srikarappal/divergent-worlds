import json
import threading

import httpx
import pytest
from support.experiment_fakes import FlaskBridge, build_experiment_service, eval_payload
from support.trajectory_fakes import episode

from rlworld.rewards.records import CheckResult, Provenance, RewardPolicy, RewardRecord
from rlworld.services.alerts import (
    Alert,
    AlertDeliveryError,
    AlertMonitor,
    AlertSettings,
    EmailChannel,
    SlackChannel,
    budget_findings,
    error_rate_jump,
    judge_verifier_disagreement,
    reward_spikes,
)
from rlworld.services.experimentd_http import create_app
from rlworld.services.review import ReviewService
from rlworld.services.trajectory_views import TrajectoryViews
from rlworld.specs import dump_spec_document, parse_spec_document
from rlworld.trajectories import InMemoryObjectStore, TrajectoryStore

SETTINGS = AlertSettings(reward_spike_window=4, reward_spike_min_history=4, disagreement_min_episodes=8,
                         error_rate_window=5, error_rate_min_history=5)


def graded(total, termination="success", verifier_passed=None, judge_passed=None):
    checks = []
    if verifier_passed is not None:
        checks.append(CheckResult(name="values_match", tier="exact", passed=verifier_passed))
    if judge_passed is not None:
        checks.append(CheckResult(name="summary_quality", tier="judge", passed=judge_passed))
    provenance = Provenance(state_hash="sha256:s", ground_truth_digest="sha256:g", reward_policy=RewardPolicy())
    return RewardRecord(total=total, components={"outcome": total}, termination=termination,
                        counts_for_training=termination != "env_error", checks=tuple(checks), provenance=provenance)


def finished(index, task_id, reward_record, run_id="run_1"):
    return episode(f"episode_{index:03d}", run_id=run_id, task_id=task_id, reward=reward_record,
                   finished_at=f"2026-10-03T10:{index // 60:02d}:{index % 60:02d}.000+00:00")


def test_reward_spikes_compare_a_tasks_latest_window_with_its_history():
    episodes = [finished(index, "task_a", graded(0.1, "failure")) for index in range(4)]
    episodes += [finished(4 + index, "task_a", graded(1.0)) for index in range(4)]
    episodes += [finished(8 + index, "task_b", graded(0.5)) for index in range(8)]
    episodes += [finished(16 + index, "task_c", graded(1.0)) for index in range(5)]
    episodes.append(finished(30, "task_a", graded(0.0, "env_error")))
    findings = reward_spikes(episodes, SETTINGS)
    assert [finding.subject for finding in findings] == ["task_a"]
    spike = findings[0]
    assert spike.kind == "reward_spike" and spike.detail["baseline_mean"] == 0.1 and spike.detail["recent_mean"] == 1.0
    assert spike.episode_ids == ("episode_004", "episode_005", "episode_006", "episode_007")


def test_disagreement_counts_only_episodes_both_graded():
    episodes = [finished(index, "task_a", graded(1.0, verifier_passed=True, judge_passed=index >= 2))
                for index in range(8)]
    episodes += [finished(20 + index, "task_a", graded(1.0, verifier_passed=True)) for index in range(10)]
    findings = judge_verifier_disagreement(episodes, SETTINGS)
    assert len(findings) == 1 and findings[0].detail == {"rate": 0.25, "graded_episodes": 8, "disagreeing_episodes": 2}
    assert findings[0].episode_ids == ("episode_000", "episode_001")
    assert judge_verifier_disagreement(episodes[:7], SETTINGS) == []
    agreeing = [finished(index, "task_a", graded(1.0, verifier_passed=True, judge_passed=True)) for index in range(8)]
    assert judge_verifier_disagreement(agreeing, SETTINGS) == []


def test_error_rate_jumps_against_the_runs_history():
    episodes = [finished(index, "task_a", graded(1.0)) for index in range(5)]
    episodes += [finished(5 + index, "task_a", graded(0.0, "env_error" if index < 2 else "success"))
                 for index in range(5)]
    findings = error_rate_jump(episodes, SETTINGS)
    assert len(findings) == 1 and findings[0].severity == "critical"
    assert findings[0].detail["baseline_rate"] == 0.0 and findings[0].detail["recent_rate"] == 0.4
    assert findings[0].episode_ids == ("episode_005", "episode_006")
    assert error_rate_jump(episodes[:9], SETTINGS) == []


def test_budget_thresholds_escalate():
    findings = budget_findings(85.0, 100.0, SETTINGS)
    assert [(finding.subject, finding.severity) for finding in findings] == [("0.5", "info"), ("0.8", "warning")]
    assert budget_findings(100.0, 100.0, SETTINGS)[-1].severity == "critical"
    assert budget_findings(10.0, 0.0, SETTINGS) == []


class RecordingChannel:
    def __init__(self, name="record", min_severity="info", failure=None):
        self.name = name
        self.min_severity = min_severity
        self.failure = failure
        self.sent = []

    def send(self, alert, text):
        if self.failure is not None:
            raise self.failure
        self.sent.append((alert.kind, text))


def launched_monitor(example_payload, tmp_path, channels=()):
    service = build_experiment_service(eval_payload(example_payload), tmp_path)
    digest = service.save_spec(dump_spec_document(parse_spec_document(eval_payload(example_payload))))
    experiment = service.launch(digest, budget_usd=100.0)
    trajectory_store = TrajectoryStore(InMemoryObjectStore())
    views = TrajectoryViews(trajectory_store)
    review = ReviewService(views, clock=float)
    monitor = AlertMonitor(service, views, review, channels=channels, settings=SETTINGS,
                           console_url="https://console.example", clock=float)
    return service, experiment, trajectory_store, review, monitor


def store(trajectory_store, record):
    writer = trajectory_store.start_episode(record.model_copy(update={"reward": None, "finished_at": None}), {}, {})
    writer.finish(record.reward, {})


def test_the_monitor_raises_files_records_and_delivers_each_alert_once(example_payload, tmp_path):
    channel = RecordingChannel()
    quiet = RecordingChannel(name="email", min_severity="critical")
    service, experiment, trajectory_store, review, monitor = launched_monitor(example_payload, tmp_path,
                                                                             [channel, quiet])
    run_id = experiment.run_ids[0]
    for index in range(4):
        store(trajectory_store, finished(index, "task_a", graded(0.0, "failure"), run_id=run_id))
    for index in range(4, 8):
        store(trajectory_store, finished(index, "task_a", graded(1.0), run_id=run_id))
    alerts = monitor.check()
    assert [alert.kind for alert in alerts] == ["reward_spike"]
    spike = alerts[0]
    assert spike.run_id == run_id and spike.experiment_id == experiment.experiment_id
    assert spike.deliveries == {"record": "sent"}
    item = review.item(spike.review_item_id)
    assert item.source == "alert" and item.episode_ids == spike.episode_ids
    assert item.payload["alert_id"] == spike.alert_id
    assert f"https://console.example/runs/{run_id}" in channel.sent[0][1]
    assert [event.detail["kind"] for event in service.run_events(run_id) if event.kind == "alert"] == ["reward_spike"]
    assert monitor.check() == []

    service.record_spend(run_id, 60.0)
    budget = monitor.check()
    assert [(alert.kind, alert.subject, alert.run_id) for alert in budget] == [("budget_threshold", "0.5", "")]
    sibling_events = [event for event in service.run_events(experiment.run_ids[1]) if event.kind == "alert"]
    assert sibling_events[0].detail["subject"] == "0.5"

    service.mark_finished(run_id, "failed", "trainer crashed")
    completion = monitor.check()
    assert [(alert.kind, alert.severity) for alert in completion] == [("run_completion", "critical")]
    assert [kind for kind, _ in quiet.sent] == ["run_completion"]
    store(trajectory_store, finished(99, "task_b", graded(0.0, "env_error"), run_id=run_id))
    assert monitor.check() == []
    assert len(monitor.alerts(run_id=run_id)) == 2 and len(monitor.alerts(experiment_id=experiment.experiment_id)) == 3


def test_failed_deliveries_name_only_the_error_type(example_payload, tmp_path):
    channel = RecordingChannel(failure=AlertDeliveryError("https://hooks.slack.com/services/T0/B0/secret"))
    service, experiment, _, _, monitor = launched_monitor(example_payload, tmp_path, [channel])
    service.mark_finished(experiment.run_ids[0], "completed")
    alerts = monitor.check()
    assert alerts[0].deliveries == {"record": "failed: AlertDeliveryError"}
    assert "secret" not in json.dumps([alert.model_dump(mode="json") for alert in alerts])


def test_acknowledging_alerts(example_payload, tmp_path):
    service, experiment, _, _, monitor = launched_monitor(example_payload, tmp_path)
    service.mark_finished(experiment.run_ids[0], "completed")
    alert = monitor.check()[0]
    acknowledged = monitor.acknowledge(alert.alert_id, "researcher@example.com")
    assert acknowledged.status == "acknowledged" and acknowledged.acknowledged_by == "researcher@example.com"
    assert monitor.alerts(status="open") == [] and monitor.alerts(status="acknowledged") == [acknowledged]


class SlackHook:
    def __init__(self, status_code=200):
        self.status_code = status_code
        self.payloads = []

    def __call__(self, request):
        self.payloads.append((str(request.url), json.loads(request.content)))
        return httpx.Response(self.status_code)


def sample_alert(severity="warning"):
    return Alert(alert_id="alert_000001", kind="reward_spike", severity=severity, experiment_id="experiment_000001",
                 run_id="run_1", tenant="default", subject="task_a", title="reward on task_a jumped", raised_at=0.0)


def test_slack_posts_the_text_to_its_webhook():
    hook = SlackHook()
    webhook_client = httpx.Client(transport=httpx.MockTransport(hook))
    channel = SlackChannel("https://hooks.slack.com/services/T0/B0/x", webhook_client)
    channel.send(sample_alert(), "[warning] reward on task_a jumped")
    assert hook.payloads == [("https://hooks.slack.com/services/T0/B0/x",
                              {"text": "[warning] reward on task_a jumped"})]
    refusing = SlackChannel("https://hooks.slack.com/services/T0/B0/x",
                            httpx.Client(transport=httpx.MockTransport(SlackHook(404))))
    with pytest.raises(AlertDeliveryError, match="slack answered 404"):
        refusing.send(sample_alert(), "text")


class FakeSmtp:
    sessions = []

    def __init__(self, host, port, timeout):
        self.host = host
        self.port = port
        self.calls = []
        FakeSmtp.sessions.append(self)

    def __enter__(self):
        return self

    def __exit__(self, *exc_info):
        return False

    def starttls(self):
        self.calls.append("starttls")

    def login(self, username, password):
        self.calls.append(f"login {username}")

    def send_message(self, message):
        self.calls.append(message)


def test_email_sends_one_message_over_tls():
    FakeSmtp.sessions.clear()
    channel = EmailChannel("smtp.example.com", "alerts@example.com", ["oncall@example.com", "lead@example.com"],
                           username="alerts", password="app password", connect=FakeSmtp)
    channel.send(sample_alert("critical"), "[critical] reward on task_a jumped")
    session = FakeSmtp.sessions[0]
    assert (session.host, session.port) == ("smtp.example.com", 587)
    assert session.calls[:2] == ["starttls", "login alerts"]
    message = session.calls[2]
    assert message["Subject"] == "[rlworld critical] reward on task_a jumped"
    assert message["To"] == "oncall@example.com, lead@example.com"
    with pytest.raises(ValueError, match="needs recipients"):
        EmailChannel("smtp.example.com", "alerts@example.com", [])


def test_alert_routes(example_payload, tmp_path):
    service, experiment, _, _, monitor = launched_monitor(example_payload, tmp_path)
    service.mark_finished(experiment.run_ids[0], "completed")
    alert = monitor.check()[0]
    client = httpx.Client(transport=httpx.MockTransport(FlaskBridge(create_app(service, alert_monitor=monitor))),
                          base_url="http://experimentd.internal")
    listed = client.get("/v1/alerts", params={"run_id": experiment.run_ids[0]}).json()["alerts"]
    assert [entry["alert_id"] for entry in listed] == [alert.alert_id]
    acknowledged = client.post(f"/v1/alerts/{alert.alert_id}/acknowledge")
    assert acknowledged.status_code == 200 and acknowledged.json()["status"] == "acknowledged"
    assert client.post("/v1/alerts/alert_999999/acknowledge").status_code == 404
    unconfigured = httpx.Client(transport=httpx.MockTransport(FlaskBridge(create_app(service))),
                                base_url="http://experimentd.internal")
    assert unconfigured.get("/v1/alerts").status_code == 503


class FlakyMonitor(AlertMonitor):
    def __init__(self, stop, *arguments, **options):
        super().__init__(*arguments, **options)
        self.stop = stop
        self.checks = 0

    def check(self):
        self.checks += 1
        if self.checks == 1:
            raise RuntimeError("trajectory store hiccup")
        self.stop.set()
        return []


def test_the_background_loop_survives_a_failing_check(example_payload, tmp_path):
    service, _, trajectory_store, _, _ = launched_monitor(example_payload, tmp_path)
    stop = threading.Event()
    monitor = FlakyMonitor(stop, service, TrajectoryViews(trajectory_store))
    loop = threading.Thread(target=monitor.run_forever, args=(stop, 0.001), daemon=True)
    loop.start()
    loop.join(timeout=5)
    assert not loop.is_alive() and monitor.checks == 2
