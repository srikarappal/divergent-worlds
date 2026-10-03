"""The rlworld command line, at parity with the web console and the SDK (DESIGN.md 6.7 and 6.14).

Every subcommand is one public operation from schemas/api_operations.json and
prints its JSON result. Endpoint keys are read from standard input so they never
land in shell history.
"""

import argparse
import json
import os
import sys
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel

from rlworld.sdk.experiments import ExperimentApiError, ExperimentClient

DEFAULT_API_URL = "http://localhost:8080"


def parse_value(text: str | None) -> Any:
    """Control values are JSON when they parse, such as 4000, and plain strings otherwise, such as eval."""
    if text is None:
        return None
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return text


def save_spec(client: ExperimentClient, arguments: argparse.Namespace) -> Any:
    return {"digest": client.save_spec(Path(arguments.spec_file).read_text())}


def get_spec(client: ExperimentClient, arguments: argparse.Namespace) -> Any:
    return {"digest": arguments.digest, "yaml": client.get_spec(arguments.digest)}


def preflight(client: ExperimentClient, arguments: argparse.Namespace) -> Any:
    return client.preflight(arguments.digest)


def dry_run(client: ExperimentClient, arguments: argparse.Namespace) -> Any:
    return {"episode_id": client.dry_run(arguments.digest)}


def launch(client: ExperimentClient, arguments: argparse.Namespace) -> Any:
    return client.launch(arguments.digest, budget_usd=arguments.budget_usd)


def approve_experiment(client: ExperimentClient, arguments: argparse.Namespace) -> Any:
    return client.approve_experiment(arguments.experiment_id)


def list_runs(client: ExperimentClient, arguments: argparse.Namespace) -> Any:
    return client.list_runs(experiment_id=arguments.experiment_id)


def get_run(client: ExperimentClient, arguments: argparse.Namespace) -> Any:
    return client.get_run(arguments.run_id)


def get_run_spec(client: ExperimentClient, arguments: argparse.Namespace) -> Any:
    return {"run_id": arguments.run_id, "yaml": client.get_run_spec(arguments.run_id)}


def control_run(client: ExperimentClient, arguments: argparse.Namespace) -> Any:
    return client.control_run(arguments.run_id, arguments.command_name, parse_value(arguments.value))


def run_events(client: ExperimentClient, arguments: argparse.Namespace) -> Any:
    for event in client.run_events(arguments.run_id, follow=arguments.follow, after=arguments.after):
        print(event.model_dump_json(), flush=True)
    return None


def list_catalog(client: ExperimentClient, arguments: argparse.Namespace) -> Any:
    return client.list_catalog(arguments.kind)


def list_endpoints(client: ExperimentClient, arguments: argparse.Namespace) -> Any:
    return client.list_endpoints()


def register_endpoint(client: ExperimentClient, arguments: argparse.Namespace) -> Any:
    return client.register_endpoint(yaml.safe_load(Path(arguments.endpoint_file).read_text()))


def test_endpoint(client: ExperimentClient, arguments: argparse.Namespace) -> Any:
    return client.test_endpoint(arguments.name)


def set_endpoint_key(client: ExperimentClient, arguments: argparse.Namespace) -> Any:
    client.set_endpoint_key(arguments.name, sys.stdin.readline().strip())
    return {"name": arguments.name, "key": "stored"}


OperationHandler = Callable[[ExperimentClient, argparse.Namespace], Any]
HANDLERS: dict[str, OperationHandler] = {
    handler.__name__: handler
    for handler in (save_spec, get_spec, preflight, dry_run, launch, approve_experiment, list_runs, get_run,
                    get_run_spec, control_run, run_events, list_catalog, list_endpoints, register_endpoint,
                    test_endpoint, set_endpoint_key)
}


