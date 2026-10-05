"""experimentd test helpers: the laptop config under a temporary directory, a small reference spec on the keyless
local endpoint, an endpoint that always finishes and a recording trainer launcher."""

import threading
from pathlib import Path

import yaml

from rlworld.gateway.openai_compatible import parse_response
from rlworld.services.experimentd_main import ExperimentdConfig, load_experimentd_config
from rlworld.specs import dump_spec_document, parse_spec_document

EXAMPLES = Path(__file__).resolve().parents[3] / "examples"
LAPTOP = EXAMPLES / "experimentd" / "laptop.yaml"
REFERENCE_SPEC = EXAMPLES / "specs" / "reconcile_invoices_linux.yaml"
SECRETS = {"RLWORLD_RUN_TOKEN_SECRET": "run token secret", "RLWORLD_JUDGE_SECRET": "judge secret"}


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


def laptop_config(tmp_path, **changes):
    """The laptop example with its stores under tmp_path and any changes validated like the file would be."""
    config = load_experimentd_config(LAPTOP)
    return ExperimentdConfig.model_validate({**config.model_dump(), "state_dir": tmp_path / "state",
                                             "object_store_dir": tmp_path / "objects", **changes})


def small_reference_spec():
    payload = yaml.safe_load(REFERENCE_SPEC.read_text())
    payload["world"]["size"] = {"employees": 6, "customers": 6, "invoices_per_customer": 3, "emails_per_person": 2}
    payload["run"]["tasks_per_batch"] = 2
    payload["run"]["policy"] = {**payload["run"]["policy"], "endpoint": "local_policy", "model": "my_policy"}
    return dump_spec_document(parse_spec_document(payload))
