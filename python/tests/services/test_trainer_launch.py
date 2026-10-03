import pytest
import yaml

from rlworld.services.trainer_launch import (
    LaunchContext,
    TrainerLauncher,
    TrainerTemplate,
    kubernetes_name,
    render_kubernetes,
    render_skypilot,
    render_slurm,
)

TEMPLATE = TrainerTemplate(
    name="verl_grpo_7b_vlm",
    image="registry.example.com/verl:0.4",
    command=("python", "-m", "verl.trainer.main_ppo", "--config", "grpo config.yaml"),
    nodes=2,
    time_limit_hours=12,
    environment={"WANDB_PROJECT": "recon"},
)
CONTEXT = LaunchContext(
    run_id="experiment_000001_run_000", tenant="Finance Lab", cost_center="research/rl",
    fleet_url="http://fleetd.internal", experimentd_url="http://experimentd.internal",
    policy_endpoint="http://vllm.internal/v1", run_token="secret_token_value",
    token_file_path="/run/rlworld/experiment_000001_run_000.token",
)


class RecordingRunner:
    def __init__(self, outputs=None):
        self.calls = []
        self.outputs = outputs or {}

    def run(self, argv, working_directory):
        self.calls.append((argv, working_directory))
        return self.outputs.get(argv[0], "")


def test_kubernetes_jobs_keep_the_token_in_a_secret():
    rendered = render_kubernetes(TEMPLATE, CONTEXT)
    secret, job = list(yaml.safe_load_all(next(iter(rendered.files.values()))))
    assert rendered.job_name == "rlworld-experiment-000001-run-000"
    assert secret["kind"] == "Secret" and secret["stringData"]["run_token"] == "secret_token_value"
    container = job["spec"]["template"]["spec"]["containers"][0]
    environment = {entry["name"]: entry for entry in container["env"]}
    assert environment["RLWORLD_RUN_TOKEN"]["valueFrom"]["secretKeyRef"]["name"] == f"{rendered.job_name}-token"
    assert "secret_token_value" not in yaml.safe_dump(job)
    assert environment["WANDB_PROJECT"]["value"] == "recon"
    assert container["resources"]["limits"]["nvidia.com/gpu"] == 8
    assert (job["spec"]["completions"], job["spec"]["completionMode"]) == (2, "Indexed")
    assert job["spec"]["activeDeadlineSeconds"] == 12 * 3600
    assert job["metadata"]["labels"] == {"rlworld/run": "experiment_000001_run_000",
                                         "rlworld/tenant": "Finance_Lab", "rlworld/cost-center": "research_rl"}
    assert rendered.submit_argv == ("kubectl", "apply", "-f", f"{rendered.job_name}.yaml")


def test_slurm_scripts_read_the_token_from_a_file():
    rendered = render_slurm(TEMPLATE, CONTEXT)
    script = next(iter(rendered.files.values()))
    assert "#SBATCH --nodes=2" in script
    assert "#SBATCH --gres=gpu:8" in script
    assert "#SBATCH --time=12:00:00" in script
    assert "export RLWORLD_RUN_TOKEN_FILE=/run/rlworld/experiment_000001_run_000.token" in script
    assert "secret_token_value" not in script
    assert "srun python -m verl.trainer.main_ppo --config 'grpo config.yaml'" in script


def test_skypilot_tasks():
    task = yaml.safe_load(next(iter(render_skypilot(TEMPLATE, CONTEXT).files.values())))
    assert task["resources"]["accelerators"] == "H100:8"
    assert task["num_nodes"] == 2
    assert task["envs"]["RLWORLD_RUN_ID"] == "experiment_000001_run_000"
    assert "secret_token_value" not in yaml.safe_dump(task)


def test_names_are_dns_safe():
    assert kubernetes_name("Experiment_01__Run_A") == "rlworld-experiment-01-run-a"
    assert len(kubernetes_name("x" * 200)) <= 63


def test_launcher_writes_files_submits_and_cancels(tmp_path):
    runner = RecordingRunner({"sbatch": "Submitted batch job 4242\n"})
    launcher = TrainerLauncher({TEMPLATE.name: TEMPLATE}, runner, tmp_path)
    job_id, rendered = launcher.launch("slurm", TEMPLATE.name, CONTEXT)
    assert job_id == "4242"
    assert (tmp_path / next(iter(rendered.files))).read_text().startswith("#!/bin/bash")
    assert runner.calls[0] == (["sbatch", "rlworld_experiment_000001_run_000.sbatch"], tmp_path)
    launcher.cancel("slurm", job_id)
    assert runner.calls[-1][0] == ["scancel", "4242"]
    kubernetes_job, _ = launcher.launch("kubernetes", TEMPLATE.name, CONTEXT)
    assert kubernetes_job == "rlworld-experiment-000001-run-000"
    launcher.cancel("kubernetes", kubernetes_job)
    assert runner.calls[-1][0][:3] == ["kubectl", "delete", "job"]


def test_external_trainers_get_no_submission(tmp_path):
    runner = RecordingRunner()
    job_id, rendered = TrainerLauncher({TEMPLATE.name: TEMPLATE}, runner, tmp_path).launch(
        "external", TEMPLATE.name, CONTEXT)
    assert (job_id, rendered.files, runner.calls) == ("external", {}, [])


def test_launch_errors(tmp_path):
    launcher = TrainerLauncher({TEMPLATE.name: TEMPLATE}, RecordingRunner({"sbatch": "error"}), tmp_path)
    with pytest.raises(RuntimeError):
        launcher.launch("slurm", TEMPLATE.name, CONTEXT)
    with pytest.raises(KeyError):
        launcher.launch("kubernetes", "missing_template", CONTEXT)
