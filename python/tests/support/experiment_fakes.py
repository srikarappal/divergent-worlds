"""Builders for experimentd tests: an eval experiment, an idle executor and an httpx bridge into the Flask app."""

import copy

import httpx

from rlworld.preflight import PreflightContext, SeatPool
from rlworld.services.experimentd_http import create_app
from rlworld.services.experiments import DryRun, ExperimentService
from rlworld.services.trainer_launch import TrainerLauncher
from rlworld.specs import expand_sweep, parse_spec_document, spec_digest
from rlworld.tasks import GateReport

SEAT_POOLS = {"windows_server_dc": SeatPool(1000, "per_host"), "office_ltsc": SeatPool(10_000)}


class FlaskBridge:
    """httpx transport handler that serves each request from a Flask app, streaming included."""

    def __init__(self, app):
        self.client = app.test_client()

    def __call__(self, request):
        headers = {name: value for name, value in request.headers.items()
                   if name.lower() in ("authorization", "content-type")}
        response = self.client.open(request.url.raw_path.decode(), method=request.method, data=request.content,
                                    headers=headers)
        return httpx.Response(response.status_code, content=response.get_data(),
                              headers={"Content-Type": response.content_type or "application/json"})


class IdleExecutor:
    def start(self, run, document):
        return None

    def apply(self, run):
        return None

    def dry_run(self, document):
        return DryRun(run_id="dry_run_000001", episode_id="dry_run_000001_s000_a0")


def eval_payload(example_payload):
    """The example spec as an eval experiment over its six sweep points."""
    payload = copy.deepcopy(example_payload)
    payload["run"].update({"type": "eval", "policy": {**payload["run"]["policy"], "token_ids": False,
                                                       "logprobs": False}})
    del payload["run"]["trainer"]
    payload["task"]["split"] = "eval"
    return payload


def build_experiment_service(payload, tmp_path, executor=None):
    document = parse_spec_document(payload)
    gate_reports = {spec_digest(point.document.task): GateReport(spec_digest(point.document.task), ())
                    for point in expand_sweep(document)}
    context = PreflightContext(gate_reports=gate_reports, seat_pools=SEAT_POOLS)
    return ExperimentService(context, executor or IdleExecutor(), TrainerLauncher({}, None, tmp_path),
                             b"run token secret", "http://fleetd.internal", "http://experimentd.internal")


def bridged_http_client(service, **app_options):
    app = create_app(service, poll_seconds=0.0, **app_options)
    return httpx.Client(transport=httpx.MockTransport(FlaskBridge(app)))
