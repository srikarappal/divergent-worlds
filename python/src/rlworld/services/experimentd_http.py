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
from typing import Any

from flask import Flask, Response, current_app, jsonify, request
from pydantic import BaseModel, ValidationError

from rlworld.gateway.config import DuplicateEndpointError, EndpointConfig
from rlworld.gateway.errors import EndpointNotFoundError
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
from rlworld.specs.document import SpecValidationError, dump_spec_document

SERVICE_KEY = "rlworld_experiment_service"
POLL_KEY = "rlworld_event_poll"
CATALOGS_KEY = "rlworld_catalog_service"
ENDPOINTS_KEY = "rlworld_endpoint_service"
FINISHED_STATES = ("completed", "failed")


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


def health():
    return jsonify({"status": "ok"})


def save_spec():
    return jsonify({"digest": service().save_spec(request.get_data(as_text=True))}), 201


def get_spec(digest: str):
    return jsonify({"digest": digest, "yaml": service().spec(digest)[0]})


def preflight(digest: str):
    return jsonify(service().preflight(digest).model_dump(mode="json"))


def dry_run(digest: str):
    return jsonify({"episode_id": service().dry_run(digest)}), 201


def launch(digest: str):
    body = request.get_json(silent=True) or {}
    return jsonify(service().launch(digest, body.get("budget_usd")).model_dump(mode="json")), 201


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


def verify_token():
    body = request.get_json(silent=True) or {}
    return jsonify({"run_id": service().verify_run_token(body.get("token", ""))})


ROUTES = (
    ("GET", "/healthz", health),
    ("POST", "/v1/specs", save_spec),
    ("GET", "/v1/specs/<digest>", get_spec),
    ("POST", "/v1/specs/<digest>/preflight", preflight),
    ("POST", "/v1/specs/<digest>/dry_run", dry_run),
    ("POST", "/v1/specs/<digest>/launch", launch),
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
)


def create_app(experiment_service: ExperimentService, poll_seconds: float = 1.0,
               sleep: Callable[[float], None] = time.sleep, catalog_service: CatalogService | None = None,
               endpoint_service: EndpointService | None = None) -> Flask:
    app = Flask("experimentd")
    app.extensions[SERVICE_KEY] = experiment_service
    app.extensions[POLL_KEY] = (poll_seconds, sleep)
    app.extensions[CATALOGS_KEY] = catalog_service
    app.extensions[ENDPOINTS_KEY] = endpoint_service
    for method, rule, view_function in ROUTES:
        app.add_url_rule(rule, endpoint=f"{method} {rule}", view_func=view_function, methods=[method])
    for error_class, handler in ERROR_HANDLERS:
        app.register_error_handler(error_class, handler)
    return app