def add_operation(commands: Any, command_name: str, handler: OperationHandler,
                  help_text: str) -> argparse.ArgumentParser:
    command_parser = commands.add_parser(command_name, help=help_text)
    command_parser.set_defaults(operation=handler.__name__)
    return command_parser


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="rlworld", description="Configure, launch and watch experiments.")
    parser.add_argument("--api-url", default=os.environ.get("RLWORLD_API_URL", DEFAULT_API_URL))
    groups = parser.add_subparsers(dest="group", required=True)

    specs = groups.add_parser("specs", help="save, read, preflight, dry run and launch specs")
    spec_commands = specs.add_subparsers(dest="command", required=True)
    add_operation(spec_commands, "save", save_spec, "save a spec file").add_argument("spec_file")
    add_operation(spec_commands, "get", get_spec, "read a saved spec").add_argument("digest")
    add_operation(spec_commands, "preflight", preflight, "validate and estimate").add_argument("digest")
    add_operation(spec_commands, "dry-run", dry_run, "play one episode").add_argument("digest")
    launch_parser = add_operation(spec_commands, "launch", launch, "launch every sweep point")
    launch_parser.add_argument("digest")
    launch_parser.add_argument("--budget-usd", type=float, default=None)

    experiments = groups.add_parser("experiments", help="approve experiments")
    experiment_commands = experiments.add_subparsers(dest="command", required=True)
    add_operation(experiment_commands, "approve", approve_experiment, "approve a launch").add_argument("experiment_id")

    runs = groups.add_parser("runs", help="list, read, control and follow runs")
    run_commands = runs.add_subparsers(dest="command", required=True)
    add_operation(run_commands, "list", list_runs, "list runs").add_argument("--experiment-id", default=None)
    add_operation(run_commands, "get", get_run, "read a run").add_argument("run_id")
    add_operation(run_commands, "spec", get_run_spec, "read a run's full spec").add_argument("run_id")
    control_parser = add_operation(run_commands, "control", control_run, "change an operational knob")
    control_parser.add_argument("run_id")
    control_parser.add_argument("command_name", metavar="command")
    control_parser.add_argument("value", nargs="?", default=None)
    events_parser = add_operation(run_commands, "events", run_events, "print a run's events")
    events_parser.add_argument("run_id")
    events_parser.add_argument("--follow", action="store_true")
    events_parser.add_argument("--after", type=int, default=0)

    catalogs = groups.add_parser("catalogs", help="browse catalogs")
    catalog_commands = catalogs.add_subparsers(dest="command", required=True)
    add_operation(catalog_commands, "list", list_catalog, "list one catalog").add_argument("kind")

    endpoints = groups.add_parser("endpoints", help="register, test and key endpoints")
    endpoint_commands = endpoints.add_subparsers(dest="command", required=True)
    add_operation(endpoint_commands, "list", list_endpoints, "list endpoints")
    add_operation(endpoint_commands, "register", register_endpoint, "register from a YAML or JSON file").add_argument(
        "endpoint_file")
    add_operation(endpoint_commands, "test", test_endpoint, "probe an endpoint").add_argument("name")
    add_operation(endpoint_commands, "set-key", set_endpoint_key, "store a key read from stdin").add_argument("name")
    return parser


def to_json(result: Any) -> Any:
    if isinstance(result, BaseModel):
        return result.model_dump(mode="json")
    if isinstance(result, list):
        return [to_json(item) for item in result]
    return result


def main(argv: Sequence[str] | None = None, client: ExperimentClient | None = None) -> int:
    arguments = build_parser().parse_args(argv)
    try:
        result = HANDLERS[arguments.operation](client or ExperimentClient(arguments.api_url), arguments)
    except ExperimentApiError as error:
        print(json.dumps({"error": str(error), "problems": error.problems}, indent=2), file=sys.stderr)
        return 1
    if result is not None:
        print(json.dumps(to_json(result), indent=2, default=str))
    return 0


if __name__ == "__main__":
    sys.exit(main())
