"""The experimentd command: the experiment API beside fleetd (DESIGN.md sections 5 and 6.7).

A YAML config names fleetd, the object store it shares with fleetd, the
endpoint registry, rubrics, trainer templates, the pool shape per VMM, sign in
and the background jobs; secrets come from the environment variables it names
or from Vault, never from the file. experimentd wires the spec, run and catalog
stores, review, admin, alerts, takeovers and task authoring onto one app; plays
eval and data runs, dry runs and the strong agent with platform hosted rollout
workers; launches RL trainers; pins and prefetches each run's snapshot; and
reruns task gates when an image's snapshot changes. An imported OSWorld suite,
when the config names one, registers beside the built in task templates.
"""

import argparse
import logging
import os
import signal
import sys
import threading
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

import httpx
import yaml
from flask import Flask
from pydantic import BaseModel, ConfigDict, Field, PositiveFloat, PositiveInt, ValidationError
from werkzeug.serving import BaseWSGIServer, make_server

from rlworld.analysis.failures import LlmFailureLabeler
from rlworld.entitlements import EntitlementBroker, EphemeralSecretIssuer, IdentityPoolConfig, SeatPoolConfig
from rlworld.entitlements.credentials import SignedSessionMinter
from rlworld.gateway.cache import FileResponseCache
from rlworld.gateway.clients import GatewayEndpointProbe, GatewayJudgeClient
from rlworld.gateway.config import EndpointRegistry, load_endpoint_registry
from rlworld.gateway.gateway import ModelGateway, Transport
from rlworld.gateway.metering import UsageMeter
from rlworld.gateway.openai_compatible import BlobImageResolver, OpenAICompatibleTransport
from rlworld.gateway.secrets import DevelopmentVault, SecretVault, VaultSecretStore
from rlworld.judge.judge import RubricJudge
from rlworld.judge.prompt import JudgePromptBuilder
from rlworld.judge.rubric import RubricLibrary
from rlworld.preflight.preflight import PreflightContext, SeatPool
from rlworld.rewards.pipeline import JudgeTier
from rlworld.rewards.verifiers import VerifierRegistry
from rlworld.rollout.groups import EnvShape
from rlworld.rollout.platform import PlatformSolvabilityProbe, PlatformWorkers, SimulatorEndpoint
from rlworld.rollout.worker import PlatformRunExecutor
from rlworld.sdk.client import FleetClient, SnapshotSource
from rlworld.services.admin import DataPolicyStore, PlatformAuditLog
from rlworld.services.alerts import AlertChannel, AlertMonitor, EmailChannel, Severity, SlackChannel
from rlworld.services.auth import DEFAULT_TENANT, FrameGrants, HttpJwks, OidcSettings, OidcVerifier, PlatformAuth
from rlworld.services.authoring import AuthoringService
from rlworld.services.catalogs import CatalogService, EndpointService
from rlworld.services.experimentd_http import create_app
from rlworld.services.experiments import ExperimentService
from rlworld.services.gate_reports import GateReports
from rlworld.services.gate_reruns import GateReruns
from rlworld.services.retention import RetentionSweeper
from rlworld.services.review import ReviewService
from rlworld.services.run_snapshots import RunSnapshots
from rlworld.services.takeover import TakeoverService
from rlworld.services.trainer_launch import (
    CommandRunner,
    SubprocessCommandRunner,
    TrainerLauncher,
    TrainerTemplate,
    load_trainer_templates,
)
from rlworld.services.trajectory_views import TrajectoryViews
from rlworld.specs.models import EndpointSpec
from rlworld.tasks.osworld import OSWorldSuite
from rlworld.tasks.registry import (
    FleetSnapshots,
    FleetWorlds,
    SnapshotResolver,
    StaticSnapshots,
    default_task_registry,
)
from rlworld.trajectories.object_store import FilesystemObjectStore
from rlworld.trajectories.trajectory_store import TrajectoryStore

LOGGER = logging.getLogger(__name__)
GATEWAY_TIMEOUT_SECONDS = 600.0
# Deciding how a new world loads on a base snapshot restores and measures envs, which takes minutes.
FLEET_TIMEOUT_SECONDS = 900.0
CONFIG_PATH_FIELDS = ("state_dir", "object_store_dir", "endpoints_file", "rubrics_dir", "trainer_templates_file",
                      "osworld_suite")


