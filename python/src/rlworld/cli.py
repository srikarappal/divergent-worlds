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
    """Control values are JSON when they parse (such as 4000) and plain strings otherwise (such as eval)."""
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


def start_takeover(client: ExperimentClient, arguments: argparse.Namespace) -> Any:
    return client.start_takeover(arguments.run_id, arguments.episode_id)


def end_takeover(client: ExperimentClient, arguments: argparse.Namespace) -> Any:
    return client.end_takeover(arguments.run_id, arguments.episode_id)


def start_authoring(client: ExperimentClient, arguments: argparse.Namespace) -> Any:
    return client.start_authoring(arguments.spec_digest, task_index=arguments.task_index)


def get_authoring(client: ExperimentClient, arguments: argparse.Namespace) -> Any:
    return client.get_authoring(arguments.session_id)


def begin_recording(client: ExperimentClient, arguments: argparse.Namespace) -> Any:
    return client.begin_recording(arguments.session_id, kind=arguments.kind)


def end_recording(client: ExperimentClient, arguments: argparse.Namespace) -> Any:
    return client.end_recording(arguments.session_id, arguments.recording_id)


def run_authoring_gates(client: ExperimentClient, arguments: argparse.Namespace) -> Any:
    return client.run_authoring_gates(arguments.session_id)


def submit_authoring(client: ExperimentClient, arguments: argparse.Namespace) -> Any:
    return client.submit_authoring(arguments.session_id)


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


def clone_fidelity(client: ExperimentClient, arguments: argparse.Namespace) -> Any:
    return client.clone_fidelity(arguments.app, arguments.clone_run_id, arguments.real_run_id)


def list_review_items(client: ExperimentClient, arguments: argparse.Namespace) -> Any:
    return client.list_review_items(queue=arguments.queue, status=arguments.status, run_id=arguments.run_id,
                                    episode_id=arguments.episode_id)


def auth_config(client: ExperimentClient, arguments: argparse.Namespace) -> Any:
    return client.auth_config()


def me(client: ExperimentClient, arguments: argparse.Namespace) -> Any:
    return client.me()


def list_experiments(client: ExperimentClient, arguments: argparse.Namespace) -> Any:
    return client.list_experiments(tenant=arguments.tenant)


def file_review_item(client: ExperimentClient, arguments: argparse.Namespace) -> Any:
    return client.file_review_item(arguments.source, arguments.title, queue=arguments.queue, run_id=arguments.run_id,
                                   episode_ids=arguments.episode_id, note=arguments.note)


def update_review_item(client: ExperimentClient, arguments: argparse.Namespace) -> Any:
    label = json.loads(arguments.label_json) if arguments.label_json else None
    return client.update_review_item(arguments.item_id, status=arguments.status, note=arguments.note, label=label)


def calibration(client: ExperimentClient, arguments: argparse.Namespace) -> Any:
    return client.calibration(run_id=arguments.run_id)


def export_run(client: ExperimentClient, arguments: argparse.Namespace) -> Any:
    return client.export_run(arguments.run_id, arguments.kind)


def list_roles(client: ExperimentClient, arguments: argparse.Namespace) -> Any:
    return client.list_roles()


def set_roles(client: ExperimentClient, arguments: argparse.Namespace) -> Any:
    return client.set_roles(arguments.subject, [role for role in arguments.roles.split(",") if role])


def list_data_policies(client: ExperimentClient, arguments: argparse.Namespace) -> Any:
    return client.list_data_policies()


def set_data_policy(client: ExperimentClient, arguments: argparse.Namespace) -> Any:
    return client.set_data_policy(arguments.tenant, yaml.safe_load(Path(arguments.policy_file).read_text()))


def audit_log(client: ExperimentClient, arguments: argparse.Namespace) -> Any:
    return client.audit_log(actor=arguments.actor, action=arguments.action, since=arguments.since,
                            limit=arguments.limit)


def pool_status(client: ExperimentClient, arguments: argparse.Namespace) -> Any:
    return client.pool_status()


def sweep_retention(client: ExperimentClient, arguments: argparse.Namespace) -> Any:
    return client.sweep_retention()


def list_alerts(client: ExperimentClient, arguments: argparse.Namespace) -> Any:
    return client.list_alerts(run_id=arguments.run_id, experiment_id=arguments.experiment_id, status=arguments.status)


def acknowledge_alert(client: ExperimentClient, arguments: argparse.Namespace) -> Any:
    return client.acknowledge_alert(arguments.alert_id)


