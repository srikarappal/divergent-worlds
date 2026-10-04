import threading
from pathlib import Path

import httpx
import pytest
import yaml
from pydantic import ValidationError
from support.rollout_fakes import FakeFleet
from werkzeug.serving import make_server

from rlworld.gateway.config import load_endpoint_registry
from rlworld.gateway.openai_compatible import parse_response
from rlworld.gateway.secrets import DevelopmentVault, VaultSecretStore
from rlworld.images.linux import dev_image_tag
from rlworld.services.experimentd_main import (
    BackgroundJob,
    ConfigError,
    ExperimentdConfig,
    build_experimentd,
    load_experimentd_config,
    main,
    run_job,
    serve,
)
from rlworld.specs import dump_spec_document, load_spec_document, parse_spec_document, spec_digest
from rlworld.tasks.osworld import TEMPLATE_NAME, ImportedSuite, SuiteAssets, SuiteTask, TaskReads, save_suite
from rlworld.tasks.osworld_evaluation import Evaluation, Getter, MetricCall

EXAMPLES = Path(__file__).resolve().parents[3] / "examples"
LAPTOP = EXAMPLES / "experimentd" / "laptop.yaml"
REFERENCE_SPEC = EXAMPLES / "specs" / "reconcile_invoices_linux.yaml"
OSWORLD_SPEC = EXAMPLES / "specs" / "suite_osworld_eval.yaml"
SECRETS = {"RLWORLD_RUN_TOKEN_SECRET": "run token secret", "RLWORLD_JUDGE_SECRET": "judge secret"}
SNAPSHOT_STORE_FLEET_DIGEST = "sha256:" + "5e" * 32
WORLD_SNAPSHOT_DIGEST = "sha256:" + "3c" * 32


class ScriptedTransport:
    """An OpenAI compatible endpoint that always finishes at once and answers probes."""

    def __init__(self):
        self.requests = []
        self.lock = threading.Lock()

    def send(self, endpoint, api_key, request):
        with self.lock:
            self.requests.append((endpoint.name, request))
        return parse_response({"choices": [{"message": {"content": "Action: finished()"}, "finish_reason": "stop"}],
                               "usage": {"prompt_tokens": 50, "completion_tokens": 3}, "model": request.model},
                              request.model)


class RecordingLaunches:
    def __init__(self):
        self.submitted = []

    def run(self, argv, working_directory):
        self.submitted.append(argv)
        return "job submitted"


class SnapshotStoreFleet(FakeFleet):
    """The fake fleet with fleetd's snapshot index, pins and prefetch."""

    def __init__(self):
        super().__init__()
        self.pins = []

    def current_snapshot(self, image_digest, pool):
        return {"image": "noble_calc_v1", "image_digest": image_digest, "pool": pool,
                "snapshot": SNAPSHOT_STORE_FLEET_DIGEST, "updated_at": "2026-10-03T12:00:00Z"}

    def current_snapshots(self):
        return []

    def pin_snapshot(self, digest, holder):
        self.pins.append((digest, holder))
        return {"snapshot": digest, "holder": holder}

    def prefetch_snapshot(self, digest, pool, envs, memory_mib, cpu_millicores, gpus=0):
        return ["laptop"]

    def prepare_world(self, base_digest, world_digest, payload, apps, pool, memory_mib, cpu_millicores, vcpus):
        """This world loads slowly, so fleetd snapshotted it."""
        return {"base": base_digest, "world": world_digest, "snapshot": WORLD_SNAPSHOT_DIGEST}


def laptop_config(tmp_path, **changes):
    """The laptop example with its stores under tmp_path and any changes validated like the file would be."""
    config = load_experimentd_config(LAPTOP)
    return ExperimentdConfig.model_validate({**config.model_dump(), "state_dir": tmp_path / "state",
                                             "object_store_dir": tmp_path / "objects", **changes})


def small_reference_spec():
    payload = yaml.safe_load(REFERENCE_SPEC.read_text())
    payload["world"]["size"] = {"employees": 6, "customers": 6, "invoices_per_customer": 3, "emails_per_person": 2}
    payload["run"]["tasks_per_batch"] = 2
    return dump_spec_document(parse_spec_document(payload))


