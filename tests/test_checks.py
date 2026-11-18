from datetime import date

import pytest

from docflow.checks import Flag, FlagCode, run_checks
from docflow.invoice import InvoiceExtraction
from tests.invoices import SAMPLE_TEXT, field, line, make_invoice

TODAY = date(2026, 11, 17)


def check(invoice: InvoiceExtraction, text: str = SAMPLE_TEXT) -> list[Flag]:
    return run_checks(invoice, text, TODAY)


def codes(flags: list[Flag]) -> set[tuple[str, FlagCode]]:
    return {(f.field, f.code) for f in flags}


def test_consistent_grounded_invoice_has_no_flags() -> None:
    assert check(make_invoice()) == []


# --- completeness ------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "name", ["invoice_number", "invoice_date", "supplier_name", "buyer_name", "currency", "total"]
)
def test_missing_required_field_is_flagged(name: str) -> None:
    flags = check(make_invoice(**{name: field(None)}))

    assert (name, FlagCode.MISSING_REQUIRED) in codes(flags)


def test_invoice_without_line_items_is_flagged() -> None:
    flags = check(make_invoice(line_items=[]))

    assert ("line_items", FlagCode.MISSING_REQUIRED) in codes(flags)


def test_optional_fields_may_be_missing() -> None:
    assert check(make_invoice(supplier_country=field(None), buyer_country=field(None))) == []


# --- grounding ---------------------------------------------------------------------------------


def test_quote_not_in_document_is_flagged() -> None:
    flags = check(make_invoice(buyer_name=field("Invented Buyer Ltd")))

    assert codes(flags) == {("buyer_name", FlagCode.NOT_GROUNDED)}


def test_value_without_a_quote_is_flagged() -> None:
    flags = check(make_invoice(supplier_name=field("Acme Tools GmbH", quote=None)))

    assert codes(flags) == {("supplier_name", FlagCode.NOT_GROUNDED)}


def test_grounding_ignores_case_and_whitespace() -> None:
    invoice = make_invoice(supplier_name=field("Acme Tools GmbH", "ACME   tools\ngmbh"))

    assert check(invoice) == []


def test_scanned_pdf_gets_one_document_level_flag_instead_of_per_field_flags() -> None:
    flags = check(make_invoice(), text="   \n ")

    assert codes(flags) == {("document", FlagCode.GROUNDING_UNVERIFIABLE)}


# --- formats -----------------------------------------------------------------------------------


@pytest.mark.parametrize("code", ["EUR", "eur", "INR", "USD"])
def test_valid_currencies_pass(code: str) -> None:
    flags = check(make_invoice(currency=field(code, "EUR")))

    assert ("currency", FlagCode.INVALID_FORMAT) not in codes(flags)


@pytest.mark.parametrize("code", ["EURO", "XXX", "XAU", "€"])
def test_invalid_currencies_are_flagged(code: str) -> None:
    flags = check(make_invoice(currency=field(code, "EUR")))

    assert ("currency", FlagCode.INVALID_FORMAT) in codes(flags)


@pytest.mark.parametrize(
    ("value", "valid"),
    [("2026-10-15", True), ("2026-11-17", True), ("2026-11-18", False), ("15.10.2026", False)],
)
def test_invoice_date_must_be_iso_and_not_in_the_future(value: str, valid: bool) -> None:
    flags = check(make_invoice(invoice_date=field(value, "15.10.2026")))

    assert (("invoice_date", FlagCode.INVALID_FORMAT) not in codes(flags)) == valid


@pytest.mark.parametrize(
    ("hs_code", "valid"),
    [
        ("820719", True),
        ("8207191000", True),
        ("8207.19.10", True),
        ("82071", False),
        ("ABC12345", False),
    ],
)
def test_hs_code_must_be_6_to_10_digits(hs_code: str, valid: bool) -> None:
    invoice = make_invoice(
        line_items=[
            line(
                hs_code=hs_code,
                quotes={"hs_code": "8207.19.10", "unit_price": "1,250.00", "amount": "2,500.00"},
            )
        ],
        subtotal=field("2500.00", "2,500.00"),
        total=field("2500.00", "2,500.00"),
    )

    flags = check(invoice)

    assert (("line_items[0].hs_code", FlagCode.INVALID_FORMAT) not in codes(flags)) == valid


def test_unparseable_number_is_flagged_once_without_arithmetic_noise() -> None:
    invoice = make_invoice(total=field("3,742.00"))  # model failed to normalise

    flags = check(invoice)

    assert codes(flags) == {("total", FlagCode.UNPARSEABLE_NUMBER)}


# --- arithmetic --------------------------------------------------------------------------------


def test_line_amount_not_matching_quantity_times_price_is_flagged() -> None:
    wrong = line(
        amount="2600.00",
        quotes={"hs_code": "8207.19.10", "unit_price": "1,250.00", "amount": "2,500.00"},
    )
    invoice = make_invoice(
        line_items=[wrong],
        subtotal=field("2600.00", "3,742.00"),
        total=field("2600.00", "3,742.00"),
    )

    flags = check(invoice)

    assert codes(flags) == {("line_items[0].amount", FlagCode.ARITHMETIC_MISMATCH)}
    assert "2 x 1250.00 = 2500.00" in flags[0].message


def test_rounding_within_one_cent_is_accepted() -> None:
    item = line(quantity="3", unit_price="33.333", amount="100.00")
    invoice = make_invoice(line_items=[item], subtotal=field("100.00"), total=field("100.00"))

    flags = check(invoice, text="x" * 50)

    assert not [f for f in flags if f.code == FlagCode.ARITHMETIC_MISMATCH]


def test_lines_not_adding_up_to_subtotal_is_flagged() -> None:
    flags = check(
        make_invoice(subtotal=field("3800.00", "3,742.00"), total=field("3800.00", "3,742.00"))
    )

    assert codes(flags) == {("subtotal", FlagCode.ARITHMETIC_MISMATCH)}


def test_subtotal_plus_tax_not_matching_total_is_flagged() -> None:
    flags = check(make_invoice(tax_total=field("100.00", "0.00")))

    assert codes(flags) == {("total", FlagCode.ARITHMETIC_MISMATCH)}


def test_tax_is_included_in_the_total() -> None:
    invoice = make_invoice(tax_total=field("100.00", "0.00"), total=field("3842.00", "3,742.00"))

    flags = check(invoice)

    assert ("total", FlagCode.ARITHMETIC_MISMATCH) not in codes(flags)


def test_missing_tax_counts_as_zero() -> None:
    assert check(make_invoice(tax_total=field(None))) == []


@pytest.mark.parametrize(
    ("value", "printed", "valid"),
    [
        ("9026", "90-26-B", False),  # seen live: letter dropped during normalisation
        ("82071910", "8207.19.1O", False),  # letter O instead of zero looks valid once normalised
        ("82071910", "HS Code: 8207.19.10", True),  # label words in the quote are fine
        ("82071910", "8207 19 10", True),
    ],
)
def test_hs_code_check_also_inspects_the_printed_code(
    value: str, printed: str, valid: bool
) -> None:
    item = line(hs_code=value, quotes={"hs_code": printed})
    invoice = make_invoice(line_items=[item], subtotal=field("2500.00"), total=field("2500.00"))

    flags = check(invoice, text="x" * 50)  # grounding is not what this test is about

    assert (("line_items[0].hs_code", FlagCode.INVALID_FORMAT) not in codes(flags)) == valid
