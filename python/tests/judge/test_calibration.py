import pytest

from rlworld.judge import LabeledVerdict, agreement_report, calibration_problems, cohen_kappa


def label_pairs(both_pass: int, both_fail: int, judge_only_pass: int, human_only_pass: int):
    return (
        [(True, True)] * both_pass
        + [(False, False)] * both_fail
        + [(True, False)] * judge_only_pass
        + [(False, True)] * human_only_pass
    )


def test_kappa_on_a_known_table():
    assert cohen_kappa(label_pairs(45, 40, 10, 5)) == pytest.approx(0.7)


def test_kappa_edges():
    assert cohen_kappa(label_pairs(10, 10, 0, 0)) == 1.0
    assert cohen_kappa(label_pairs(5, 0, 0, 0)) == 1.0
    assert cohen_kappa(label_pairs(1, 1, 1, 1)) == pytest.approx(0.0)
    with pytest.raises(ValueError):
        cohen_kappa([])


def labels_for(item_id: str, pairs) -> list[LabeledVerdict]:
    return [
        LabeledVerdict(episode_id=f"episode_{index}", item_id=item_id, judge_passed=judge, human_passed=human)
        for index, (judge, human) in enumerate(pairs)
    ]


def test_report_has_a_row_per_item_and_overall():
    labels = labels_for("tone", label_pairs(45, 40, 10, 5)) + labels_for("facts", label_pairs(50, 50, 0, 0))
    report = agreement_report(labels)
    assert set(report) == {"tone", "facts", "overall"}
    assert report["tone"].false_passes == 10
    assert report["tone"].false_fails == 5
    assert report["facts"].kappa == 1.0
    assert report["overall"].label_count == 200


def test_calibration_problems():
    labels = labels_for("tone", label_pairs(45, 40, 10, 5)) + labels_for("facts", label_pairs(50, 50, 0, 0))
    report = agreement_report(labels)
    problems = calibration_problems(report, minimum_kappa=0.8, episode_count=100)
    assert "only 100 labeled episodes, need 300" in problems
    assert "tone: kappa 0.70 is below 0.8" in problems
    assert not any(problem.startswith("facts") for problem in problems)
    assert calibration_problems(agreement_report(labels_for("facts", label_pairs(200, 150, 0, 0))),
                                episode_count=350) == []