class ConfigError(ValueError):
    """The config or the environment it points into cannot start experimentd."""


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class PoolShapeConfig(StrictModel):
    """The pool and per env resources leases of one VMM's images take."""

    pool: str = Field(min_length=1)
    memory_mib: PositiveInt
    vcpus: PositiveInt
    cpu_millicores: PositiveInt = 2000
    host_scoped_licenses: list[str] = Field(default_factory=list)


class SnapshotConfig(StrictModel):
    """A snapshot by its files, for hosts no snapshot store serves, such as the Docker dev backend."""

    digest: str = Field(min_length=1)
    state_path: str = ""
    memory_path: str = ""
    base_disk_path: str = ""


class OidcConfig(StrictModel):
    issuer: str
    audience: str
    client_id: str
    role_claim: str = "roles"
    tenant_claim: str = "tenant"
    scopes: tuple[str, ...] = ("openid", "profile", "email")
    frame_grant_secret_env: str = "RLWORLD_FRAME_GRANT_SECRET"


class VaultConfig(StrictModel):
    url: str
    mount: str = "secret"
    path_prefix: str = "rlworld/endpoints"
    token_env: str = "VAULT_TOKEN"


class SlackConfig(StrictModel):
    webhook_env: str
    min_severity: Severity = "info"


class EmailConfig(StrictModel):
    smtp_host: str
    sender: str
    recipients: list[str] = Field(min_length=1)
    smtp_port: PositiveInt = 587
    username_env: str | None = None
    password_env: str | None = None
    min_severity: Severity = "warning"


class AlertsConfig(StrictModel):
    slack: SlackConfig | None = None
    email: EmailConfig | None = None
    console_url: str = ""
    interval_seconds: PositiveFloat = 60.0


class ModelConfig(StrictModel):
    endpoint: str
    model: str


class SeatPoolEntry(StrictModel):
    name: str
    capacity: PositiveInt
    scope: Literal["per_env", "per_host"] = "per_env"
    issues_token: bool = False


class IdentityPoolEntry(StrictModel):
    persona_key: str
    accounts: list[str] = Field(min_length=1)


class EntitlementsConfig(StrictModel):
    seat_pools: list[SeatPoolEntry] = Field(default_factory=list)
    identity_pools: list[IdentityPoolEntry] = Field(default_factory=list)
    cookie_domain: str = "example.com"
    signing_key_env: str = "RLWORLD_SESSION_SIGNING_KEY"


class ExperimentdConfig(StrictModel):
    host: str = "127.0.0.1"
    port: int = Field(default=8080, ge=0, le=65_535)
    public_url: str
    fleet_url: str
    state_dir: Path
    object_store_dir: Path
    endpoints_file: Path | None = None
    rubrics_dir: Path | None = None
    trainer_templates_file: Path | None = None
    osworld_suite: Path | None = None
    approval_threshold_usd: float | None = None
    pools: dict[str, PoolShapeConfig] = Field(min_length=1)
    snapshot_store: bool = True
    static_snapshots: dict[str, SnapshotConfig] = Field(default_factory=dict)
    run_token_secret_env: str = "RLWORLD_RUN_TOKEN_SECRET"
    judge_secret_env: str = "RLWORLD_JUDGE_SECRET"
    vault: VaultConfig | None = None
    oidc: OidcConfig | None = None
    alerts: AlertsConfig = Field(default_factory=AlertsConfig)
    user_simulator: ModelConfig | None = None
    strong_agent: EndpointSpec | None = None
    gate_judge: ModelConfig | None = None
    failure_labeler: ModelConfig | None = None
    entitlements: EntitlementsConfig | None = None
    gate_rerun_interval_seconds: PositiveFloat = 300.0
    retention_interval_hours: PositiveFloat = 24.0


def load_experimentd_config(config_path: Path) -> ExperimentdConfig:
    """Relative paths in the file are relative to it."""
    raw_config = yaml.safe_load(config_path.read_text()) or {}
    for field_name in CONFIG_PATH_FIELDS:
        if raw_config.get(field_name):
            raw_config[field_name] = str(config_path.parent / raw_config[field_name])
    return ExperimentdConfig.model_validate(raw_config)


def required_secret(environment: Mapping[str, str], variable_name: str) -> str:
    secret_value = environment.get(variable_name, "")
    if not secret_value:
        raise ConfigError(f"set {variable_name}; experimentd keeps no secrets in its config")
    return secret_value


