from typing import Any

import pytest

from rlworld.specs import SpecValidationError, parse_spec_document, set_path
from rlworld.specs.models import ClockSpec


def assert_rejected(payload: dict[str, Any], expected_fragment: str) -> None:
    with pytest.raises(SpecValidationError) as raised:
        parse_spec_document(payload)
    assert expected_fragment in str(raised.value)


def test_example_spec_parses(example_payload):
    document = parse_spec_document(example_payload)
    assert document.image.os == "windows"
    assert document.task.allowed_actions == ["computer", "control"]
    assert document.run.group_size == 8
    assert document.world.clock.fixed.isoformat() == "2026-03-14T09:00:00"


def test_fixed_clocks_reach_leases_in_rfc_3339_and_a_time_without_offset_is_utc():
    assert ClockSpec(fixed="2026-03-14T09:00:00").lease_time() == "2026-03-14T09:00:00+00:00"
    assert ClockSpec(fixed="2026-03-14T09:00:00-07:00").lease_time() == "2026-03-14T09:00:00-07:00"
    assert ClockSpec().lease_time() is None


def test_linux_images_pick_their_desktop_and_other_images_keep_the_default(example_payload):
    linux_payload = {**example_payload, "image": {**example_payload["image"], "os": "linux",
                                                    "pool": {"vmm": "firecracker", "cpu_model": "fleet_baseline"},
                                                    "desktop": "xfce"}}
    assert parse_spec_document(linux_payload).image.desktop == "xfce"
    windows_payload = {**example_payload, "image": {**example_payload["image"], "desktop": "xfce"}}
    assert_rejected(windows_payload, "desktop xfce only applies to linux images")


def test_only_text_action_policies_point_on_a_relative_grid(example_payload):
    policy = example_payload["run"]["policy"]
    relative = {**example_payload, "run": {**example_payload["run"],
                                           "policy": {**policy, "action_format": "text_actions",
                                                      "coordinate_mode": "relative_1000"}}}
    assert parse_spec_document(relative).run.policy.coordinate_mode == "relative_1000"
    typed = {**example_payload, "run": {**example_payload["run"],
                                        "policy": {**policy, "action_format": "typed_actions",
                                                   "coordinate_mode": "relative_1000"}}}
    assert_rejected(typed, "coordinate_mode applies to the text_actions format only")


def test_unpinned_base_image_is_rejected(example_payload):
    assert_rejected(set_path(example_payload, "image.base", "windows_server_2025_desktop:stable"), "pinned")


def test_vmm_must_support_the_os_family(example_payload):
    assert_rejected(set_path(example_payload, "image.pool.vmm", "firecracker"), "cannot run windows guests")


def test_qemu_pools_pin_a_machine_type(example_payload):
    assert_rejected(set_path(example_payload, "image.pool.machine", None), "machine type")


def test_linux_images_may_use_qemu_for_gpu_envs(example_payload):
    payload = set_path(example_payload, "image.os", "linux")
    assert parse_spec_document(payload).image.pool.vmm == "qemu_templated"


def test_unknown_keys_are_rejected(example_payload):
    assert_rejected(set_path(example_payload, "task.max_step", 40), "task.max_step")


def test_tasks_must_allow_the_control_modality(example_payload):
    assert_rejected(set_path(example_payload, "task.allowed_actions", ["computer"]), "include control")


def test_tasks_must_not_repeat_a_modality(example_payload):
    payload = set_path(example_payload, "task.allowed_actions", ["computer", "control", "computer"])
    assert_rejected(payload, "repeat")


def test_ambiguous_tasks_need_a_user_simulator(example_payload):
    assert_rejected(set_path(example_payload, "task.user_simulator", None), "user_simulator")


def test_tasks_need_a_reward_source(example_payload):
    payload = set_path(example_payload, "task.verifiers", {"exact": [], "semantic": []})
    payload = set_path(payload, "task.judge_rubric", None)
    assert_rejected(payload, "reward source")


def test_network_fault_ranges_must_be_ordered(example_payload):
    assert_rejected(set_path(example_payload, "task.network_faults.latency_ms", [300, 100]), "latency_ms")


def test_packet_loss_must_stay_within_one(example_payload):
    assert_rejected(set_path(example_payload, "task.network_faults.packet_loss", [0.0, 1.5]), "packet_loss")


def test_allowed_changes_are_slash_separated_paths(example_payload):
    payload = set_path(example_payload, "task.side_effects.allowed_changes", ["/files/report.xlsx"])
    assert_rejected(payload, "task.side_effects.allowed_changes.0")


def test_dns_overrides_need_a_trusted_ca(example_payload):
    assert_rejected(set_path(example_payload, "world.network.trusted_ca", None), "trusted_ca")


def test_allowlist_mode_needs_entries(example_payload):
    assert_rejected(set_path(example_payload, "world.network.internet", "allowlist"), "allowlist entry")


def test_rl_runs_need_token_ids_and_logprobs(example_payload):
    assert_rejected(set_path(example_payload, "run.policy.logprobs", False), "token_ids and logprobs")


def test_rl_runs_need_a_trainer(example_payload):
    assert_rejected(set_path(example_payload, "run.trainer", None), "trainer template")


def test_eval_runs_do_not_take_a_trainer(example_payload):
    assert_rejected(set_path(example_payload, "run.type", "eval"), "only rl runs take a trainer")


def test_concurrency_must_fit_a_group(example_payload):
    assert_rejected(set_path(example_payload, "run.max_concurrent_envs", 4), "max_concurrent_envs")


def test_the_policy_needs_an_action_format(example_payload):
    assert_rejected(set_path(example_payload, "run.policy.action_format", None), "action_format")


def test_moving_model_aliases_are_rejected(example_payload):
    assert_rejected(set_path(example_payload, "run.judge.model", "judge_latest"), "moving alias")


def test_random_sweeps_need_a_sample_count(example_payload):
    assert_rejected(set_path(example_payload, "experiment.sweep_mode", "random"), "random_samples")


def test_grid_sweeps_reject_a_sample_count(example_payload):
    assert_rejected(set_path(example_payload, "experiment.random_samples", 3), "only applies to random")


def test_sweeps_cannot_target_the_experiment_layer(example_payload):
    payload = set_path(example_payload, "experiment.sweep", {"experiment.budget_usd": [1, 2]})
    assert_rejected(payload, "image, world, task or run")


def test_sweep_paths_need_values(example_payload):
    assert_rejected(set_path(example_payload, "experiment.sweep", {"task.max_steps": []}), "at least one value")
