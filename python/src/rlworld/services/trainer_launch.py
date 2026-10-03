"""Trainer launcher (DESIGN.md sections 6.7 and 6.12).

RL runs start trainer jobs from named templates through a pluggable backend:
Kubernetes indexed jobs, Slurm batch scripts or SkyPilot tasks. The run token
never goes into a manifest in plain text: Kubernetes gets a Secret, Slurm and
SkyPilot get a token file path. External trainers skip submission and attach
with the token. The platform launches trainers but never becomes one.
"""

import re
import shlex
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal, Protocol

import yaml
from pydantic import BaseModel, ConfigDict, Field, PositiveInt

LauncherKind = Literal["kubernetes", "slurm", "skypilot", "external"]
SLURM_JOB_ID_PATTERN = re.compile(r"Submitted batch job (\d+)")


class TrainerTemplate(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    name: str = Field(pattern=r"^[a-z0-9_]+$")
    image: str
    command: tuple[str, ...] = Field(min_length=1)
    accelerator: str = "H100"
    gpus_per_node: PositiveInt = 8
    nodes: PositiveInt = 1
    time_limit_hours: PositiveInt = 24
    environment: dict[str, str] = Field(default_factory=dict)


@dataclass(frozen=True)
class LaunchContext:
    run_id: str
    tenant: str
    cost_center: str
    fleet_url: str
    experimentd_url: str
    policy_endpoint: str
    run_token: str
    token_file_path: str


@dataclass(frozen=True)
class RenderedLaunch:
    launcher: LauncherKind
    files: dict[str, str]
    submit_argv: tuple[str, ...]
    job_name: str = ""


class CommandRunner(Protocol):
    def run(self, argv: list[str], working_directory: Path) -> str: ...


def kubernetes_name(run_id: str) -> str:
    """DNS 1123 label: lowercase letters, digits and dashes, at most 63 characters."""
    return ("rlworld-" + re.sub(r"[^a-z0-9]+", "-", run_id.lower())).strip("-")[:63]


def label_value(text: str) -> str:
    return re.sub(r"[^A-Za-z0-9_.]+", "_", text)[:63].strip("_.") or "unset"


def platform_environment(context: LaunchContext, template: TrainerTemplate) -> dict[str, str]:
    return {
        **template.environment,
        "RLWORLD_RUN_ID": context.run_id,
        "RLWORLD_FLEET_URL": context.fleet_url,
        "RLWORLD_EXPERIMENTD_URL": context.experimentd_url,
        "RLWORLD_POLICY_ENDPOINT": context.policy_endpoint,
    }


def render_kubernetes(template: TrainerTemplate, context: LaunchContext) -> RenderedLaunch:
    job_name = kubernetes_name(context.run_id)
    labels = {"rlworld/run": label_value(context.run_id), "rlworld/tenant": label_value(context.tenant),
              "rlworld/cost-center": label_value(context.cost_center)}
    platform_variables = sorted(platform_environment(context, template).items())
    environment = [{"name": name, "value": value} for name, value in platform_variables]
    environment.append({"name": "RLWORLD_RUN_TOKEN",
                        "valueFrom": {"secretKeyRef": {"name": f"{job_name}-token", "key": "run_token"}}})
    secret = {"apiVersion": "v1", "kind": "Secret", "metadata": {"name": f"{job_name}-token", "labels": labels},
              "type": "Opaque", "stringData": {"run_token": context.run_token}}
    job_spec: dict[str, Any] = {
        "backoffLimit": 0,
        "ttlSecondsAfterFinished": 86_400,
        "activeDeadlineSeconds": template.time_limit_hours * 3600,
        "template": {
            "metadata": {"labels": labels},
            "spec": {
                "restartPolicy": "Never",
                "containers": [{
                    "name": "trainer",
                    "image": template.image,
                    "command": list(template.command),
                    "env": environment,
                    "resources": {"limits": {"nvidia.com/gpu": template.gpus_per_node}},
                }],
            },
        },
    }
    if template.nodes > 1:
        job_spec.update({"completions": template.nodes, "parallelism": template.nodes, "completionMode": "Indexed"})
    job = {"apiVersion": "batch/v1", "kind": "Job", "metadata": {"name": job_name, "labels": labels}, "spec": job_spec}
    manifest_path = f"{job_name}.yaml"
    return RenderedLaunch("kubernetes", {manifest_path: yaml.safe_dump_all([secret, job], sort_keys=False)},
                          ("kubectl", "apply", "-f", manifest_path), job_name)


def render_slurm(template: TrainerTemplate, context: LaunchContext) -> RenderedLaunch:
    hours = template.time_limit_hours
    script_lines = [
        "#!/bin/bash",
        f"#SBATCH --job-name=rlworld_{context.run_id}",
        f"#SBATCH --nodes={template.nodes}",
        f"#SBATCH --gres=gpu:{template.gpus_per_node}",
        f"#SBATCH --time={hours:02d}:00:00",
        f"#SBATCH --comment=tenant={label_value(context.tenant)},cost_center={label_value(context.cost_center)}",
        *(f"export {name}={shlex.quote(value)}"
          for name, value in sorted(platform_environment(context, template).items())),
        f"export RLWORLD_RUN_TOKEN_FILE={shlex.quote(context.token_file_path)}",
        f"srun {shlex.join(template.command)}",
    ]
    script_path = f"rlworld_{context.run_id}.sbatch"
    return RenderedLaunch("slurm", {script_path: "\n".join(script_lines) + "\n"}, ("sbatch", script_path))


def render_skypilot(template: TrainerTemplate, context: LaunchContext) -> RenderedLaunch:
    task = {
        "name": kubernetes_name(context.run_id),
        "num_nodes": template.nodes,
        "resources": {"accelerators": f"{template.accelerator}:{template.gpus_per_node}"},
        "envs": {**platform_environment(context, template), "RLWORLD_RUN_TOKEN_FILE": context.token_file_path},
        "run": shlex.join(template.command),
    }
    cluster_name = kubernetes_name(context.run_id)
    task_path = f"{cluster_name}.sky.yaml"
    return RenderedLaunch("skypilot", {task_path: yaml.safe_dump(task, sort_keys=False)},
                          ("sky", "launch", "--yes", "--cluster", cluster_name, task_path), cluster_name)


RENDERERS = {"kubernetes": render_kubernetes, "slurm": render_slurm, "skypilot": render_skypilot}


def render_launch(launcher: LauncherKind, template: TrainerTemplate, context: LaunchContext) -> RenderedLaunch:
    if launcher == "external":
        return RenderedLaunch("external", {}, ())
    return RENDERERS[launcher](template, context)


class TrainerLauncher:
    """Writes rendered files into a work directory and submits them with the backend's CLI."""

    def __init__(self, templates: dict[str, TrainerTemplate], runner: CommandRunner, work_directory: Path) -> None:
        self.templates = templates
        self.runner = runner
        self.work_directory = work_directory

    def launch(self, launcher: LauncherKind, template_name: str, context: LaunchContext) -> tuple[str, RenderedLaunch]:
        if template_name not in self.templates:
            raise KeyError(f"unknown trainer template {template_name}")
        rendered = render_launch(launcher, self.templates[template_name], context)
        if launcher == "external":
            return "external", rendered
        self.work_directory.mkdir(parents=True, exist_ok=True)
        for file_name, content in rendered.files.items():
            (self.work_directory / file_name).write_text(content)
        output = self.runner.run(list(rendered.submit_argv), self.work_directory)
        return job_identifier(rendered, output), rendered

    def cancel(self, launcher: LauncherKind, job_id: str) -> None:
        cancel_commands = {
            "kubernetes": ["kubectl", "delete", "job", job_id, "--wait=false"],
            "slurm": ["scancel", job_id],
            "skypilot": ["sky", "down", "--yes", job_id],
        }
        if launcher in cancel_commands:
            self.runner.run(cancel_commands[launcher], self.work_directory)


def job_identifier(rendered: RenderedLaunch, submit_output: str) -> str:
    if rendered.launcher != "slurm":
        return rendered.job_name
    match = SLURM_JOB_ID_PATTERN.search(submit_output)
    if match is None:
        raise RuntimeError(f"sbatch did not report a job id: {submit_output!r}")
    return match.group(1)