@dataclass(frozen=True)
class BackgroundJob:
    name: str
    work: Callable[[], Any]
    interval_seconds: float


def run_job(job: BackgroundJob, stop: threading.Event) -> None:
    """Runs a job every interval until stopped; a failure is logged and the next round tries again."""
    while not stop.wait(job.interval_seconds):
        try:
            job.work()
        except Exception:  # a background job failing never takes the API down
            LOGGER.exception("background job %s failed", job.name)


@dataclass
class Experimentd:
    app: Flask
    experiment_service: ExperimentService
    executor: PlatformRunExecutor
    workers: PlatformWorkers
    gateway: ModelGateway
    meter: UsageMeter
    trajectory_store: TrajectoryStore
    review_service: ReviewService
    alert_monitor: AlertMonitor
    authoring_service: AuthoringService
    gate_reports: GateReports
    gate_reruns: GateReruns | None
    retention_sweeper: RetentionSweeper
    jobs: tuple[BackgroundJob, ...]


def secret_vault(config: ExperimentdConfig, environment: Mapping[str, str],
                 http_client: httpx.Client | None) -> SecretVault:
    if config.vault is None:
        return DevelopmentVault()
    return VaultSecretStore(config.vault.url, required_secret(environment, config.vault.token_env),
                            config.vault.mount, config.vault.path_prefix, http_client)


def pool_shapes(config: ExperimentdConfig) -> dict[str, EnvShape]:
    return {vmm: EnvShape(pool=shape.pool, memory_mib=shape.memory_mib, vcpus=shape.vcpus,
                          cpu_millicores=shape.cpu_millicores,
                          host_scoped_licenses=tuple(shape.host_scoped_licenses))
            for vmm, shape in config.pools.items()}


def snapshot_resolver(config: ExperimentdConfig, fleet: FleetClient) -> SnapshotResolver:
    if config.snapshot_store:
        return FleetSnapshots(fleet, {vmm: shape.pool for vmm, shape in config.pools.items()})
    return StaticSnapshots({image_digest: SnapshotSource(digest=snapshot.digest, state_path=snapshot.state_path,
                                                         memory_path=snapshot.memory_path,
                                                         base_disk_path=snapshot.base_disk_path)
                            for image_digest, snapshot in config.static_snapshots.items()})


def entitlement_broker(config: ExperimentdConfig, environment: Mapping[str, str]) -> EntitlementBroker | None:
    settings = config.entitlements
    if settings is None:
        return None
    minter = SignedSessionMinter(required_secret(environment, settings.signing_key_env).encode(),
                                 settings.cookie_domain, time.time)
    return EntitlementBroker(
        seat_pools=[SeatPoolConfig(pool.name, pool.capacity, pool.scope, pool.issues_token)
                    for pool in settings.seat_pools],
        identity_pools=[IdentityPoolConfig(pool.persona_key, tuple(pool.accounts))
                        for pool in settings.identity_pools],
        session_minter=minter, secret_issuer=EphemeralSecretIssuer(time.time))


def alert_channels(config: ExperimentdConfig, environment: Mapping[str, str],
                   http_client: httpx.Client | None) -> list[AlertChannel]:
    channels: list[AlertChannel] = []
    alerts = config.alerts
    if alerts.slack is not None:
        channels.append(SlackChannel(required_secret(environment, alerts.slack.webhook_env), http_client,
                                     min_severity=alerts.slack.min_severity))
    if alerts.email is not None:
        email = alerts.email
        username = environment.get(email.username_env) if email.username_env else None
        password = environment.get(email.password_env) if email.password_env else None
        channels.append(EmailChannel(email.smtp_host, email.sender, email.recipients, email.smtp_port, username,
                                     password, min_severity=email.min_severity))
    return channels


def trainer_templates(config: ExperimentdConfig) -> dict[str, TrainerTemplate]:
    return load_trainer_templates(config.trainer_templates_file) if config.trainer_templates_file else {}


def platform_auth(config: ExperimentdConfig, environment: Mapping[str, str],
                  http_client: httpx.Client | None) -> PlatformAuth | None:
    if config.oidc is None:
        return None
    oidc = config.oidc
    settings = OidcSettings(issuer=oidc.issuer, audience=oidc.audience, client_id=oidc.client_id,
                            role_claim=oidc.role_claim, tenant_claim=oidc.tenant_claim, scopes=oidc.scopes)
    grants = FrameGrants(required_secret(environment, oidc.frame_grant_secret_env).encode())
    return PlatformAuth(OidcVerifier(settings, HttpJwks(oidc.issuer, http_client)), grants)


