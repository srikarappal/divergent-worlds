"""Tier 1 exact and tier 2 semantic check helpers (DESIGN.md section 6.9).

Each helper returns one CheckResult whose detail explains a failure in a line,
so a reviewer can see why an episode scored what it did.
"""

import difflib
import math
import re
import unicodedata
from collections.abc import Iterable, Mapping, Sequence
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from rlworld.rewards.records import CheckResult, CheckTier

CURRENCY_SYMBOLS = "$€£¥₹"
DEFAULT_PORTS = {"http": 80, "https": 443}
MAX_EXPANDED_CELLS = 10_000
CELL_REFERENCE_PATTERN = re.compile(
    r"(?<![\w.$])(?:(?P<sheet>'[^']+'|[A-Za-z_][\w.]*)!)?"
    r"\$?(?P<start_column>[A-Z]{1,3})\$?(?P<start_row>\d+)"
    r"(?::\$?(?P<end_column>[A-Z]{1,3})\$?(?P<end_row>\d+))?(?![\w(])"
)
STRING_LITERAL_PATTERN = re.compile(r'"[^"]*"')


def check_result(
    name: str,
    tier: CheckTier,
    passed: bool,
    failure_detail: str,
    expected: Any = None,
    actual: Any = None,
    weight: float = 1.0,
) -> CheckResult:
    return CheckResult(
        name=name,
        tier=tier,
        passed=passed,
        weight=weight,
        detail="" if passed else failure_detail,
        expected=expected,
        actual=actual,
    )


def parse_number(raw_value: Any) -> float:
    """Numbers from cells and text: "$1,234.50", "(12)" for negatives, "15%" as 0.15."""
    if isinstance(raw_value, bool) or raw_value is None:
        raise ValueError(f"not a number: {raw_value!r}")
    if isinstance(raw_value, (int, float)):
        return float(raw_value)
    if not isinstance(raw_value, str):
        raise ValueError(f"not a number: {raw_value!r}")
    text = raw_value.strip().replace(",", "").replace(" ", "")
    for currency_symbol in CURRENCY_SYMBOLS:
        text = text.replace(currency_symbol, "")
    is_negative = text.startswith("(") and text.endswith(")")
    if is_negative:
        text = text[1:-1]
    is_percent = text.endswith("%")
    if is_percent:
        text = text[:-1]
    number = float(text)
    if is_percent:
        number /= 100
    return -number if is_negative else number


def values_match(actual: Any, expected: Any, numeric_tolerance: float = 0.0) -> bool:
    if isinstance(expected, (int, float)) and not isinstance(expected, bool):
        try:
            return math.isclose(parse_number(actual), float(expected), rel_tol=0.0, abs_tol=numeric_tolerance)
        except ValueError:
            return False
    return actual == expected


def normalize_text(text: str) -> str:
    folded_text = unicodedata.normalize("NFKC", text).casefold()
    spaced_text = re.sub(r"[^\w\s]", " ", folded_text)
    return " ".join(spaced_text.split())


def normalize_url(url: str) -> str:
    parts = urlsplit(url.strip())
    scheme = parts.scheme.lower()
    hostname = (parts.hostname or "").lower()
    port = parts.port
    netloc = hostname if port in (None, DEFAULT_PORTS.get(scheme)) else f"{hostname}:{port}"
    path = parts.path.rstrip("/") or "/"
    query = urlencode(sorted(parse_qsl(parts.query, keep_blank_values=True)))
    return urlunsplit((scheme, netloc, path, query, ""))


def exact_equal(name: str, actual: Any, expected: Any, weight: float = 1.0) -> CheckResult:
    return check_result(
        name, "exact", actual == expected, f"expected {expected!r}, got {actual!r}", expected, actual, weight
    )


