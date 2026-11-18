import json
from decimal import Decimal
from pathlib import Path

import pytest

from evals.harness import Budget, BudgetExceededError
from evals.score import (
    FlagCounts,
    percentile,
    results_document,
    score_fields,
    values_match,
    without_unverifiable,
)


@pytest.mark.parametrize(
    ("name", "expected", "actual", "match"),
    [
        ("amount", "2500.00", "2500", True),  # same number
        ("amount", "2500.00", "2,500.00", False),  # not normalised: wrong
        ("amount", "2500.00", "2500.01", False),
        ("supplier_name", "Acme Tools GmbH", "ACME  tools gmbh", True),  # case and spacing
        ("supplier_name", "Acme Tools GmbH", "Acme Tools", False),
        ("hs_code", "82071910", "8207.19.10", True),  # digits compared
        ("invoice_date", "2026-10-14", "2026-10-14", True),
        ("invoice_date", "2026-10-14", "2026-14-10", False),
        ("currency", "EUR", "EUR", True),
        ("buyer_name", None, None, True),  # correctly absent
        ("buyer_name", None, "", True),
        ("buyer_name", None, "Someone", False),  # invented
        ("buyer_name", "Gulf Drilling", None, False),  # missed
    ],
)
def test_values_match(name: str, expected: str | None, actual: str | None, match: bool) -> None:
    assert values_match(name, expected, actual) is match


def field(value: str | None) -> dict[str, str | None]:
    return {"value": value, "source_quote": value}


def test_missing_line_items_count_as_wrong_and_unscored_fields_are_skipped() -> None:
    expected = {
        "total": field("10.00"),
        "line_items": [
            {"amount": field("4.00"), "hs_code": field("82071910")},
            {"amount": field("6.00"), "hs_code": field("82071911")},
        ],
    }
    actual = {
        "total": field("10"),
        "line_items": [{"amount": field("4.00"), "hs_code": field("1")}],
    }

    results = {r.path: r.correct for r in score_fields(expected, actual, ["line_items[0].hs_code"])}

    assert results == {
        "total": True,
        "line_items[0].amount": True,
        "line_items[1].amount": False,  # the model returned only one line
        "line_items[1].hs_code": False,
    }


def test_flag_counts() -> None:
    counts = FlagCounts()
    counts.add(expected={("total", "x"), ("a", "y")}, actual={("total", "x"), ("b", "z")})
    counts.add(expected=set(), actual=set())

    assert (counts.tp, counts.fp, counts.fn) == (1, 1, 1)
    assert counts.precision == 0.5
    assert counts.recall == 0.5


def test_no_flags_expected_or_raised_has_undefined_rates() -> None:
    assert FlagCounts().precision is None
    assert FlagCounts().recall is None


def test_grounding_unverifiable_is_not_a_planted_problem() -> None:
    flags = {("document", "grounding_unverifiable"), ("total", "arithmetic_mismatch")}

    assert without_unverifiable(flags) == {("total", "arithmetic_mismatch")}


def test_percentile() -> None:
    assert percentile([100, 200, 300, 400, 1000], 0.5) == 300
    assert percentile([100, 200, 300, 400, 1000], 0.95) == 1000
    assert percentile([], 0.5) == 0


def test_budget_blocks_a_call_that_could_exceed_the_cap() -> None:
    budget = Budget(Decimal("0.10"))
    budget.reserve(Decimal("0.06"))
    with pytest.raises(BudgetExceededError):
        budget.reserve(Decimal("0.06"))  # 0.12 reserved in the worst case

    budget.settle(Decimal("0.06"), Decimal("0.01"))  # the call was cheaper than reserved
    budget.reserve(Decimal("0.06"))  # now fits: 0.01 spent + 0.06 reserved
    assert budget.spent == Decimal("0.01")


RUNS = sorted(
    p
    for p in (Path(__file__).resolve().parent.parent / "evals" / "runs").glob("*")
    if (p / "results.json").exists()
)


@pytest.mark.parametrize("run_dir", RUNS, ids=[p.name for p in RUNS])
def test_committed_results_are_reproducible_from_the_saved_outputs(run_dir: Path) -> None:
    """Re-scores the committed raw model outputs (no API calls) and requires the committed
    results.json to come out identical, so the published numbers can always be re-derived."""
    committed = json.loads((run_dir / "results.json").read_text(encoding="utf-8"))

    assert results_document(run_dir) == committed