def default_tenant(run_id: str) -> str:
    """Whose run this is until the experiment service, which knows, is wired in."""
    return DEFAULT_TENANT


def build_experimentd(config: ExperimentdConfig, environment: Mapping[str, str], fleet: FleetClient | None = None,
                      transport: Transport | None = None, launch_runner: CommandRunner | None = None,
                      http_client: httpx.Client | None = None) -> Experimentd:
    """Every store and service wired together; nothing listens or runs until serve."""
    run_token_secret = required_secret(environment, config.run_token_secret_env).encode()
    judge_secret = required_secret(environment, config.judge_secret_env).encode()
    for directory in (config.state_dir, config.object_store_dir):
        directory.mkdir(parents=True, exist_ok=True)
    object_store = FilesystemObjectStore(config.object_store_dir)
    trajectory_store = TrajectoryStore(object_store)
    views = TrajectoryViews(trajectory_store)
    registry = load_endpoint_registry(config.endpoints_file) if config.endpoints_file else EndpointRegistry([])
    vault = secret_vault(config, environment, http_client)
    meter = UsageMeter()
    policies = DataPolicyStore()
    transport = transport or OpenAICompatibleTransport(httpx.Client(timeout=GATEWAY_TIMEOUT_SECONDS),
                                                       BlobImageResolver(object_store))
    gateway = ModelGateway(registry, transport, vault, response_cache=FileResponseCache(config.state_dir / "responses"),
                           meter=meter, screenshot_policy=policies)
    verifier_registry = VerifierRegistry()
    task_registry = default_task_registry(verifier_registry)
    if config.osworld_suite:
        task_registry.register(OSWorldSuite(config.osworld_suite))
    rubric_library = RubricLibrary(config.rubrics_dir) if config.rubrics_dir else None
    fleet = fleet or FleetClient(config.fleet_url, timeout_seconds=FLEET_TIMEOUT_SECONDS)
    shapes = pool_shapes(config)
    snapshots = snapshot_resolver(config, fleet)
    worlds = FleetWorlds(fleet, shapes) if config.snapshot_store else None
    gate_reports = GateReports()
    probe = GatewayEndpointProbe(gateway, registry)
    broker = entitlement_broker(config, environment)
    seat_pools = {pool.name: SeatPool(pool.capacity, pool.scope)
                  for pool in (config.entitlements.seat_pools if config.entitlements else [])}
    frontier = frozenset({"frontier", *(endpoint.name for endpoint in registry.endpoints()
                                        if endpoint.provider_kind == "frontier")})
    preflight_context = PreflightContext(verifier_registry=verifier_registry, rubric_library=rubric_library,
                                         gate_reports=gate_reports, seat_pools=seat_pools, endpoint_probe=probe,
                                         frontier_endpoints=frontier)
    simulator = (SimulatorEndpoint(config.user_simulator.endpoint, config.user_simulator.model)
                 if config.user_simulator else None)
    workers = PlatformWorkers(fleet, gateway, trajectory_store, task_registry, snapshots, shapes, default_tenant,
                              judge_secret, rubric_library, simulator, broker, meter, worlds=worlds)
    executor = PlatformRunExecutor(workers)
    launcher = TrainerLauncher(trainer_templates(config), launch_runner or SubprocessCommandRunner(),
                               config.state_dir / "launches")
    run_snapshots = RunSnapshots(fleet, snapshots, shapes, worlds) if config.snapshot_store else None
    experiment_service = ExperimentService(preflight_context, executor, launcher, run_token_secret, config.fleet_url,
                                           config.public_url, config.approval_threshold_usd,
                                           run_snapshots=run_snapshots)
    # The workers and the policy store serve the experiment service they are built for, so it is set on them now.
    workers.control, workers.tenants = experiment_service, experiment_service.tenant_of_run
    policies.tenant_of_run = experiment_service.tenant_of_run
    catalogs = CatalogService(experiment_service.documents, verifier_registry, rubric_library, registry)
    labeler = (LlmFailureLabeler(GatewayJudgeClient(gateway, config.failure_labeler.endpoint),
                                 config.failure_labeler.model, judge_secret) if config.failure_labeler else None)
    review = ReviewService(views, labeler)
    retention = RetentionSweeper(views, policies)
    alerts = AlertMonitor(experiment_service, views, review, alert_channels(config, environment, http_client),
                          console_url=config.alerts.console_url)
    solvability = PlatformSolvabilityProbe(workers, config.strong_agent) if config.strong_agent else None
    gate_judge = judge_tier(config, gateway, rubric_library, judge_secret)
    authoring = AuthoringService(experiment_service, task_registry, snapshots, fleet, review, shapes, judge=gate_judge,
                                 solvability=solvability, gate_reports=gate_reports, worlds=worlds)
    reruns = (GateReruns(experiment_service, task_registry, gate_reports, fleet, review, shapes,
                         catalog_service=catalogs, judge=gate_judge, solvability=solvability, worlds=worlds)
              if config.snapshot_store else None)
    app = create_app(experiment_service, catalog_service=catalogs,
                     endpoint_service=EndpointService(registry, vault, probe), trajectory_views=views,
                     review_service=review, auth=platform_auth(config, environment, http_client),
                     audit_log_store=PlatformAuditLog(), data_policy_store=policies, pool_status_source=broker,
                     retention_sweeper=retention, alert_monitor=alerts,
                     takeover_service=TakeoverService(fleet, trajectory_store), authoring_service=authoring)
    jobs = [BackgroundJob("alerts", alerts.check, config.alerts.interval_seconds),
            BackgroundJob("retention", retention.sweep, config.retention_interval_hours * 3600)]
    if reruns is not None:
        jobs.append(BackgroundJob("gate_reruns", reruns.poll, config.gate_rerun_interval_seconds))
    return Experimentd(app=app, experiment_service=experiment_service, executor=executor, workers=workers,
                       gateway=gateway, meter=meter, trajectory_store=trajectory_store, review_service=review,
                       alert_monitor=alerts, authoring_service=authoring, gate_reports=gate_reports,
                       gate_reruns=reruns, retention_sweeper=retention, jobs=tuple(jobs))


