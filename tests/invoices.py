"""Builders for InvoiceExtraction test data."""

from typing import Any

from docflow.invoice import ExtractedField, InvoiceExtraction, LineItem

# Text a PDF of this invoice would contain, for grounding checks.
SAMPLE_TEXT = """
Acme Tools GmbH, Germany
Invoice INV-2026-0042   Date: 15.10.2026
Bill to: Gulf Drilling Services LLP, India
Drill bit, tungsten carbide   8207.19.10   2   1,250.00   2,500.00
Mud pump seal kit             8484.20.00   4     310.50   1,242.00
Subtotal 3,742.00
VAT 0.00
Total EUR 3,742.00
"""


def field(value: str | None, quote: str | None = "") -> ExtractedField:
    """A field whose quote defaults to the value itself."""
    return ExtractedField(value=value, source_quote=value if quote == "" else quote)


def line(
    description: str = "Drill bit, tungsten carbide",
    hs_code: str = "82071910",
    quantity: str = "2",
    unit_price: str = "1250.00",
    amount: str = "2500.00",
    quotes: dict[str, str | None] | None = None,
) -> LineItem:
    quotes = quotes or {}
    values = {
        "description": description,
        "hs_code": hs_code,
        "quantity": quantity,
        "unit_price": unit_price,
        "amount": amount,
    }
    return LineItem(**{name: field(value, quotes.get(name, "")) for name, value in values.items()})


def make_invoice(**overrides: Any) -> InvoiceExtraction:
    """A consistent, fully grounded invoice matching SAMPLE_TEXT. Override any field."""
    data: dict[str, Any] = {
        "invoice_number": field("INV-2026-0042"),
        "invoice_date": field("2026-10-15", "15.10.2026"),
        "supplier_name": field("Acme Tools GmbH"),
        "supplier_country": field("Germany"),
        "buyer_name": field("Gulf Drilling Services LLP"),
        "buyer_country": field("India"),
        "currency": field("EUR"),
        "line_items": [
            line(
                hs_code="82071910",
                quotes={"hs_code": "8207.19.10", "unit_price": "1,250.00", "amount": "2,500.00"},
            ),
            line(
                description="Mud pump seal kit",
                hs_code="84842000",
                quantity="4",
                unit_price="310.50",
                amount="1242.00",
                quotes={"hs_code": "8484.20.00", "amount": "1,242.00"},
            ),
        ],
        "subtotal": field("3742.00", "3,742.00"),
        "tax_total": field("0.00"),
        "total": field("3742.00", "3,742.00"),
    }
    data.update(overrides)
    return InvoiceExtraction(**data)
