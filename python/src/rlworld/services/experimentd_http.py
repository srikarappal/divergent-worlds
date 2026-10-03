"""experimentd HTTP API: the routes the web console, the SDK and the CLI share (DESIGN.md 6.14).

Synchronous Flask with module level handlers. The event stream is server sent
events that either replay what happened (follow=false) or keep polling until
the run ends. Rollout workers report spend, metrics and completion with a bearer
run token scoped to their run.
"""

import json
import time
from collections.abc import Callable, Iterator
from dataclasses import asdict
from typing import Any, Protocol

from flask import Flask, Response, current_app, g, jsonify, request
from pydantic import BaseModel, ValidationError

from rlworld.gateway.config import DuplicateEndpointError, EndpointConfig
from rlworld.gateway.errors import EndpointNotFoundError
from rlworld.sdk.client import FleetError
from rlworld.services.admin import DataPolicy, DataPolicyStore, PlatformAuditLog
from rlworld.services.alerts import AlertMonitor
from rlworld.services.auth import DEVELOPMENT_PRINCIPAL, AuthError, ForbiddenError, PlatformAuth
from rlworld.services.catalogs import (
    CatalogEntry,
    CatalogService,
    CatalogWriteError,
    EndpointService,
    UnknownCatalogError,
)
from rlworld.services.experiments import (
    TERMINAL_STATES,
    ControlError,
    ExperimentService,
    InvalidRunTokenError,
    LaunchBlockedError,
    UnknownRecordError,
)
from rlworld.services.retention import RetentionSweeper
from rlworld.services.takeover import TakeoverError, TakeoverService
from rlworld.services.review import (
    EpisodeFilter,
    FiledItem,
    ReviewError,
    ReviewRequest,
    ReviewService,
    ReviewUpdate,
)
from rlworld.services.trajectory_views import TrajectoryViews
from rlworld.specs.document import SpecValidationError, dump_spec_document
from rlworld.trajectories.exports import preference_pairs, sft_rows

SERVICE_KEY = "rlworld_experiment_service"
POLL_KEY = "rlworld_event_poll"
CATALOGS_KEY = "rlworld_catalog_service"
ENDPOINTS_KEY = "rlworld_endpoint_service"
TRAJECTORIES_KEY = "rlworld_trajectory_views"
REVIEW_KEY = "rlworld_review_service"
AUTH_KEY = "rlworld_auth"
AUDIT_KEY = "rlworld_audit_log"
POLICIES_KEY = "rlworld_data_policies"
POOLS_KEY = "rlworld_pool_status"
RETENTION_KEY = "rlworld_retention"
ALERTS_KEY = "rlworld_alerts"
TAKEOVER_KEY = "rlworld_takeover"
ALERT_FILTERS = ("run_id", "experiment_id", "status")
PUBLIC_RULES = frozenset({"/healthz", "/v1/auth/config"})
FRAME_RULE = "/v1/frames/<frame_digest>"
RUN_TOKEN_RULES = frozenset({"/v1/runs/<run_id>/spend", "/v1/runs/<run_id>/metrics", "/v1/runs/<run_id>/finish",
                             "/v1/run_tokens/verify"})
WRITE_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})
EXPORT_KINDS = ("sft", "preferences")
# Request fields the audit log keeps per operation; anything not listed, keys above all, never enters it.
AUDIT_REQUEST_FIELDS: dict[str, tuple[str, ...]] = {
    "launch": ("budget_usd",),
    "control": ("command", "value"),
    "review_episode": ("action",),
    "update_review_item": ("status",),
    "file_review_item": ("source", "queue", "title"),
    "register_endpoint": ("name", "base_url", "allowed_models"),
    "publish_catalog_entry": ("version",),
    "set_roles": ("roles",),
    "set_data_policy": ("screenshot_endpoints", "trajectory_days", "full_frame_days", "exports"),
}
AUDIT_RESPONSE_FIELDS: dict[str, tuple[str, ...]] = {
    "save_spec": ("digest",),
    "launch": ("experiment_id", "run_ids"),
    "dry_run": ("run_id", "episode_id"),
    "review_episode": ("item_id", "queue"),
    "file_review_item": ("item_id",),
}
COHORT_ARGUMENTS = ("termination", "task_id", "template", "difficulty_key", "category", "policy_version", "limit")
ITEM_FILTERS = ("queue", "status", "run_id", "episode_id")
EPISODE_STATUSES = ("running", "finished")
FRAME_SIZES = ("full", "thumbnail")
FINISHED_STATES = ("completed", "failed")