def number_close(
    name: str,
    actual: Any,
    expected: Any,
    relative_tolerance: float = 1e-9,
    absolute_tolerance: float = 1e-9,
    weight: float = 1.0,
) -> CheckResult:
    try:
        actual_number = parse_number(actual)
    except ValueError:
        return check_result(name, "semantic", False, f"{actual!r} is not a number", expected, actual, weight)
    expected_number = parse_number(expected)
    passed = math.isclose(actual_number, expected_number, rel_tol=relative_tolerance, abs_tol=absolute_tolerance)
    return check_result(
        name, "semantic", passed, f"expected about {expected_number}, got {actual_number}", expected, actual, weight
    )


def text_equivalent(name: str, actual: str, expected: str, weight: float = 1.0) -> CheckResult:
    passed = normalize_text(actual or "") == normalize_text(expected)
    return check_result(
        name, "semantic", passed, f"expected text like {expected!r}, got {actual!r}", expected, actual, weight
    )


def answer_matches(
    name: str, answer_text: str | None, expected: Any, numeric_tolerance: float = 1e-9, weight: float = 1.0
) -> CheckResult:
    """Number answers compare by value, everything else by normalized text."""
    if isinstance(expected, (int, float)) and not isinstance(expected, bool):
        return number_close(name, answer_text, expected, relative_tolerance=0.0,
                            absolute_tolerance=numeric_tolerance, weight=weight)
    return text_equivalent(name, answer_text or "", str(expected), weight)


def entity_found(normalized_entity: str, normalized_text: str, text_tokens: list[str], fuzzy_threshold: float) -> bool:
    """Exact word match first; fuzzy only for entities without digits, so INV 1001 never matches INV 1007."""
    if not normalized_entity:
        return True
    if f" {normalized_entity} " in f" {normalized_text} ":
        return True
    if any(character.isdigit() for character in normalized_entity):
        return False
    window_size = len(normalized_entity.split())
    for start in range(len(text_tokens) - window_size + 1):
        window_text = " ".join(text_tokens[start : start + window_size])
        if difflib.SequenceMatcher(None, normalized_entity, window_text).ratio() >= fuzzy_threshold:
            return True
    return False


def entities_mentioned(
    name: str,
    text: str,
    entities: Sequence[str],
    minimum_fraction: float = 1.0,
    fuzzy_threshold: float = 0.85,
    weight: float = 1.0,
) -> CheckResult:
    normalized_text = normalize_text(text or "")
    text_tokens = normalized_text.split()
    missing_entities = [
        entity
        for entity in entities
        if not entity_found(normalize_text(entity), normalized_text, text_tokens, fuzzy_threshold)
    ]
    found_fraction = 1.0 - len(missing_entities) / len(entities) if entities else 1.0
    return check_result(
        name,
        "semantic",
        found_fraction >= minimum_fraction,
        f"missing {missing_entities}",
        list(entities),
        missing_entities,
        weight,
    )


def url_equivalent(name: str, actual: str, expected: str, weight: float = 1.0) -> CheckResult:
    passed = normalize_url(actual or "") == normalize_url(expected)
    return check_result(name, "semantic", passed, f"expected {expected!r}, got {actual!r}", expected, actual, weight)


def file_present(name: str, files: Mapping[str, Any], path: str, weight: float = 1.0) -> CheckResult:
    return check_result(name, "exact", path in files, f"{path} does not exist", path, sorted(files), weight)


def cells_equal(
    name: str,
    cells: Mapping[str, Mapping[str, Any]],
    expected_values: Mapping[str, Any],
    numeric_tolerance: float = 0.0,
    weight: float = 1.0,
) -> CheckResult:
    """Cell values as the app computed them; exact unless a numeric tolerance is given."""
    mismatches = []
    for cell_name, expected_value in expected_values.items():
        actual_value = (cells.get(cell_name) or {}).get("value")
        if not values_match(actual_value, expected_value, numeric_tolerance):
            mismatches.append(f"{cell_name}: expected {expected_value!r}, got {actual_value!r}")
    tier: CheckTier = "exact" if numeric_tolerance == 0 else "semantic"
    return check_result(name, tier, not mismatches, "; ".join(mismatches), dict(expected_values), mismatches, weight)


