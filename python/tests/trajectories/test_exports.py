import pytest
from support.trajectory_fakes import episode, reward, step

from rlworld.trajectories import eval_report, pass_at_k, preference_pairs, retention_class, sft_rows, wilson_interval


def test_sft_rows_come_only_from_trainable_successes():
    episodes = [
        episode("winner", reward=reward(1.0), steps=(step(0), step(1, with_tokens=False))),
        episode("loser", reward=reward(0.0, "failure"), steps=(step(0),)),
        episode("crashed", reward=reward(0.0, "env_error", counts_for_training=False), steps=(step(0),)),
        episode("helped", reward=reward(1.0), human_intervened=True, steps=(step(0),)),
    ]
    rows = sft_rows(episodes)
    assert [(row.episode_id, row.step_index) for row in rows] == [("winner", 0), ("winner", 1)]
    assert rows[0].completion_token_ids == (3,)
    assert rows[1].completion_token_ids is None


def test_preference_pairs_match_best_against_worst_per_group():
    episodes = [
        episode("a", group_id="g1", reward=reward(1.0)),
        episode("b", group_id="g1", reward=reward(0.0, "failure")),
        episode("c", group_id="g1", reward=reward(0.0, "failure")),
        episode("d", group_id="g1", reward=reward(0.5, "failure")),
        episode("e", group_id="g2", reward=reward(1.0)),
        episode("f", group_id="g2", reward=reward(1.0)),
        episode("g", group_id=None, reward=reward(0.0, "failure")),
        episode("h", group_id="g3", reward=reward(1.0)),
        episode("i", group_id="g3", reward=reward(0.0, "env_error", counts_for_training=False)),
    ]
    pairs = preference_pairs(episodes)
    assert [(pair.chosen_episode_id, pair.rejected_episode_id) for pair in pairs] == [("a", "b"), ("a", "c")]
    assert preference_pairs(episodes, max_pairs_per_group=1)[0].rejected_episode_id == "b"


def test_wilson_interval_and_pass_at_k():
    low, high = wilson_interval(8, 10)
    assert (low, high) == (pytest.approx(0.4902, abs=1e-4), pytest.approx(0.9433, abs=1e-4))
    assert wilson_interval(0, 0) == (0.0, 1.0)
    assert pass_at_k(4, 1, 1) == pytest.approx(0.25)
    assert pass_at_k(4, 1, 4) == 1.0
    assert pass_at_k(4, 0, 2) == 0.0
    assert pass_at_k(5, 2, 2) == pytest.approx(1 - 3 / 10)


def test_eval_report_breaks_down_and_excludes_noise():
    episodes = [
        episode("t1_s0", task_id="t1", reward=reward(1.0), difficulty_key="easy"),
        episode("t1_s1", task_id="t1", reward=reward(0.0, "failure"), difficulty_key="easy"),
        episode("t2_s0", task_id="t2", template="tasks.crm.update_deal", reward=reward(0.0, "max_steps"),
                difficulty_key="hard"),
        episode("t2_s1", task_id="t2", template="tasks.crm.update_deal", reward=reward(0.0, "failure"),
                difficulty_key="hard"),
        episode("t2_s2", task_id="t2", reward=reward(0.0, "env_error", counts_for_training=False)),
        episode("t2_s3", task_id="t2", reward=reward(1.0), human_intervened=True),
        episode("unscored"),
    ]
    report = eval_report(episodes, k_values=(1, 2, 3))
    assert (report.overall.episodes, report.overall.successes) == (4, 1)
    assert report.overall.terminations == {"success": 1, "failure": 2, "max_steps": 1}
    assert report.by_template["tasks.crm.update_deal"].success_rate == 0.0
    assert report.by_difficulty["easy"].success_rate == 0.5
    assert report.pass_at_k[1] == pytest.approx((0.5 + 0.0) / 2)
    assert report.pass_at_k[2] == pytest.approx((1.0 + 0.0) / 2)
    assert 3 not in report.pass_at_k
    assert (report.excluded_env_errors, report.excluded_human_intervened) == (1, 1)


def test_retention_keeps_full_resolution_where_it_matters():
    successful_training = episode(reward=reward(1.0))
    assert retention_class(successful_training, full_sample_rate=0.05, random_value=0.5) == "thumbnail"
    assert retention_class(successful_training, full_sample_rate=0.05, random_value=0.01) == "full"
    assert retention_class(episode(reward=reward(0.0, "failure")), 0.05, 0.9) == "full"
    assert retention_class(episode(run_type="eval", reward=reward(1.0)), 0.05, 0.9) == "full"
    assert retention_class(episode(reward=reward(1.0), human_intervened=True), 0.05, 0.9) == "full"