class PoolStatus(Protocol):
    """What the entitlements broker reports: free seats and identities per pool."""

    def availability(self) -> dict[str, int]: ...


class ServiceUnavailableError(Exception):
    """This experimentd was started without the service a route needs."""


class RequestValidationError(ValueError):
    def __init__(self, problems: list[str]) -> None:
        super().__init__("; ".join(problems))
        self.problems = problems


def service() -> ExperimentService:
    return current_app.extensions[SERVICE_KEY]


def configured(extension_key: str, description: str) -> Any:
    configured_service = current_app.extensions.get(extension_key)
    if configured_service is None:
        raise ServiceUnavailableError(f"{description} are not configured on this experimentd")
    return configured_service


def validated(model_class: type[BaseModel], payload: Any) -> Any:
    try:
        return model_class.model_validate(payload)
    except ValidationError as error:
        problems = [f"{'.'.join(str(part) for part in problem['loc']) or 'body'}: {problem['msg']}"
                    for problem in error.errors()]
        raise RequestValidationError(problems) from error


def principal() -> Any:
    return g.get("principal", DEVELOPMENT_PRINCIPAL)


def authenticate() -> None:
    """Every route but health, auth config and the run token routes needs a signed in user with a fitting role."""
    rule = request.url_rule.rule if request.url_rule is not None else None
    if rule is None or rule in PUBLIC_RULES or rule in RUN_TOKEN_RULES:
        return
    auth: PlatformAuth | None = current_app.extensions[AUTH_KEY]
    if auth is None:
        g.principal = DEVELOPMENT_PRINCIPAL
    elif rule == FRAME_RULE and "grant" in request.args:
        if not auth.frame_grants.valid(request.args["grant"]):
            raise AuthError("the frame grant is forged or expired")
    else:
        g.principal = auth.authorize(request.method, rule, request.headers.get("Authorization", ""))


def picked(source: Any, fields: tuple[str, ...]) -> dict[str, Any]:
    return {field: source[field] for field in fields if isinstance(source, dict) and field in source}


def record_audit(response: Response) -> Response:
    """Every successful write lands in the audit log with its actor, target and safe details."""
    rule = request.url_rule.rule if request.url_rule is not None else None
    if request.method not in WRITE_METHODS or response.status_code >= 400 or rule is None or rule in RUN_TOKEN_RULES:
        return response
    operation = current_app.view_functions[request.endpoint].__name__
    detail = picked(request.get_json(silent=True) if request.is_json else None, AUDIT_REQUEST_FIELDS.get(operation, ()))
    if response.is_json:
        detail.update(picked(response.get_json(), AUDIT_RESPONSE_FIELDS.get(operation, ())))
    actor = principal()
    current_app.extensions[AUDIT_KEY].record(actor.subject, actor.tenant, operation, dict(request.view_args or {}),
                                             detail)
    return response


def require_run_token(run_id: str) -> None:
    """Worker routes take a bearer run token that is valid only for its own run."""
    authorization = request.headers.get("Authorization", "")
    token = authorization.removeprefix("Bearer ").strip() if authorization.startswith("Bearer ") else ""
    if service().verify_run_token(token) != run_id:
        raise InvalidRunTokenError(f"the run token is not scoped to {run_id}")


def server_sent_event(sequence: int, kind: str, payload: str) -> str:
    return f"id: {sequence}\nevent: {kind}\ndata: {payload}\n\n"


def event_stream(experiment_service: ExperimentService, run_id: str, after_sequence: int, follow: bool,
                 poll_seconds: float, sleep: Callable[[float], None]) -> Iterator[str]:
    last_sequence = after_sequence
    while True:
        for event in experiment_service.run_events(run_id, last_sequence):
            last_sequence = event.sequence
            yield server_sent_event(event.sequence, event.kind, json.dumps(event.model_dump()))
        if not follow or experiment_service.run(run_id).state in TERMINAL_STATES:
            return
        sleep(poll_seconds)


