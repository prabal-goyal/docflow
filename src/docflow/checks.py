"""Deterministic checks on an extraction. These, not the model's own confidence, decide what a
reviewer must look at. Pure functions: no I/O, no clock except the `today` argument."""

import re
from collections.abc import Iterator
from dataclasses import asdict, dataclass
from datetime import date
from decimal import Decimal, InvalidOperation
from enum import StrEnum

from docflow.currencies import INVOICE_CURRENCIES
from docflow.invoice import ExtractedField, InvoiceExtraction

# Rounding on printed invoices: each line and each total may be off by up to one cent.
TOLERANCE = Decimal("0.01")
# Below this much extractable text the PDF is treated as scanned (images only).
MIN_TEXT_CHARS = 20
HS_CODE = re.compile(r"\d{6,10}")
HS_CODE_SEPARATORS = re.compile(r"[\s.\-]")
# A run of letters, digits, dots and dashes: "HS Code: 8207.19.10" -> ["HS", "Code", "8207.19.10"]
CODE_TOKEN = re.compile(r"[0-9A-Za-z][0-9A-Za-z.\-]*")
WHITESPACE = re.compile(r"\s+")


class FlagCode(StrEnum):
    NOT_GROUNDED = "not_grounded"
    GROUNDING_UNVERIFIABLE = "grounding_unverifiable"
    ARITHMETIC_MISMATCH = "arithmetic_mismatch"
    INVALID_FORMAT = "invalid_format"
    UNPARSEABLE_NUMBER = "unparseable_number"
    MISSING_REQUIRED = "missing_required"


@dataclass(frozen=True)
class Flag:
    field: str  # path such as "total" or "line_items[1].amount"; "document" for whole-file flags
    code: FlagCode
    message: str

    def to_dict(self) -> dict[str, str]:
        return {key: str(value) for key, value in asdict(self).items()}


REQUIRED_FIELDS = (
    "invoice_number",
    "invoice_date",
    "supplier_name",
    "buyer_name",
    "currency",
    "total",
)


def run_checks(invoice: InvoiceExtraction, pdf_text: str, today: date) -> list[Flag]:
    flags = [
        *check_required(invoice),
        *check_grounding(invoice, pdf_text),
        *check_formats(invoice, today),
    ]
    flags.extend(check_arithmetic(invoice, already_flagged={f.field for f in flags}))
    return flags


# --- field iteration ---------------------------------------------------------------------------


def all_fields(invoice: InvoiceExtraction) -> Iterator[tuple[str, ExtractedField]]:
    for name in InvoiceExtraction.model_fields:
        if name == "line_items":
            continue
        yield name, getattr(invoice, name)
    for index, item in enumerate(invoice.line_items):
        for name in type(item).model_fields:
            yield f"line_items[{index}].{name}", getattr(item, name)


# --- completeness ------------------------------------------------------------------------------


def check_required(invoice: InvoiceExtraction) -> Iterator[Flag]:
    for name in REQUIRED_FIELDS:
        if not getattr(invoice, name).value:
            yield Flag(name, FlagCode.MISSING_REQUIRED, f"{name} is missing")
    if not invoice.line_items:
        yield Flag("line_items", FlagCode.MISSING_REQUIRED, "The invoice has no line items")


# --- grounding ---------------------------------------------------------------------------------


def normalise_text(text: str) -> str:
    return WHITESPACE.sub(" ", text).strip().casefold()


def check_grounding(invoice: InvoiceExtraction, pdf_text: str) -> Iterator[Flag]:
    """Each quote must appear in the PDF's text layer, or the value may be invented."""
    haystack = normalise_text(pdf_text)
    if len(haystack) < MIN_TEXT_CHARS:
        yield Flag(
            "document",
            FlagCode.GROUNDING_UNVERIFIABLE,
            "The PDF has no text layer (scanned?), so values could not be traced to the document",
        )
        return
    for path, field in all_fields(invoice):
        if field.value is None:
            continue
        if not field.source_quote:
            yield Flag(path, FlagCode.NOT_GROUNDED, "No source quote was given for this value")
        elif normalise_text(field.source_quote) not in haystack:
            yield Flag(path, FlagCode.NOT_GROUNDED, "The quoted text was not found in the PDF")


# --- formats -----------------------------------------------------------------------------------