def test_the_laptop_example_pairs_with_the_dev_backend_and_the_reference_spec():
    config = load_experimentd_config(LAPTOP)
    image = load_spec_document(REFERENCE_SPEC.read_text()).image
    snapshot = config.static_snapshots[spec_digest(image)]
    assert snapshot.base_disk_path == dev_image_tag(image) and not config.snapshot_store
    assert config.pools[image.pool.vmm].pool == "linux" and config.fleet_url == "http://127.0.0.1:8700"
    assert config.endpoints_file == LAPTOP.parent / "endpoints.yaml"
    policy = load_spec_document(REFERENCE_SPEC.read_text()).run.policy
    assert load_endpoint_registry(config.endpoints_file).resolve(policy.endpoint).name == "local_policy"


def test_configs_refuse_unknown_fields_and_secrets_come_only_from_the_environment(tmp_path):
    path = tmp_path / "experimentd.yaml"
    path.write_text(yaml.safe_dump({"public_url": "http://x", "fleet_url": "http://f", "state_dir": "state",
                                    "object_store_dir": "objects", "pools": {"firecracker": {
                                        "pool": "linux", "memory_mib": 2048, "vcpus": 2}},
                                    "run_token_secret": "inline"}))
    with pytest.raises(ValidationError, match="run_token_secret"):
        load_experimentd_config(path)
    path.write_text(yaml.safe_dump({"public_url": "http://x", "fleet_url": "http://f", "state_dir": "state",
                                    "object_store_dir": "objects", "pools": {"firecracker": {
                                        "pool": "linux", "memory_mib": 2048, "vcpus": 2}}}))
    config = load_experimentd_config(path)
    assert config.state_dir == tmp_path / "state" and config.endpoints_file is None
    with pytest.raises(ConfigError, match="RLWORLD_RUN_TOKEN_SECRET"):
        build_experimentd(config, {})
    with pytest.raises(ConfigError, match="RLWORLD_JUDGE_SECRET"):
        build_experimentd(config, {"RLWORLD_RUN_TOKEN_SECRET": "x"})
    vaulted = ExperimentdConfig.model_validate({**config.model_dump(), "vault": {"url": "https://vault.internal"}})
    with pytest.raises(ConfigError, match="VAULT_TOKEN"):
        build_experimentd(vaulted, SECRETS)


def test_experimentd_plays_an_eval_run_from_spec_to_episodes(tmp_path):
    fleet, transport, launches = FakeFleet(), ScriptedTransport(), RecordingLaunches()
    experimentd = build_experimentd(laptop_config(tmp_path), SECRETS, fleet=fleet, transport=transport,
                                    launch_runner=launches)
    client = experimentd.app.test_client()
    assert client.get("/healthz").get_json() == {"status": "ok"}
    digest = client.post("/v1/specs", data=small_reference_spec(), content_type="application/yaml").get_json()[
        "digest"]
    preflight = client.post(f"/v1/specs/{digest}/preflight").get_json()
    point = next(iter(preflight["points"].values()))
    assert "ungated_task" in [issue["code"] for issue in point["issues"]]
    assert any(request.purpose == "probe" for _, request in transport.requests)
    launched = client.post(f"/v1/specs/{digest}/launch", json={"budget_usd": 10.0})
    assert launched.status_code == 201
    run_id = launched.get_json()["run_ids"][0]
    assert experimentd.executor.wait(run_id, timeout_seconds=120)
    assert client.get(f"/v1/runs/{run_id}").get_json()["state"] == "completed"
    episodes = client.get(f"/v1/runs/{run_id}/episodes", query_string={"status": "finished"}).get_json()["episodes"]
    assert len(episodes) == 2 and {episode["reward"]["termination"] for episode in episodes} == {"failure"}
    policy_calls = [request for name, request in transport.requests if request.purpose == "policy"]
    assert {(request.run_id, request.model) for request in policy_calls} == {(run_id, "my_policy")}
    leases = fleet.calls_named("lease_group")
    assert leases and {call[3] for call in leases} == {"sha256:e197927b25a48f8a2a938f4eea30f6665e42a8086f0cd1340cc"
                                                         "2a33816dfbf0b"}
    assert experimentd.meter.summary(run_id)["local_policy"].calls == len(policy_calls)
    assert launches.submitted == [] and experimentd.gate_reruns is None
    assert {job.name for job in experimentd.jobs} == {"alerts", "retention"}
    catalog = client.get("/v1/catalogs/task_templates").get_json()
    assert catalog["kind"] == "task_templates"


