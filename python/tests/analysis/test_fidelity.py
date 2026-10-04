import math

import pytest

from rlworld.analysis import CloneFidelity, FidelityError, clone_fidelity, mcnemar_p_value


@pytest.mark.parametrize("clone_only, real_only, expected", [
    (0, 0, 1.0),
    (1, 0, 1.0),
    (0, 6, 2 / 64),
    (6, 0, 2 / 64),
    (3, 3, 1.0),
    (8, 1, 2 * (1 + 9) / 512),
    (10, 2, 2 * (1 + 12 + 66) / 4096),
])
def test_mcnemar_is_the_exact_two_sided_binomial(clone_only, real_only, expected):
    assert math.isclose(mcnemar_p_value(clone_only, real_only), expected)


def test_a_faithful_clone_scores_like_the_real_app():
    rates = [(1.0, 1.0)] * 6 + [(0.0, 0.0)] * 3 + [(1.0, 0.0), (0.0, 1.0)]
    report = clone_fidelity("crm", "run_clone", "run_real", rates)
    assert isinstance(report, CloneFidelity)
    assert (report.tasks, report.clone_only, report.real_only) == (11, 1, 1)
    assert math.isclose(report.clone_success_rate, 7 / 11) and math.isclose(report.real_success_rate, 7 / 11)
    assert (report.gap, report.fidelity, report.p_value, report.large_gap) == (0.0, 1.0, 1.0, False)
    assert math.isclose(report.agreement, 9 / 11)


def test_an_easier_clone_shows_a_large_significant_gap():
    rates = [(1.0, 0.0)] * 10 + [(1.0, 1.0)] * 5 + [(0.0, 0.0)] * 5
    report = clone_fidelity("crm", "run_clone", "run_real", rates)
    assert math.isclose(report.gap, 0.5) and math.isclose(report.fidelity, 0.5)
    assert (report.clone_only, report.real_only, report.agreement) == (10, 0, 0.5)
    assert report.p_value < 0.01 and report.large_gap


def test_wide_gaps_from_few_tasks_are_not_called_large():
    report = clone_fidelity("crm", "run_clone", "run_real", [(1.0, 0.0), (1.0, 0.0), (0.0, 0.0)])
    assert report.gap > 0.5 and report.p_value == 0.5 and not report.large_gap


def test_small_significant_gaps_stay_within_tolerance():
    rates = [(1.0, 0.0)] * 6 + [(1.0, 1.0)] * 94
    report = clone_fidelity("crm", "run_clone", "run_real", rates, max_gap=0.1)
    assert report.p_value < 0.05 and math.isclose(report.gap, 0.06) and not report.large_gap
    assert clone_fidelity("crm", "run_clone", "run_real", rates, max_gap=0.05).large_gap


def test_task_rates_average_and_half_solved_tasks_count_as_solved():
    report = clone_fidelity("files", "a", "b", [(0.5, 0.25), (1.0, 0.75)])
    assert math.isclose(report.clone_success_rate, 0.75) and math.isclose(report.real_success_rate, 0.5)
    assert (report.clone_only, report.real_only) == (1, 0)


def test_runs_that_share_no_task_cannot_be_compared():
    with pytest.raises(FidelityError, match="share no scored task"):
        clone_fidelity("crm", "run_clone", "run_real", [])
