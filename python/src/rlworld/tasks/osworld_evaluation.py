"""OSWorld evaluators scored host side over what the guest read (DESIGN.md sections 6.3 and 6.9).

An imported OSWorld task keeps its evaluator as data: metric functions, the
getters that feed them and how their scores combine. The guest hands back the
files and command outputs the getters name under the final state's reads;
expected files fetched at import stay on the host. Scores follow OSWorld's own
evaluate: an infeasible task passes only when the agent gives up, giving up on
a feasible task fails and metrics combine with and (every metric must pass,
stopping at the first that fails) or or (any metric passes, stopping at the
first that does). The metrics are OSWorld's, rewritten over bytes with the same
semantics; the importer refuses tasks whose metrics or getters are not here.
"""

import base64
import hashlib
import json
import re
from collections.abc import Callable, Mapping
from typing import Any, Literal, Protocol

from pydantic import BaseModel, ConfigDict, Field

from rlworld.guest.protocol import TASK_READS_KEY
from rlworld.rewards.checks import check_result
from rlworld.rewards.records import CheckResult
from rlworld.rewards.verifiers import VerifierContext

GIVE_UP_ACTION = "give_up"
SUMMARY_CHARACTERS = 300


class EvaluationModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class Getter(EvaluationModel):
    """One metric input: guest files or a command's output, a literal rule or files fetched at import.

    A getter that names several files gives its metric a list, as OSWorld's multi getters do.
    """

    kind: Literal["vm_file", "vm_command_line", "vm_command_error", "rule", "cloud_file"]
    paths: tuple[str, ...] = ()
    command: str = ""
    rules: Any = None
    assets: tuple[str, ...] = ()


class MetricCall(EvaluationModel):
    func: str
    result: Getter
    expected: Getter
    options: dict[str, Any] = Field(default_factory=dict)


class Evaluation(EvaluationModel):
    infeasible: bool = False
    conj: Literal["and", "or"] = "and"
    metrics: tuple[MetricCall, ...] = ()


class AssetReader(Protocol):
    def read(self, digest: str) -> bytes: ...


def file_bytes(entry: Mapping[str, Any] | None) -> bytes | None:
    """A file the guest read, or None when it was missing, as OSWorld's getters give None."""
    if not entry or "base64" not in entry:
        return None
    return base64.b64decode(entry["base64"])


def getter_value(getter: Getter, reads: Mapping[str, Any], assets: AssetReader) -> Any:
    if getter.kind == "rule":
        return getter.rules
    if getter.kind in ("vm_command_line", "vm_command_error"):
        output = (reads.get("commands") or {}).get(getter.command)
        if output is None:
            return None
        return output["stdout"] if getter.kind == "vm_command_line" else output["stderr"]
    if getter.kind == "vm_file":
        files = reads.get("files") or {}
        values = [file_bytes(files.get(path)) for path in getter.paths]
    else:
        values = [assets.read(digest) for digest in getter.assets]
    return values[0] if len(values) == 1 else values


def exact_match(result: Any, rules: Any, options: Mapping[str, Any]) -> float:
    return 1.0 if result == rules["expected"] else 0.0


def match_in_list(result: Any, rules: Any, options: Mapping[str, Any]) -> float:
    return 1.0 if result in rules["expected"] else 0.0


def is_in_list(result: Any, rules: Any, options: Mapping[str, Any]) -> float:
    if result is None:
        return 0.0
    return 1.0 if rules["expected"] in result else 0.0


def check_include_exclude(result: Any, rules: Any, options: Mapping[str, Any]) -> float:
    if result is None:
        return 0.0
    included = all(item in result for item in rules.get("include", []))
    excluded = all(item not in result for item in rules.get("exclude", []))
    return 1.0 if included and excluded else 0.0


def literal_text(value: Any, ignore_case: bool) -> str:
    return str(value).lower() if ignore_case else str(value)


def literal_match(result: Any, expected: Any, options: Mapping[str, Any]) -> float:
    literal_type = options.get("type", "str")
    ignore_case = options.get("ignore_case", False)
    if literal_type == "str":
        return 1.0 if literal_text(result, ignore_case) == literal_text(expected, ignore_case) else 0.0
    if literal_type != "list":
        raise ValueError(f"literal_match has no type {literal_type!r}")
    if not isinstance(result, (list, tuple)) or not isinstance(expected, (list, tuple)):
        return 0.0
    result_items = [literal_text(item, ignore_case) for item in result]
    expected_items = [literal_text(item, ignore_case) for item in expected]
    return 1.0 if result_items == expected_items else 0.0