def unknown_record(error: UnknownRecordError):
    return jsonify({"code": "not_found", "message": str(error)}), 404


def invalid_spec(error: SpecValidationError):
    return jsonify({"code": "invalid_spec", "problems": error.problems}), 400


def blocked_launch(error: LaunchBlockedError):
    return jsonify({"code": "launch_blocked", "problems": error.problems}), 422


def rejected_control(error: ControlError):
    return jsonify({"code": "control_rejected", "message": str(error)}), 409


def invalid_token(error: InvalidRunTokenError):
    return jsonify({"code": "invalid_run_token", "message": str(error)}), 401


def invalid_request(error: RequestValidationError):
    return jsonify({"code": "invalid_request", "problems": error.problems}), 400


def unknown_catalog(error: UnknownCatalogError):
    return jsonify({"code": "not_found", "message": f"no catalog named {error.args[0]}"}), 404


def unknown_endpoint(error: EndpointNotFoundError):
    return jsonify({"code": "not_found", "message": str(error)}), 404


def rejected_write(error: ValueError):
    return jsonify({"code": "conflict", "message": str(error)}), 409


def unavailable(error: ServiceUnavailableError):
    return jsonify({"code": "unavailable", "message": str(error)}), 503


def unauthenticated(error: AuthError):
    return jsonify({"code": "unauthenticated", "message": str(error)}), 401


def forbidden(error: ForbiddenError):
    return jsonify({"code": "forbidden", "message": str(error)}), 403


def health():
    return jsonify({"status": "ok"})


def save_spec():
    return jsonify({"digest": service().save_spec(request.get_data(as_text=True))}), 201


def get_spec(digest: str):
    return jsonify({"digest": digest, "yaml": service().spec(digest)[0]})


def preflight(digest: str):
    return jsonify(service().preflight(digest).model_dump(mode="json"))


def dry_run(digest: str):
    return jsonify(service().dry_run(digest).model_dump()), 201


def launch(digest: str):
    body = request.get_json(silent=True) or {}
    experiment = service().launch(digest, body.get("budget_usd"), tenant=principal().tenant)
    return jsonify(experiment.model_dump(mode="json")), 201


def list_experiments():
    experiments = service().list_experiments(request.args.get("tenant"))
    return jsonify({"experiments": [experiment.model_dump(mode="json") for experiment in experiments]})


def get_experiment(experiment_id: str):
    return jsonify(service().experiment(experiment_id).model_dump(mode="json"))


def approve(experiment_id: str):
    return jsonify(service().approve(experiment_id).model_dump(mode="json"))


def list_runs():
    runs = service().list_runs(request.args.get("experiment_id"))
    return jsonify({"runs": [run.model_dump(mode="json") for run in runs]})


def get_run(run_id: str):
    return jsonify(service().run(run_id).model_dump(mode="json"))


def get_run_spec(run_id: str):
    run = service().run(run_id)
    return jsonify({"run_id": run_id, "spec_digest": run.spec_digest,
                    "yaml": dump_spec_document(service().run_document(run_id))})


def control(run_id: str):
    body = request.get_json(silent=True) or {}
    return jsonify(service().control(run_id, body.get("command", ""), body.get("value")).model_dump(mode="json"))


def spend(run_id: str):
    require_run_token(run_id)
    body = request.get_json(silent=True) or {}
    return jsonify(service().record_spend(run_id, float(body.get("usd", 0.0))).model_dump(mode="json"))


def metrics(run_id: str):
    require_run_token(run_id)
    service().publish_metrics(run_id, request.get_json(silent=True) or {})
    return "", 204


def finish(run_id: str):
    require_run_token(run_id)
    body = request.get_json(silent=True) or {}
    state = body.get("state")
    if state not in FINISHED_STATES:
        raise ControlError(f"a run finishes as one of {', '.join(FINISHED_STATES)}")
    return jsonify(service().mark_finished(run_id, state, body.get("detail", "")).model_dump(mode="json"))


def events(run_id: str):
    experiment_service = service()
    experiment_service.run(run_id)
    poll_seconds, sleep = current_app.extensions[POLL_KEY]
    follow = request.args.get("follow", "true").lower() == "true"
    stream = event_stream(experiment_service, run_id, int(request.args.get("after", 0)), follow, poll_seconds, sleep)
    return Response(stream, mimetype="text/event-stream", headers={"Cache-Control": "no-cache"})