def test_a_snapshot_store_turns_on_pins_prefetch_world_snapshots_and_gate_reruns(tmp_path):
    fleet = SnapshotStoreFleet()
    config = laptop_config(tmp_path, snapshot_store=True, static_snapshots={})
    experimentd = build_experimentd(config, SECRETS, fleet=fleet, transport=ScriptedTransport(),
                                    launch_runner=RecordingLaunches())
    assert experimentd.gate_reruns is not None
    assert {job.name for job in experimentd.jobs} == {"alerts", "retention", "gate_reruns"}
    client = experimentd.app.test_client()
    digest = client.post("/v1/specs", data=small_reference_spec(), content_type="application/yaml").get_json()[
        "digest"]
    run_id = client.post(f"/v1/specs/{digest}/launch", json={"budget_usd": 10.0}).get_json()["run_ids"][0]
    assert experimentd.executor.wait(run_id, timeout_seconds=120)
    assert fleet.pins == [(SNAPSHOT_STORE_FLEET_DIGEST, f"eval_report:{run_id}"),
                          (WORLD_SNAPSHOT_DIGEST, f"eval_report:{run_id}")]
    assert {call[3] for call in fleet.calls_named("lease_group")} == {WORLD_SNAPSHOT_DIGEST}
    injected = [call[2] for call in fleet.calls_named("inject")]
    assert injected and all(len(payload["world"]["emails"]) < 10 for payload in injected)
    assert experimentd.gate_reruns.poll() == []


def notes_suite(root):
    """One imported OSWorld style task: a notes file set up on the desktop, compared with a fetched gold file."""
    assets = SuiteAssets(root)
    notes, gold = assets.store(b"draft notes\n"), assets.store(b"final notes\n")
    metric = MetricCall(func="compare_text_file", result=Getter(kind="vm_file", paths=("Desktop/notes.txt",)),
                        expected=Getter(kind="cloud_file", assets=(gold,)))
    task = SuiteTask(id="w1", domain="libreoffice_writer", instruction="Tidy the notes.",
                     setup=({"kind": "file", "path": "Desktop/notes.txt", "asset": notes},),
                     reads=TaskReads(files=("Desktop/notes.txt",)), evaluation=Evaluation(metrics=(metric,)))
    save_suite(root, ImportedSuite(tasks=(task,)))


def test_an_imported_osworld_suite_runs_as_an_eval_run(tmp_path):
    config_path = tmp_path / "config" / "experimentd.yaml"
    config_path.parent.mkdir()
    notes_suite(config_path.parent / "osworld_suite")
    payload = load_experimentd_config(LAPTOP).model_dump(mode="json")
    image = load_spec_document(OSWORLD_SPEC.read_text()).image
    payload["static_snapshots"][spec_digest(image)] = {"digest": "sha256:" + "8b" * 32,
                                                       "base_disk_path": "rlworld-dev/noble_osworld_calc_v1:8b"}
    payload.update({"state_dir": str(tmp_path / "state"), "object_store_dir": str(tmp_path / "objects"),
                    "endpoints_file": str(LAPTOP.parent / "endpoints.yaml"), "osworld_suite": "osworld_suite"})
    config_path.write_text(yaml.safe_dump(payload))
    config = load_experimentd_config(config_path)
    assert config.osworld_suite == config_path.parent / "osworld_suite"
    fleet = FakeFleet()
    experimentd = build_experimentd(config, SECRETS, fleet=fleet, transport=ScriptedTransport(),
                                    launch_runner=RecordingLaunches())
    assert TEMPLATE_NAME in experimentd.workers.registry.templates
    client = experimentd.app.test_client()
    verifiers = client.get("/v1/catalogs/verifiers").get_json()["entries"]
    assert "verifiers.public.osworld" in {entry["name"] for entry in verifiers}
    digest = client.post("/v1/specs", data=OSWORLD_SPEC.read_text(), content_type="application/yaml").get_json()[
        "digest"]
    run_id = client.post(f"/v1/specs/{digest}/launch", json={"budget_usd": 10.0}).get_json()["run_ids"][0]
    assert experimentd.executor.wait(run_id, timeout_seconds=120)
    assert client.get(f"/v1/runs/{run_id}").get_json()["state"] == "completed"
    episodes = client.get(f"/v1/runs/{run_id}/episodes", query_string={"status": "finished"}).get_json()["episodes"]
    assert len(episodes) == 1 and episodes[0]["reward"]["termination"] == "failure"
    injected = [call[2] for call in fleet.calls_named("inject")]
    assert injected[0]["setup"] == [{"kind": "file", "path": "Desktop/notes.txt",
                                     "base64": "ZHJhZnQgbm90ZXMK"}]
    assert injected[0]["reads"] == {"before": [], "files": ["Desktop/notes.txt"], "commands": []}


