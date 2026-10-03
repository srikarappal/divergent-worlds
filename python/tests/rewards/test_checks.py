import pytest

from rlworld.rewards.checks import (
    answer_matches,
    cells_equal,
    column_index,
    column_letters,
    entities_mentioned,
    exact_equal,
    file_present,
    formula_equivalent,
    formula_references,
    normalize_text,
    normalize_url,
    number_close,
    parse_number,
    rows_match,
    text_equivalent,
    url_equivalent,
    values_match,
)


@pytest.mark.parametrize(
    ("raw_value", "number"),
    [("$1,234.50", 1234.5), ("(12)", -12.0), ("15%", 0.15), (" 3 ", 3.0), (7, 7.0), ("€ 2.5", 2.5), (1.25, 1.25)],
)
def test_parse_number(raw_value, number):
    assert parse_number(raw_value) == pytest.approx(number)


@pytest.mark.parametrize("raw_value", [None, True, "abc", [1], "12 apples"])
def test_parse_number_rejects_non_numbers(raw_value):
    with pytest.raises(ValueError):
        parse_number(raw_value)


def test_values_match():
    assert values_match(42.0, 42)
    assert values_match("42", 42)
    assert not values_match(None, 42)
    assert values_match(41.99, 42, numeric_tolerance=0.02)
    assert values_match("open", "open")
    assert not values_match("Open", "open")


def test_exact_equal_explains_failures():
    assert exact_equal("stage", "won", "won").passed
    failed = exact_equal("stage", "lost", "won")
    assert not failed.passed
    assert failed.tier == "exact"
    assert failed.detail == "expected 'won', got 'lost'"


def test_number_close():
    assert number_close("total", "$1,250.00", 1250).passed
    assert not number_close("total", "1,251", 1250).passed
    assert number_close("total", 1250.4, 1250, absolute_tolerance=0.5).passed
    assert "not a number" in number_close("total", "n/a", 1250).detail


def test_text_equivalent_ignores_case_spacing_and_punctuation():
    assert normalize_text("  Hello,   WORLD! ") == "hello world"
    assert text_equivalent("greeting", "hello world.", "Hello, World").passed
    assert not text_equivalent("greeting", "hello there", "hello world").passed


def test_answer_matches_numbers_and_text():
    assert answer_matches("answer", "1,250", 1250).passed
    assert not answer_matches("answer", "about 1250", 1250).passed
    assert answer_matches("answer", "Acme Holdings.", "acme holdings").passed
    assert not answer_matches("answer", None, "acme").passed


def test_entities_mentioned_exact_fuzzy_and_fraction():
    body = "Flagged INV-1001 and inv 1007 for Acme Holdngs."
    assert entities_mentioned("ids", body, ["INV-1001", "INV-1007"]).passed
    missing = entities_mentioned("ids", body, ["INV-1001", "INV-1002"])
    assert not missing.passed
    assert missing.actual == ["INV-1002"]
    assert entities_mentioned("customer", body, ["Acme Holdings"]).passed
    assert entities_mentioned("ids", body, ["INV-1001", "INV-1002"], minimum_fraction=0.5).passed


def test_ids_with_digits_never_match_fuzzily():
    assert not entities_mentioned("ids", "please review INV-1007", ["INV-1001"]).passed


def test_url_equivalent():
    assert normalize_url("HTTPS://CRM.example.com:443/deals/?b=2&a=1#top") == "https://crm.example.com/deals?a=1&b=2"
    assert url_equivalent("url", "https://crm.example.com/deals/?b=2&a=1",
                          "https://crm.example.com/deals?a=1&b=2").passed
    assert not url_equivalent("url", "https://crm.example.com/leads", "https://crm.example.com/deals").passed
    assert normalize_url("http://crm.example.com:8080") == "http://crm.example.com:8080/"


def test_file_present():
    files = {"reconciliation.xlsx": {}, "invoices.csv": {}}
    assert file_present("saved", files, "reconciliation.xlsx").passed
    assert "does not exist" in file_present("saved", files, "summary.pdf").detail


def test_cells_equal_lists_every_mismatch():
    cells = {"B2": {"value": 1250.0}, "B3": {"value": "ok"}}
    assert cells_equal("summary", cells, {"B2": 1250, "B3": "ok"}).passed
    failed = cells_equal("summary", cells, {"B2": 1200, "B4": 5})
    assert failed.detail == "B2: expected 1200, got 1250.0; B4: expected 5, got None"
    assert failed.tier == "exact"
    assert cells_equal("summary", cells, {"B2": 1250.4}, numeric_tolerance=0.5).tier == "semantic"


def test_column_letters_round_trip():
    for column_number, letters in [(1, "A"), (26, "Z"), (27, "AA"), (702, "ZZ"), (703, "AAA")]:
        assert column_letters(column_number) == letters
        assert column_index(letters) == column_number


@pytest.mark.parametrize(
    ("formula", "cells"),
    [
        ("=SUM(A1:A3)", {"A1", "A2", "A3"}),
        ("=A1+A2+A3", {"A1", "A2", "A3"}),
        ("=SUM($B$2:$C$3)", {"B2", "B3", "C2", "C3"}),
        ("=SUM(A3:A1)", {"A1", "A2", "A3"}),
        ("=Invoices!C2+'Q3 Data'!D4", {"Invoices!C2", "Q3 Data!D4"}),
        ("=LOG10(A1)", {"A1"}),
        ('=IF(A1="B2",1,0)', {"A1"}),
    ],
)
def test_formula_references(formula, cells):
    assert formula_references(formula) == frozenset(cells)


def test_huge_ranges_are_refused():
    with pytest.raises(ValueError):
        formula_references("=SUM(A1:A20000)")


def test_formula_equivalent():
    expected_references = ["A1:A3"]
    assert formula_equivalent("total", {"value": 6, "formula": "=A1+A2+A3"}, 6, expected_references).passed
    assert formula_equivalent("total", {"value": 6, "formula": "=SUM(A1:A3)"}, 6, expected_references).passed
    typed_in = formula_equivalent("total", {"value": 6, "formula": None}, 6, expected_references)
    assert "typed in" in typed_in.detail
    wrong_cells = formula_equivalent("total", {"value": 6, "formula": "=A1+A2+A4"}, 6, expected_references)
    assert "extra ['A4']" in wrong_cells.detail and "misses ['A3']" in wrong_cells.detail
    assert not formula_equivalent("total", {"value": 7, "formula": "=SUM(A1:A3)"}, 6, expected_references).passed


def test_rows_match():
    actual_rows = [{"id": 1, "stage": "won", "amount": 10}, {"id": 2, "stage": "open", "amount": 5}]
    assert rows_match("deals", actual_rows, [{"id": 1, "stage": "won"}], key_field="id").passed
    failed = rows_match("deals", actual_rows, [{"id": 2, "stage": "won"}, {"id": 3, "stage": "open"}], key_field="id")
    assert failed.detail == "row 2 stage: expected 'won', got 'open'; row 3 is missing"
