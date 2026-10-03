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
    return client.dry_run(arguments.digest)


def launch(client: ExperimentClient, arguments: argparse.Namespace) -> Any:
    return client.launch(arguments.digest, budget_usd=arguments.budget_usd)


def get_experiment(client: ExperimentClient, arguments: argparse.Namespace) -> Any:
    return client.get_experiment(arguments.experiment_id)


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


def list_episodes(client: ExperimentClient, arguments: argparse.Namespace) -> Any:
    return [episode.model_dump(mode="json", exclude={"steps"})
            for episode in client.list_episodes(arguments.run_id, status=arguments.status)]


def get_episode(client: ExperimentClient, arguments: argparse.Namespace) -> Any:
    return client.get_episode(arguments.run_id, arguments.episode_id, first_step=arguments.first_step)


def live_wall(client: ExperimentClient, arguments: argparse.Namespace) -> Any:
    return client.live_wall(arguments.run_id, limit=arguments.limit)


def get_frame(client: ExperimentClient, arguments: argparse.Namespace) -> Any:
    frame = client.get_frame(arguments.frame_digest, size=arguments.size)
    Path(arguments.output).write_bytes(frame)
    return {"frame": arguments.frame_digest, "bytes": len(frame), "output": arguments.output}


def cohort(client: ExperimentClient, arguments: argparse.Namespace) -> Any:
    return client.cohort(arguments.run_id, termination=arguments.termination, task_id=arguments.task_id,
                         template=arguments.template, difficulty_key=arguments.difficulty_key,
                         category=arguments.category, policy_version=arguments.policy_version, limit=arguments.limit)


def get_provenance(client: ExperimentClient, arguments: argparse.Namespace) -> Any:
    return client.get_provenance(arguments.run_id, arguments.episode_id)


def get_step_prompt(client: ExperimentClient, arguments: argparse.Namespace) -> Any:
    return client.get_step_prompt(arguments.run_id, arguments.episode_id, arguments.step_index)


def review_episode(client: ExperimentClient, arguments: argparse.Namespace) -> Any:
    label = json.loads(arguments.label_json) if arguments.label_json else None
    return client.review_episode(arguments.run_id, arguments.episode_id, arguments.action, arguments.note, label)


def analyze_failures(client: ExperimentClient, arguments: argparse.Namespace) -> Any:
    return client.analyze_failures(arguments.run_id)


def get_failures(client: ExperimentClient, arguments: argparse.Namespace) -> Any:
    return client.get_failures(arguments.run_id)


def run_report(client: ExperimentClient, arguments: argparse.Namespace) -> Any:
    return client.run_report(arguments.run_id)


def compare_policy_versions(client: ExperimentClient, arguments: argparse.Namespace) -> Any:
    return client.compare_policy_versions(arguments.run_id)


def compare_runs(client: ExperimentClient, arguments: argparse.Namespace) -> Any:
    return client.compare_runs(arguments.run_ids)


def list_review_items(client: ExperimentClient, arguments: argparse.Namespace) -> Any:
    return client.list_review_items(queue=arguments.queue, status=arguments.status, run_id=arguments.run_id,
                                    episode_id=arguments.episode_id)


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
    for handler in (save_spec, get_spec, preflight, dry_run, launch, get_experiment, approve_experiment, list_runs,
                    get_run, get_run_spec, control_run, run_events, list_episodes, get_episode, live_wall, get_frame,
                    cohort, get_provenance, get_step_prompt, review_episode, analyze_failures, get_failures,
                    run_report, compare_policy_versions, compare_runs, list_review_items, list_catalog,
                    list_endpoints, register_endpoint, test_endpoint, set_endpoint_key)
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

    experiments = groups.add_parser("experiments", help="read and approve experiments")
    experiment_commands = experiments.add_subparsers(dest="command", required=True)
    add_operation(experiment_commands, "get", get_experiment, "read an experiment").add_argument("experiment_id")
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
    live_parser = add_operation(run_commands, "live", live_wall, "show the latest frame of running episodes")
    live_parser.add_argument("run_id")
    live_parser.add_argument("--limit", type=int, default=24)
    add_operation(run_commands, "report", run_report, "eval report with confidence intervals").add_argument("run_id")

    episodes = groups.add_parser("episodes", help="list and read episodes")
    episode_commands = episodes.add_subparsers(dest="command", required=True)
    episodes_list = add_operation(episode_commands, "list", list_episodes, "list a run's episodes")
    episodes_list.add_argument("run_id")
    episodes_list.add_argument("--status", choices=("running", "finished"), default=None)
    episode_get = add_operation(episode_commands, "get", get_episode, "read an episode with its steps")
    episode_get.add_argument("run_id")
    episode_get.add_argument("episode_id")
    episode_get.add_argument("--first-step", type=int, default=0)
    cohort_parser = add_operation(episode_commands, "cohort", cohort, "filter finished episodes with filmstrips")
    cohort_parser.add_argument("run_id")
    for filter_name in ("termination", "task-id", "template", "difficulty-key", "category"):
        cohort_parser.add_argument(f"--{filter_name}", default=None)
    cohort_parser.add_argument("--policy-version", type=int, default=None)
    cohort_parser.add_argument("--limit", type=int, default=50)
    provenance_parser = add_operation(episode_commands, "provenance", get_provenance, "score, checks, states, diff")
    provenance_parser.add_argument("run_id")
    provenance_parser.add_argument("episode_id")
    prompt_parser = add_operation(episode_commands, "prompt", get_step_prompt, "one step's exact model input")
    prompt_parser.add_argument("run_id")
    prompt_parser.add_argument("episode_id")
    prompt_parser.add_argument("step_index", type=int)
    review_parser = add_operation(episode_commands, "review", review_episode, "flag, label or file an episode")
    review_parser.add_argument("run_id")
    review_parser.add_argument("episode_id")
    review_parser.add_argument("action", choices=("flag", "label", "verifier_bug", "task_draft", "audit_sample"))
    review_parser.add_argument("--note", default="")
    review_parser.add_argument("--label-json", default=None)

    failures = groups.add_parser("failures", help="label and cluster failures")
    failure_commands = failures.add_subparsers(dest="command", required=True)
    add_operation(failure_commands, "analyze", analyze_failures, "label failures now").add_argument("run_id")
    add_operation(failure_commands, "get", get_failures, "read the stored analysis").add_argument("run_id")

    comparisons = groups.add_parser("compare", help="compare runs or policy versions")
    compare_commands = comparisons.add_subparsers(dest="command", required=True)
    add_operation(compare_commands, "runs", compare_runs, "runs side by side").add_argument("run_ids", nargs="+")
    add_operation(compare_commands, "versions", compare_policy_versions, "policy versions in one run").add_argument(
        "run_id")

    reviews = groups.add_parser("review", help="review queues")
    review_commands = reviews.add_subparsers(dest="command", required=True)
    review_list = add_operation(review_commands, "list", list_review_items, "list review items")
    for filter_name in ("queue", "status", "run-id", "episode-id"):
        review_list.add_argument(f"--{filter_name}", default=None)

    frames = groups.add_parser("frames", help="download frames")
    frame_commands = frames.add_subparsers(dest="command", required=True)
    frame_get = add_operation(frame_commands, "get", get_frame, "save a frame to a file")
    frame_get.add_argument("frame_digest")
    frame_get.add_argument("--size", choices=("full", "thumbnail"), default="full")
    frame_get.add_argument("--output", required=True)

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