def list_catalog(kind: str):
    catalog_service: CatalogService = configured(CATALOGS_KEY, "catalogs")
    entries = [entry.model_dump(mode="json") for entry in catalog_service.entries(kind)]
    return jsonify({"kind": kind, "entries": entries})


def publish_catalog_entry(kind: str, name: str):
    """Factory jobs publish images, worlds and task templates here."""
    catalog_service: CatalogService = configured(CATALOGS_KEY, "catalogs")
    entry = validated(CatalogEntry, {**(request.get_json(silent=True) or {}), "kind": kind, "name": name})
    return jsonify(catalog_service.publish(entry).model_dump(mode="json"))


def list_endpoints():
    return jsonify({"endpoints": configured(ENDPOINTS_KEY, "endpoints").list()})


def register_endpoint():
    endpoint = validated(EndpointConfig, request.get_json(silent=True) or {})
    return jsonify(configured(ENDPOINTS_KEY, "endpoints").register(endpoint)), 201


def test_endpoint(name: str):
    return jsonify(asdict(configured(ENDPOINTS_KEY, "endpoints").test(name)))


def set_endpoint_key(name: str):
    body = request.get_json(silent=True) or {}
    configured(ENDPOINTS_KEY, "endpoints").set_key(name, str(body.get("key", "")))
    return "", 204


def trajectories() -> TrajectoryViews:
    return configured(TRAJECTORIES_KEY, "trajectories")


def choice_argument(name: str, allowed: tuple[str, ...], default: str | None) -> str | None:
    value = request.args.get(name, default)
    if value is not None and value not in allowed:
        raise RequestValidationError([f"{name}: must be one of {', '.join(allowed)}"])
    return value


def integer_argument(name: str, default: int) -> int:
    try:
        return int(request.args.get(name, default))
    except ValueError as error:
        raise RequestValidationError([f"{name}: must be an integer"]) from error


def list_episodes(run_id: str):
    episodes = trajectories().episodes(run_id, choice_argument("status", EPISODE_STATUSES, None))
    return jsonify({"run_id": run_id, "episodes": [episode.model_dump(mode="json", exclude={"steps"})
                                                   for episode in episodes]})


def get_episode(run_id: str, episode_id: str):
    episode = trajectories().episode(run_id, episode_id, integer_argument("first_step", 0))
    return jsonify(episode.model_dump(mode="json"))


def live_wall(run_id: str):
    tiles = trajectories().live_wall(run_id, integer_argument("limit", 24))
    return jsonify({"run_id": run_id, "tiles": [tile.model_dump(mode="json") for tile in tiles]})


def get_frame(frame_digest: str):
    frame, media_type = trajectories().frame(frame_digest, choice_argument("size", FRAME_SIZES, "full"))
    return Response(frame, mimetype=media_type, headers={"Cache-Control": "private, max-age=31536000, immutable"})


def review() -> ReviewService:
    return configured(REVIEW_KEY, "review")


def cohort(run_id: str):
    filters = validated(EpisodeFilter, {name: request.args[name] for name in COHORT_ARGUMENTS if name in request.args})
    return jsonify({"run_id": run_id, "episodes": [member.model_dump(mode="json")
                                                   for member in review().cohort(run_id, filters)]})


def get_provenance(run_id: str, episode_id: str):
    return jsonify(review().provenance(run_id, episode_id).model_dump(mode="json"))


def get_step_prompt(run_id: str, episode_id: str, step_index: str):
    if not step_index.isdigit():
        raise RequestValidationError(["step_index: must be a non negative integer"])
    return jsonify({"messages": review().prompt(run_id, episode_id, int(step_index))})


def analyze_failures(run_id: str):
    return jsonify(review().analyze(run_id).model_dump(mode="json"))


def get_failures(run_id: str):
    analysis = review().analysis(run_id)
    if analysis is None:
        raise UnknownRecordError(f"no failure analysis for {run_id} yet")
    return jsonify(analysis.model_dump(mode="json"))


def run_report(run_id: str):
    return jsonify(review().report(run_id))


def compare_runs():
    run_ids = [run_id for run_id in request.args.get("run_ids", "").split(",") if run_id]
    if len(run_ids) < 2:
        raise RequestValidationError(["run_ids: name at least two runs, separated by commas"])
    return jsonify(review().compare_runs(run_ids).model_dump(mode="json"))