def fleet_admission(client: ExperimentClient, arguments: argparse.Namespace) -> Any:
    return client.fleet_admission()


def set_tenant_quota(client: ExperimentClient, arguments: argparse.Namespace) -> Any:
    return client.set_tenant_quota(arguments.tenant, arguments.quota)


def frame_grant(client: ExperimentClient, arguments: argparse.Namespace) -> Any:
    return client.frame_grant()


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
                    cohort, get_provenance, get_step_prompt, review_episode, start_takeover, end_takeover,
                    start_authoring, get_authoring, begin_recording, end_recording, run_authoring_gates,
                    submit_authoring, analyze_failures, get_failures,
                    run_report, compare_policy_versions, compare_runs, clone_fidelity, list_review_items, list_catalog,
                    list_endpoints, register_endpoint, test_endpoint, set_endpoint_key, auth_config, me,
                    list_experiments, file_review_item, update_review_item, calibration, export_run, list_roles,
                    set_roles, list_data_policies, set_data_policy, audit_log, pool_status, sweep_retention,
                    list_alerts, acknowledge_alert, fleet_admission, set_tenant_quota, frame_grant)
}


def add_operation(commands: Any, command_name: str, handler: OperationHandler,
                  help_text: str) -> argparse.ArgumentParser:
    command_parser = commands.add_parser(command_name, help=help_text)
    command_parser.set_defaults(operation=handler.__name__)
    return command_parser


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="rlworld", description="Configure, launch and watch experiments.")
    parser.add_argument("--api-url", default=os.environ.get("RLWORLD_API_URL", DEFAULT_API_URL))
    parser.add_argument("--token", default=os.environ.get("RLWORLD_ACCESS_TOKEN"),
                        help="OIDC access token; defaults to RLWORLD_ACCESS_TOKEN")
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
    add_operation(experiment_commands, "list", list_experiments, "list experiments").add_argument("--tenant",
                                                                                                 default=None)
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
    export_parser = add_operation(run_commands, "export", export_run, "export SFT rows or preference pairs")
    export_parser.add_argument("run_id")
    export_parser.add_argument("kind", choices=("sft", "preferences"))
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
    for command_name, handler, help_text in (("takeover", start_takeover, "drive a running episode's env yourself"),
                                             ("end-takeover", end_takeover, "hand back and keep your demonstration")):
        takeover_parser = add_operation(episode_commands, command_name, handler, help_text)
        takeover_parser.add_argument("run_id")
        takeover_parser.add_argument("episode_id")

    authoring = groups.add_parser("authoring", help="author a task: record solutions, run every gate, submit")
    authoring_commands = authoring.add_subparsers(dest="command", required=True)
    authoring_start = add_operation(authoring_commands, "start", start_authoring, "start from a saved spec")
    authoring_start.add_argument("spec_digest")
    authoring_start.add_argument("--task-index", type=int, default=0)
    add_operation(authoring_commands, "get", get_authoring, "read a session").add_argument("session_id")
    record_parser = add_operation(authoring_commands, "record", begin_recording, "drive a fresh dev env through noVNC")
    record_parser.add_argument("session_id")
    record_parser.add_argument("--kind", choices=("oracle", "alternative", "near_miss"), default="oracle")
    stop_parser = add_operation(authoring_commands, "stop", end_recording, "keep the recording, give the env back")
    stop_parser.add_argument("session_id")
    stop_parser.add_argument("recording_id")
    add_operation(authoring_commands, "gates", run_authoring_gates, "run every gate in fresh envs").add_argument(
        "session_id")
    add_operation(authoring_commands, "submit", submit_authoring, "submit for human review").add_argument(
        "session_id")

    failures = groups.add_parser("failures", help="label and cluster failures")
    failure_commands = failures.add_subparsers(dest="command", required=True)
    add_operation(failure_commands, "analyze", analyze_failures, "label failures now").add_argument("run_id")
    add_operation(failure_commands, "get", get_failures, "read the stored analysis").add_argument("run_id")

    comparisons = groups.add_parser("compare", help="compare runs or policy versions")
    compare_commands = comparisons.add_subparsers(dest="command", required=True)
    add_operation(compare_commands, "runs", compare_runs, "runs side by side").add_argument("run_ids", nargs="+")
    add_operation(compare_commands, "versions", compare_policy_versions, "policy versions in one run").add_argument(
        "run_id")
    fidelity_parser = add_operation(compare_commands, "fidelity", clone_fidelity,
                                    "success on an app clone against the real app")
    for argument_name in ("app", "clone_run_id", "real_run_id"):
        fidelity_parser.add_argument(argument_name)

    reviews = groups.add_parser("review", help="review queues")
    review_commands = reviews.add_subparsers(dest="command", required=True)
    review_list = add_operation(review_commands, "list", list_review_items, "list review items")
    for filter_name in ("queue", "status", "run-id", "episode-id"):
        review_list.add_argument(f"--{filter_name}", default=None)
    review_file = add_operation(review_commands, "file", file_review_item, "file an item for human review")
    review_file.add_argument("source", choices=("gates", "alert"))
    review_file.add_argument("--title", required=True)
    review_file.add_argument("--queue", default="review")
    review_file.add_argument("--run-id", default="")
    review_file.add_argument("--episode-id", action="append", default=[])
    review_file.add_argument("--note", default="")
    review_update = add_operation(review_commands, "update", update_review_item, "resolve, dismiss or label")
    review_update.add_argument("item_id")
    review_update.add_argument("--status", choices=("open", "resolved", "dismissed"), default=None)
    review_update.add_argument("--note", default=None)
    review_update.add_argument("--label-json", default=None)
    add_operation(review_commands, "calibration", calibration, "agreement with human labels").add_argument(
        "--run-id", default=None)

    auth = groups.add_parser("auth", help="sign in details")
    auth_commands = auth.add_subparsers(dest="command", required=True)
    add_operation(auth_commands, "config", auth_config, "how to sign in")
    add_operation(auth_commands, "me", me, "who is signed in")

    admin = groups.add_parser("admin", help="roles, data policies, audit, pools and retention")
    admin_commands = admin.add_subparsers(dest="command", required=True)
    add_operation(admin_commands, "roles", list_roles, "roles granted by admins")
    roles_parser = add_operation(admin_commands, "set-roles", set_roles, "grant comma separated roles")
    roles_parser.add_argument("subject")
    roles_parser.add_argument("roles")
    add_operation(admin_commands, "data-policies", list_data_policies, "per tenant data policies")
    policy_parser = add_operation(admin_commands, "set-data-policy", set_data_policy, "set from a YAML or JSON file")
    policy_parser.add_argument("tenant")
    policy_parser.add_argument("policy_file")
    audit_parser = add_operation(admin_commands, "audit", audit_log, "the audit log, newest first")
    audit_parser.add_argument("--actor", default=None)
    audit_parser.add_argument("--action", default=None)
    audit_parser.add_argument("--since", type=float, default=None)
    audit_parser.add_argument("--limit", type=int, default=500)
    add_operation(admin_commands, "pools", pool_status, "free seats and identities")
    add_operation(admin_commands, "sweep-retention", sweep_retention, "apply retention windows now")

    alerts = groups.add_parser("alerts", help="alerts raised on runs")
    alert_commands = alerts.add_subparsers(dest="command", required=True)
    alert_list = add_operation(alert_commands, "list", list_alerts, "list alerts")
    alert_list.add_argument("--run-id", default=None)
    alert_list.add_argument("--experiment-id", default=None)
    alert_list.add_argument("--status", choices=("open", "acknowledged"), default=None)
    add_operation(alert_commands, "ack", acknowledge_alert, "acknowledge an alert").add_argument("alert_id")

    fleet = groups.add_parser("fleet", help="fleet admission")
    fleet_commands = fleet.add_subparsers(dest="command", required=True)
    add_operation(fleet_commands, "admission", fleet_admission, "capacity, quotas and usage")
    quota_parser = add_operation(fleet_commands, "set-quota", set_tenant_quota, "set a tenant quota")
    quota_parser.add_argument("tenant")
    quota_parser.add_argument("quota", type=int)

    frames = groups.add_parser("frames", help="download frames")
    frame_commands = frames.add_subparsers(dest="command", required=True)
    frame_get = add_operation(frame_commands, "get", get_frame, "save a frame to a file")
    frame_get.add_argument("frame_digest")
    frame_get.add_argument("--size", choices=("full", "thumbnail"), default="full")
    frame_get.add_argument("--output", required=True)
    add_operation(frame_commands, "grant", frame_grant, "a short lived grant to view frames")

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
        result = HANDLERS[arguments.operation](client or ExperimentClient(arguments.api_url,
                                                                          access_token=arguments.token), arguments)
    except ExperimentApiError as error:
        print(json.dumps({"error": str(error), "problems": error.problems}, indent=2), file=sys.stderr)
        return 1
    if result is not None:
        print(json.dumps(to_json(result), indent=2, default=str))
    return 0


if __name__ == "__main__":
    sys.exit(main())