def collapsed_blanks(text: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"[\t\n]", " ", text).strip())


def compare_text_file(actual: bytes | None, expected: bytes, options: Mapping[str, Any]) -> float:
    if actual is None:
        return 0.0
    actual_text = actual.decode("utf-8", errors="replace")
    expected_text = expected.decode("utf-8", errors="replace")
    if options.get("ignore_blanks", False):
        actual_text, expected_text = collapsed_blanks(actual_text), collapsed_blanks(expected_text)
    if options.get("ignore_case", False):
        actual_text, expected_text = actual_text.lower(), expected_text.lower()
    return 1.0 if actual_text == expected_text else 0.0


def check_json_settings(actual: bytes | None, rules: Any, options: Mapping[str, Any]) -> float:
    """Every expected setting is present with its value, as in a VS Code settings.json."""
    if actual is None:
        return 0.0
    try:
        settings = json.loads(actual)
    except ValueError:
        return 0.0
    if not isinstance(settings, dict):
        return 0.0
    expected = rules["expected"]
    return 1.0 if all(key in settings and settings[key] == value for key, value in expected.items()) else 0.0


def keybinding_list(content: bytes) -> list[Any] | None:
    """VS Code may write a comment line above the JSON list, so OSWorld retries without the first line."""
    text = content.decode("utf-8", errors="replace")
    for candidate in (text, text.partition("\n")[2]):
        try:
            parsed = json.loads(candidate)
        except ValueError:
            continue
        if isinstance(parsed, list):
            return parsed
    return None


def check_json_keybindings(actual: bytes | None, rules: Any, options: Mapping[str, Any]) -> float:
    if actual is None:
        return 0.0
    keybindings = keybinding_list(actual)
    if keybindings is None:
        return 0.0
    return 1.0 if rules["expected"] in keybindings else 0.0


Metric = Callable[[Any, Any, Mapping[str, Any]], float]
METRICS: dict[str, Metric] = {
    "exact_match": exact_match,
    "match_in_list": match_in_list,
    "is_in_list": is_in_list,
    "check_include_exclude": check_include_exclude,
    "literal_match": literal_match,
    "compare_text_file": compare_text_file,
    "check_json_settings": check_json_settings,
    "check_json_keybindings": check_json_keybindings,
}


def summary(value: Any) -> Any:
    """Small enough for a reward record: file bytes by size and digest, long text cut short."""
    if isinstance(value, bytes):
        return f"{len(value)} bytes, sha256 {hashlib.sha256(value).hexdigest()[:16]}"
    if isinstance(value, str) and len(value) > SUMMARY_CHARACTERS:
        return f"{value[:SUMMARY_CHARACTERS]}..."
    if isinstance(value, list):
        return [summary(item) for item in value]
    return value


def metric_check(call: MetricCall, reads: Mapping[str, Any], assets: AssetReader) -> CheckResult:
    result = getter_value(call.result, reads, assets)
    expected = getter_value(call.expected, reads, assets)
    score = METRICS[call.func](result, expected, call.options)
    return check_result(f"osworld_{call.func}", "exact", score >= 1.0, f"{call.func} scored {score:g}",
                        summary(expected), summary(result))


def evaluation_checks(evaluation: Evaluation, context: VerifierContext, assets: AssetReader) -> list[CheckResult]:
    gave_up = context.final_action == GIVE_UP_ACTION
    if evaluation.infeasible:
        return [check_result("osworld_infeasible", "exact", gave_up,
                             "the task is infeasible, so only giving up passes")]
    if gave_up:
        return [check_result("osworld_feasible", "exact", False, "the agent gave up on a feasible task")]
    reads = context.final_state.get(TASK_READS_KEY) or {}
    checks = []
    for call in evaluation.metrics:
        check = metric_check(call, reads, assets)
        if evaluation.conj == "or" and check.passed:
            return [check]
        checks.append(check)
        if evaluation.conj == "and" and not check.passed:
            break
    if evaluation.conj == "or":
        failed = ", ".join(check.name for check in checks)
        return [check_result("osworld_any_metric", "exact", False, f"no metric passed: {failed}")]
    return checks