def compare_policy_versions(run_id: str):
    return jsonify(review().compare_policy_versions(run_id).model_dump(mode="json"))


def review_episode(run_id: str, episode_id: str):
    review_request = validated(ReviewRequest, request.get_json(silent=True) or {})
    return jsonify(review().act(run_id, episode_id, review_request).model_dump(mode="json")), 201


def list_review_items():
    filters = {name: request.args[name] for name in ITEM_FILTERS if name in request.args}
    return jsonify({"items": [item.model_dump(mode="json") for item in review().list_items(**filters)]})


def update_review_item(item_id: str):
    update = validated(ReviewUpdate, request.get_json(silent=True) or {})
    return jsonify(review().update_item(item_id, update, principal().subject).model_dump(mode="json"))


def file_review_item():
    filed = validated(FiledItem, request.get_json(silent=True) or {})
    return jsonify(review().file_item(filed, principal().subject).model_dump(mode="json")), 201


def calibration():
    return jsonify(review().calibration(request.args.get("run_id")).model_dump(mode="json"))


def data_policies() -> DataPolicyStore:
    return current_app.extensions[POLICIES_KEY]


def export_run(run_id: str, kind: str):
    """Exports leave the platform, so the run's tenant policy must allow the kind."""
    if kind not in EXPORT_KINDS:
        raise RequestValidationError([f"kind: must be one of {', '.join(EXPORT_KINDS)}"])
    if not data_policies().allows_export(run_id, kind):
        raise ForbiddenError(f"the data policy of {run_id} does not allow {kind} exports")
    views = trajectories()
    episodes = [views.episode(run_id, episode.episode_id) for episode in views.episodes(run_id, "finished")]
    rows = sft_rows(episodes) if kind == "sft" else preference_pairs(episodes)
    return jsonify({"run_id": run_id, "kind": kind, "rows": [asdict(row) for row in rows]})


def auth_config():
    auth: PlatformAuth | None = current_app.extensions[AUTH_KEY]
    if auth is None:
        return jsonify({"enabled": False})
    settings = auth.verifier.settings
    return jsonify({"enabled": True, "issuer": settings.issuer, "client_id": settings.client_id,
                    "audience": settings.audience, "scopes": list(settings.scopes)})


def me():
    actor = principal()
    return jsonify({"subject": actor.subject, "email": actor.email, "tenant": actor.tenant,
                    "roles": sorted(actor.roles)})


def frame_grant():
    """A grant the console appends to frame URLs; img elements cannot carry the bearer token."""
    auth: PlatformAuth | None = current_app.extensions[AUTH_KEY]
    if auth is None:
        return jsonify({"grant": "", "expires_at": None})
    grant, expires_at = auth.frame_grants.issue(principal().subject)
    return jsonify({"grant": grant, "expires_at": expires_at})


def platform_auth() -> PlatformAuth:
    return configured(AUTH_KEY, "platform sign in and roles")


def list_roles():
    return jsonify({"bindings": platform_auth().bindings.all()})


def set_roles(subject: str):
    roles = (request.get_json(silent=True) or {}).get("roles")
    if not isinstance(roles, list):
        raise RequestValidationError(["roles: must be a list"])
    try:
        granted = platform_auth().bindings.set(subject, frozenset(str(role) for role in roles))
    except ValueError as error:
        raise RequestValidationError([str(error)]) from error
    return jsonify({"subject": subject, "roles": sorted(granted)})


def list_data_policies():
    return jsonify({"policies": [policy.model_dump(mode="json") for policy in data_policies().all()]})


def set_data_policy(tenant: str):
    policy = validated(DataPolicy, {**(request.get_json(silent=True) or {}), "tenant": tenant})
    return jsonify(data_policies().set(policy).model_dump(mode="json"))


def audit_log():
    since = request.args.get("since")
    records = current_app.extensions[AUDIT_KEY].query(actor=request.args.get("actor"),
                                                      action=request.args.get("action"),
                                                      since=float(since) if since else None,
                                                      limit=integer_argument("limit", 500))
    return jsonify({"records": [record.model_dump(mode="json") for record in records]})