def judge_tier(config: ExperimentdConfig, gateway: ModelGateway, rubric_library: RubricLibrary | None,
               judge_secret: bytes) -> JudgeTier | None:
    """The judge task gates grade rubric items with; tasks without a rubric never need it."""
    if config.gate_judge is None or rubric_library is None:
        return None
    return RubricJudge(GatewayJudgeClient(gateway, config.gate_judge.endpoint, "gates"), rubric_library,
                       config.gate_judge.model, JudgePromptBuilder(judge_secret))


def start_jobs(jobs: Sequence[BackgroundJob], stop: threading.Event) -> list[threading.Thread]:
    threads = [threading.Thread(target=run_job, args=(job, stop), name=f"experimentd_{job.name}", daemon=True)
               for job in jobs]
    for thread in threads:
        thread.start()
    return threads


def serve(experimentd: Experimentd, server: BaseWSGIServer) -> None:
    """Serves until the server shuts down, with the background jobs beside it."""
    stop = threading.Event()
    threads = start_jobs(experimentd.jobs, stop)
    try:
        server.serve_forever()
    finally:
        stop.set()
        for thread in threads:
            thread.join(timeout=5.0)
        server.server_close()


def interrupt(signal_number: int, frame: Any) -> None:
    raise KeyboardInterrupt


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="rlworld-experimentd", description="Serve the experiment API.")
    parser.add_argument("--config", type=Path, default=Path("experimentd.yaml"), help="experimentd config YAML")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    arguments = build_parser().parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    try:
        config = load_experimentd_config(arguments.config)
        experimentd = build_experimentd(config, os.environ)
    except (ConfigError, ValidationError, OSError) as error:
        print(f"experimentd cannot start: {error}", file=sys.stderr)
        return 1
    signal.signal(signal.SIGTERM, interrupt)
    server = make_server(config.host, config.port, experimentd.app, threaded=True)
    LOGGER.info("experimentd serves %s:%s beside fleetd at %s", config.host, server.server_port, config.fleet_url)
    try:
        serve(experimentd, server)
    except KeyboardInterrupt:
        LOGGER.info("experimentd stopped")
    return 0


if __name__ == "__main__":
    sys.exit(main())