def test_sign_in_vault_and_alert_channels_follow_the_config(tmp_path):
    config = laptop_config(tmp_path, oidc={"issuer": "https://sso.example.com/realms/platform",
                                           "audience": "rlworld", "client_id": "console"},
                           vault={"url": "https://vault.internal"},
                           alerts={"slack": {"webhook_env": "SLACK_WEBHOOK"},
                                   "email": {"smtp_host": "smtp.example.com", "sender": "rlworld@example.com",
                                             "recipients": ["oncall@example.com"]}})
    environment = {**SECRETS, "RLWORLD_FRAME_GRANT_SECRET": "grants", "VAULT_TOKEN": "s.token",
                   "SLACK_WEBHOOK": "https://hooks.slack.example/T0/B0/x"}
    experimentd = build_experimentd(config, environment, fleet=FakeFleet(), transport=ScriptedTransport())
    client = experimentd.app.test_client()
    assert client.get("/v1/auth/config").get_json()["issuer"] == "https://sso.example.com/realms/platform"
    assert client.get("/v1/runs").status_code == 401
    assert isinstance(experimentd.gateway.secret_store, VaultSecretStore)
    assert [channel.name for channel in experimentd.alert_monitor.channels] == ["slack", "email"]
    with pytest.raises(ConfigError, match="SLACK_WEBHOOK"):
        build_experimentd(config, {**SECRETS, "RLWORLD_FRAME_GRANT_SECRET": "g", "VAULT_TOKEN": "t"},
                          fleet=FakeFleet(), transport=ScriptedTransport())
    plain = build_experimentd(laptop_config(tmp_path / "plain"), SECRETS, fleet=FakeFleet(),
                              transport=ScriptedTransport())
    assert isinstance(plain.gateway.secret_store, DevelopmentVault)
    assert plain.app.test_client().get("/v1/runs").status_code == 200


class FlakyWork:
    """Fails on its first round, then asks the job to stop."""

    def __init__(self, stop):
        self.stop = stop
        self.rounds = 0

    def work(self):
        self.rounds += 1
        if self.rounds == 1:
            raise RuntimeError("the fleet is restarting")
        self.stop.set()


def test_background_jobs_survive_a_failing_round(caplog):
    stop = threading.Event()
    flaky = FlakyWork(stop)
    run_job(BackgroundJob("alerts", flaky.work, 0.001), stop)
    assert flaky.rounds == 2 and "background job alerts failed" in caplog.text


def test_experimentd_serves_until_shut_down(tmp_path):
    experimentd = build_experimentd(laptop_config(tmp_path), SECRETS, fleet=FakeFleet(), transport=ScriptedTransport())
    server = make_server("127.0.0.1", 0, experimentd.app, threaded=True)
    serving = threading.Thread(target=serve, args=(experimentd, server))
    serving.start()
    response = httpx.get(f"http://127.0.0.1:{server.server_port}/healthz", timeout=10)
    server.shutdown()
    serving.join(timeout=10)
    assert response.json() == {"status": "ok"} and not serving.is_alive()


def test_main_says_why_it_cannot_start(tmp_path, capsys):
    assert main(["--config", str(tmp_path / "missing.yaml")]) == 1
    assert "experimentd cannot start" in capsys.readouterr().err