def pool_status():
    return jsonify({"pools": configured(POOLS_KEY, "entitlement pools").availability()})


def sweep_retention():
    sweeper: RetentionSweeper = configured(RETENTION_KEY, "retention")
    return jsonify(asdict(sweeper.sweep()))


def list_alerts():
    monitor: AlertMonitor = configured(ALERTS_KEY, "alerts")
    filters = {name: request.args[name] for name in ALERT_FILTERS if name in request.args}
    return jsonify({"alerts": [alert.model_dump(mode="json") for alert in monitor.alerts(**filters)]})


def acknowledge_alert(alert_id: str):
    monitor: AlertMonitor = configured(ALERTS_KEY, "alerts")
    return jsonify(monitor.acknowledge(alert_id, principal().subject).model_dump(mode="json"))


def start_takeover(run_id: str, episode_id: str):
    takeovers: TakeoverService = configured(TAKEOVER_KEY, "takeovers")
    return jsonify(takeovers.start(run_id, episode_id).model_dump(mode="json")), 201


def end_takeover(run_id: str, episode_id: str):
    takeovers: TakeoverService = configured(TAKEOVER_KEY, "takeovers")
    return jsonify(takeovers.end(run_id, episode_id, principal().subject).model_dump(mode="json"))


def refused_takeover(error: TakeoverError | FleetError):
    """The episode has ended or lost its lease, or fleetd said no: an operator cannot take it over now."""
    return jsonify({"code": "takeover_refused", "problems": [str(error)]}), 409


def invalid_review(error: ReviewError):
    return jsonify({"code": "invalid_request", "problems": [str(error)]}), 400


def verify_token():
    body = request.get_json(silent=True) or {}
    return jsonify({"run_id": service().verify_run_token(body.get("token", ""))})


ROUTES = (
    ("GET", "/healthz", health),
    ("GET", "/v1/auth/config", auth_config),
    ("GET", "/v1/me", me),
    ("POST", "/v1/specs", save_spec),
    ("GET", "/v1/specs/<digest>", get_spec),
    ("POST", "/v1/specs/<digest>/preflight", preflight),
    ("POST", "/v1/specs/<digest>/dry_run", dry_run),
    ("POST", "/v1/specs/<digest>/launch", launch),
    ("GET", "/v1/experiments", list_experiments),
    ("GET", "/v1/experiments/<experiment_id>", get_experiment),
    ("POST", "/v1/experiments/<experiment_id>/approve", approve),
    ("GET", "/v1/runs", list_runs),
    ("GET", "/v1/runs/<run_id>", get_run),
    ("GET", "/v1/runs/<run_id>/spec", get_run_spec),
    ("POST", "/v1/runs/<run_id>/control", control),
    ("POST", "/v1/runs/<run_id>/spend", spend),
    ("POST", "/v1/runs/<run_id>/metrics", metrics),
    ("POST", "/v1/runs/<run_id>/finish", finish),
    ("GET", "/v1/runs/<run_id>/events", events),
    ("POST", "/v1/run_tokens/verify", verify_token),
    ("GET", "/v1/runs/<run_id>/episodes", list_episodes),
    ("GET", "/v1/runs/<run_id>/episodes/<episode_id>", get_episode),
    ("GET", "/v1/runs/<run_id>/live", live_wall),
    ("GET", "/v1/frames/<frame_digest>", get_frame),
    ("GET", "/v1/frame_grant", frame_grant),
    ("GET", "/v1/runs/<run_id>/cohort", cohort),
    ("GET", "/v1/runs/<run_id>/episodes/<episode_id>/provenance", get_provenance),
    ("GET", "/v1/runs/<run_id>/episodes/<episode_id>/steps/<step_index>/prompt", get_step_prompt),
    ("POST", "/v1/runs/<run_id>/episodes/<episode_id>/actions", review_episode),
    ("POST", "/v1/runs/<run_id>/episodes/<episode_id>/takeover", start_takeover),
    ("POST", "/v1/runs/<run_id>/episodes/<episode_id>/takeover/end", end_takeover),
    ("POST", "/v1/runs/<run_id>/failures/analyze", analyze_failures),
    ("GET", "/v1/runs/<run_id>/failures", get_failures),
    ("GET", "/v1/runs/<run_id>/report", run_report),
    ("GET", "/v1/runs/<run_id>/compare/policy_versions", compare_policy_versions),
    ("GET", "/v1/compare/runs", compare_runs),
    ("GET", "/v1/review_items", list_review_items),
    ("POST", "/v1/review_items", file_review_item),
    ("PATCH", "/v1/review_items/<item_id>", update_review_item),
    ("GET", "/v1/calibration", calibration),
    ("GET", "/v1/runs/<run_id>/exports/<kind>", export_run),
    ("GET", "/v1/admin/roles", list_roles),
    ("PUT", "/v1/admin/roles/<subject>", set_roles),
    ("GET", "/v1/admin/data_policies", list_data_policies),
    ("PUT", "/v1/admin/data_policies/<tenant>", set_data_policy),
    ("GET", "/v1/admin/audit", audit_log),
    ("GET", "/v1/admin/pools", pool_status),
    ("POST", "/v1/admin/retention/sweep", sweep_retention),
    ("GET", "/v1/alerts", list_alerts),
    ("POST", "/v1/alerts/<alert_id>/acknowledge", acknowledge_alert),
    ("GET", "/v1/catalogs/<kind>", list_catalog),
    ("PUT", "/v1/catalogs/<kind>/<name>", publish_catalog_entry),
    ("GET", "/v1/endpoints", list_endpoints),
    ("POST", "/v1/endpoints", register_endpoint),
    ("POST", "/v1/endpoints/<name>/test", test_endpoint),
    ("PUT", "/v1/endpoints/<name>/key", set_endpoint_key),
)
ERROR_HANDLERS = (
    (UnknownRecordError, unknown_record),
    (SpecValidationError, invalid_spec),
    (LaunchBlockedError, blocked_launch),
    (ControlError, rejected_control),
    (InvalidRunTokenError, invalid_token),
    (RequestValidationError, invalid_request),
    (UnknownCatalogError, unknown_catalog),
    (EndpointNotFoundError, unknown_endpoint),
    (CatalogWriteError, rejected_write),
    (DuplicateEndpointError, rejected_write),
    (ServiceUnavailableError, unavailable),
    (ReviewError, invalid_review),
    (TakeoverError, refused_takeover),
    (FleetError, refused_takeover),
    (AuthError, unauthenticated),
    (ForbiddenError, forbidden),
)


