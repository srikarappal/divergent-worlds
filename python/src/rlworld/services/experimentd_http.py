"""experimentd HTTP API: the routes the web console, the SDK and the CLI share (DESIGN.md 6.14).

Synchronous Flask with module level handlers. The event stream is server sent
events that either replay what happened (follow=false) or keep polling until
the run ends.
"""

import json
import time
from collections.abc import Callable, Iterator

from flask import Flask, Response, current_app, jsonify, request

from rlworld.services.experiments import (
    TERMINAL_STATES,
    ControlError,
    ExperimentService,
    InvalidRunTokenError,
    LaunchBlockedError,
    UnknownRecordError,
)
from rlworld.specs.document import SpecValidationError

SERVICE_KEY = "rlworld_experiment_service"
POLL_KEY = "rlworld_event_poll"


def service() -> ExperimentService:
    return current_app.extensions[SERVICE_KEY]


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


def control(run_id: str):
    body = request.get_json(silent=True) or {}
    return jsonify(service().control(run_id, body.get("command", ""), body.get("value")).model_dump(mode="json"))


def spend(run_id: str):
    body = request.get_json(silent=True) or {}
    return jsonify(service().record_spend(run_id, float(body.get("usd", 0.0))).model_dump(mode="json"))


def events(run_id: str):
    experiment_service = service()
    experiment_service.run(run_id)
    poll_seconds, sleep = current_app.extensions[POLL_KEY]
    follow = request.args.get("follow", "true").lower() == "true"
    stream = event_stream(experiment_service, run_id, int(request.args.get("after", 0)), follow, poll_seconds, sleep)
    return Response(stream, mimetype="text/event-stream", headers={"Cache-Control": "no-cache"})


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
    ("POST", "/v1/runs/<run_id>/control", control),
    ("POST", "/v1/runs/<run_id>/spend", spend),
    ("GET", "/v1/runs/<run_id>/events", events),
    ("POST", "/v1/run_tokens/verify", verify_token),
)
ERROR_HANDLERS = (
    (UnknownRecordError, unknown_record),
    (SpecValidationError, invalid_spec),
    (LaunchBlockedError, blocked_launch),
    (ControlError, rejected_control),
    (InvalidRunTokenError, invalid_token),
)


def create_app(experiment_service: ExperimentService, poll_seconds: float = 1.0,
               sleep: Callable[[float], None] = time.sleep) -> Flask:
    app = Flask("experimentd")
    app.extensions[SERVICE_KEY] = experiment_service
    app.extensions[POLL_KEY] = (poll_seconds, sleep)
    for method, rule, view_function in ROUTES:
        app.add_url_rule(rule, endpoint=f"{method} {rule}", view_func=view_function, methods=[method])
    for error_class, handler in ERROR_HANDLERS:
        app.register_error_handler(error_class, handler)
    return app
