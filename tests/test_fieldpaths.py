import pytest

from docflow.fieldpaths import FieldPath, InvalidFieldPathError, get_value, parse, with_value
from tests.invoices import make_invoice

FIELDS = make_invoice().model_dump(mode="json")  # two line items


@pytest.mark.parametrize(
    ("path", "expected"),
    [
        ("total", FieldPath("total")),
        ("invoice_number", FieldPath("invoice_number")),
        ("line_items[0].amount", FieldPath("amount", 0)),
        ("line_items[1].hs_code", FieldPath("hs_code", 1)),
    ],
)
def test_valid_paths_round_trip(path: str, expected: FieldPath) -> None:
    parsed = parse(path, FIELDS)

    assert parsed == expected
    assert str(parsed) == path


@pytest.mark.parametrize(
    "path",
    [
        "line_items",  # the list itself, not a field
        "nonexistent",
        "line_items[2].amount",  # only two line items
        "line_items[0].nonexistent",
        "line_items[-1].amount",
        "line_items[0]",
        "total.value",
        "",
    ],
)
def test_invalid_paths_are_rejected(path: str) -> None:
    with pytest.raises(InvalidFieldPathError):
        parse(path, FIELDS)


def test_with_value_returns_a_copy_and_clears_the_quote() -> None:
    path = parse("line_items[1].amount", FIELDS)

    updated = with_value(FIELDS, path, "999.00")

    assert get_value(updated, path) == "999.00"
    assert updated["line_items"][1]["amount"]["source_quote"] is None
    assert get_value(FIELDS, path) == "1242.00"  # original untouched