def column_index(column_letters_text: str) -> int:
    index = 0
    for letter in column_letters_text:
        index = index * 26 + (ord(letter) - ord("A") + 1)
    return index


def column_letters(column_number: int) -> str:
    letters = ""
    while column_number:
        column_number, remainder = divmod(column_number - 1, 26)
        letters = chr(ord("A") + remainder) + letters
    return letters


def formula_references(formula: str, default_sheet: str = "") -> frozenset[str]:
    """Cells a formula reads with ranges expanded, as "Sheet!A1", or "A1" when no sheet is named."""
    referenced_cells: set[str] = set()
    for match in CELL_REFERENCE_PATTERN.finditer(STRING_LITERAL_PATTERN.sub('""', formula)):
        sheet_name = (match.group("sheet") or default_sheet).strip("'")
        first_column = column_index(match.group("start_column"))
        last_column = column_index(match.group("end_column") or match.group("start_column"))
        first_row = int(match.group("start_row"))
        last_row = int(match.group("end_row") or match.group("start_row"))
        low_column, high_column = sorted((first_column, last_column))
        low_row, high_row = sorted((first_row, last_row))
        cell_count = (high_column - low_column + 1) * (high_row - low_row + 1)
        if len(referenced_cells) + cell_count > MAX_EXPANDED_CELLS:
            raise ValueError(f"formula {formula!r} references more than {MAX_EXPANDED_CELLS} cells")
        for column_number in range(low_column, high_column + 1):
            for row_number in range(low_row, high_row + 1):
                cell_name = f"{column_letters(column_number)}{row_number}"
                referenced_cells.add(f"{sheet_name}!{cell_name}" if sheet_name else cell_name)
    return frozenset(referenced_cells)


def formula_equivalent(
    name: str,
    cell: Mapping[str, Any] | None,
    expected_value: Any,
    expected_references: Iterable[str],
    numeric_tolerance: float = 1e-9,
    weight: float = 1.0,
) -> CheckResult:
    """Any formula that yields the expected value from exactly the expected cells passes."""
    formula = (cell or {}).get("formula")
    if not formula:
        return check_result(name, "semantic", False, "cell holds no formula, the value was typed in",
                            expected_value, cell, weight)
    actual_value = cell.get("value")
    if not values_match(actual_value, expected_value, numeric_tolerance):
        return check_result(name, "semantic", False, f"formula yields {actual_value!r}, expected {expected_value!r}",
                            expected_value, actual_value, weight)
    actual_references = formula_references(formula)
    wanted_references = formula_references(",".join(expected_references))
    passed = actual_references == wanted_references
    failure_detail = (
        f"formula {formula} reads extra {sorted(actual_references - wanted_references)} "
        f"and misses {sorted(wanted_references - actual_references)}"
    )
    return check_result(name, "semantic", passed, failure_detail, sorted(wanted_references), formula, weight)


def rows_match(
    name: str,
    actual_rows: Sequence[Mapping[str, Any]],
    expected_rows: Sequence[Mapping[str, Any]],
    key_field: str,
    compared_fields: Sequence[str] | None = None,
    weight: float = 1.0,
) -> CheckResult:
    actual_rows_by_key = {row.get(key_field): row for row in actual_rows}
    problems = []
    for expected_row in expected_rows:
        row_key = expected_row[key_field]
        actual_row = actual_rows_by_key.get(row_key)
        if actual_row is None:
            problems.append(f"row {row_key!r} is missing")
            continue
        fields = compared_fields or [field_name for field_name in expected_row if field_name != key_field]
        for field_name in fields:
            if actual_row.get(field_name) != expected_row.get(field_name):
                problems.append(
                    f"row {row_key!r} {field_name}: expected {expected_row.get(field_name)!r}, "
                    f"got {actual_row.get(field_name)!r}"
                )
    return check_result(name, "exact", not problems, "; ".join(problems), list(expected_rows), problems, weight)