def create_app(experiment_service: ExperimentService, poll_seconds: float = 1.0,
               sleep: Callable[[float], None] = time.sleep, catalog_service: CatalogService | None = None,
               endpoint_service: EndpointService | None = None, trajectory_views: TrajectoryViews | None = None,
               review_service: ReviewService | None = None, auth: PlatformAuth | None = None,
               audit_log_store: PlatformAuditLog | None = None, data_policy_store: DataPolicyStore | None = None,
               pool_status_source: PoolStatus | None = None,
               retention_sweeper: RetentionSweeper | None = None,
               alert_monitor: AlertMonitor | None = None,
               takeover_service: TakeoverService | None = None) -> Flask:
    app = Flask("experimentd")
    app.extensions[SERVICE_KEY] = experiment_service
    app.extensions[POLL_KEY] = (poll_seconds, sleep)
    app.extensions[CATALOGS_KEY] = catalog_service
    app.extensions[ENDPOINTS_KEY] = endpoint_service
    app.extensions[TRAJECTORIES_KEY] = trajectory_views
    app.extensions[REVIEW_KEY] = review_service
    app.extensions[AUTH_KEY] = auth
    app.extensions[AUDIT_KEY] = audit_log_store or PlatformAuditLog()
    app.extensions[POLICIES_KEY] = data_policy_store or DataPolicyStore(experiment_service.tenant_of_run)
    app.extensions[POOLS_KEY] = pool_status_source
    app.extensions[RETENTION_KEY] = retention_sweeper
    app.extensions[ALERTS_KEY] = alert_monitor
    app.extensions[TAKEOVER_KEY] = takeover_service
    app.before_request(authenticate)
    app.after_request(record_audit)
    for method, rule, view_function in ROUTES:
        app.add_url_rule(rule, endpoint=f"{method} {rule}", view_func=view_function, methods=[method])
    for error_class, handler in ERROR_HANDLERS:
        app.register_error_handler(error_class, handler)
    return app