def check_formats(invoice: InvoiceExtraction, today: date) -> Iterator[Flag]:
    currency = invoice.currency.value
    if currency and currency.upper() not in INVOICE_CURRENCIES:
        yield Flag("currency", FlagCode.INVALID_FORMAT, f"{currency!r} is not an ISO 4217 code")

    issued = invoice.invoice_date.value
    if issued:
        try:
            parsed = date.fromisoformat(issued)
        except ValueError:
            yield Flag("invoice_date", FlagCode.INVALID_FORMAT, f"{issued!r} is not a valid date")
        else:
            if parsed > today:
                yield Flag("invoice_date", FlagCode.INVALID_FORMAT, f"{issued} is in the future")

    for index, item in enumerate(invoice.line_items):
        code = item.hs_code.value
        if not code:
            continue
        printed = item.hs_code.source_quote or code
        if not HS_CODE.fullmatch(HS_CODE_SEPARATORS.sub("", code)) or printed_code_has_letters(
            printed
        ):
            yield Flag(
                f"line_items[{index}].hs_code",
                FlagCode.INVALID_FORMAT,
                f"{printed!r} is not a 6-10 digit HS code",
            )

    for path, field in all_fields(invoice):
        if is_numeric_field(path) and field.value is not None and to_decimal(field.value) is None:
            yield Flag(path, FlagCode.UNPARSEABLE_NUMBER, f"{field.value!r} is not a number")


def printed_code_has_letters(quote: str) -> bool:
    """The value is normalised to digits only, which would silently turn "90-26-B" into "9026"
    or "8207.19.1O" (letter O) into a valid-looking code. So the printed code itself is checked:
    any token that contains a digit must not also contain a letter. Label words such as
    "HS Code:" are separate tokens and are ignored."""
    return any(
        any(c.isdigit() for c in token) and any(c.isalpha() for c in token)
        for token in CODE_TOKEN.findall(quote)
    )


# --- arithmetic --------------------------------------------------------------------------------

NUMERIC_FIELDS = {"subtotal", "tax_total", "total", "quantity", "unit_price", "amount"}


def is_numeric_field(path: str) -> bool:
    return path.rsplit(".", 1)[-1] in NUMERIC_FIELDS


def to_decimal(value: str | None) -> Decimal | None:
    if value is None:
        return None
    try:
        number = Decimal(value.strip())
    except InvalidOperation:
        return None
    return number if number.is_finite() else None


def mismatch(expected: Decimal, actual: Decimal) -> bool:
    return abs(expected - actual) > TOLERANCE


def check_arithmetic(invoice: InvoiceExtraction, already_flagged: set[str]) -> Iterator[Flag]:
    """Skips a sum when any input is missing or already flagged, so one bad value produces one
    flag where it is, not a cascade of follow-on mismatches."""
    line_amounts: list[Decimal] = []
    for index, item in enumerate(invoice.line_items):
        quantity, price, amount = (
            to_decimal(item.quantity.value),
            to_decimal(item.unit_price.value),
            to_decimal(item.amount.value),
        )
        if amount is None:
            line_amounts = []  # cannot total the lines reliably
            break
        line_amounts.append(amount)
        if quantity is not None and price is not None and mismatch(quantity * price, amount):
            yield Flag(
                f"line_items[{index}].amount",
                FlagCode.ARITHMETIC_MISMATCH,
                f"{quantity} x {price} = {quantity * price}, but the invoice says {amount}",
            )

    subtotal = to_decimal(invoice.subtotal.value)
    if line_amounts and subtotal is not None and "subtotal" not in already_flagged:
        lines_total = sum(line_amounts, Decimal(0))
        if mismatch(lines_total, subtotal):
            yield Flag(
                "subtotal",
                FlagCode.ARITHMETIC_MISMATCH,
                f"Line items add up to {lines_total}, but the subtotal is {subtotal}",
            )

    total = to_decimal(invoice.total.value)
    # No tax line means zero tax; a tax value that does not parse is flagged elsewhere.
    tax = Decimal(0) if invoice.tax_total.value is None else to_decimal(invoice.tax_total.value)
    if tax is None or subtotal is None or total is None or "total" in already_flagged:
        return
    if mismatch(subtotal + tax, total):
        yield Flag(
            "total",
            FlagCode.ARITHMETIC_MISMATCH,
            f"Subtotal {subtotal} + tax {tax} = {subtotal + tax}, but the total is {total}",
        )
