"""Generates synthetic commercial invoices with known correct answers.

    uv run python scripts/make_sample_invoices.py

Writes samples/<name>.pdf and samples/<name>.expected.json. The JSON holds the values a perfect
extraction would return (normalised) and the flags the checks should raise. All companies and
numbers are invented.
"""

import json
from dataclasses import dataclass, field
from decimal import Decimal
from pathlib import Path

from reportlab.lib import colors
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import getSampleStyleSheet
from reportlab.lib.units import mm
from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle

OUTPUT_DIR = Path(__file__).resolve().parent.parent / "samples"


@dataclass
class Line:
    description: str
    hs_code: str  # as printed
    quantity: int
    unit_price: Decimal
    printed_amount: Decimal | None = None  # set to plant an arithmetic error

    @property
    def amount(self) -> Decimal:
        if self.printed_amount is not None:
            return self.printed_amount
        return self.quantity * self.unit_price


@dataclass
class Invoice:
    name: str
    number: str
    date_printed: str
    date_iso: str
    supplier: str
    supplier_country: str
    buyer: str | None
    buyer_country: str | None
    currency: str
    tax_rate: Decimal
    lines: list[Line]
    expected_flags: list[dict[str, str]] = field(default_factory=list)

    @property
    def subtotal(self) -> Decimal:
        return sum((line.amount for line in self.lines), Decimal(0))

    @property
    def tax(self) -> Decimal:
        return (self.subtotal * self.tax_rate).quantize(Decimal("0.01"))

    @property
    def total(self) -> Decimal:
        return self.subtotal + self.tax


def money(value: Decimal) -> str:
    return f"{value:,.2f}"


def render(invoice: Invoice, path: Path) -> None:
    styles = getSampleStyleSheet()
    story = [
        Paragraph(f"<b>{invoice.supplier}</b>", styles["Title"]),
        Paragraph(f"Country of origin: {invoice.supplier_country}", styles["Normal"]),
        Spacer(1, 6 * mm),
        Paragraph("<b>COMMERCIAL INVOICE</b>", styles["Heading2"]),
        Paragraph(f"Invoice No: {invoice.number}", styles["Normal"]),
        Paragraph(f"Invoice Date: {invoice.date_printed}", styles["Normal"]),
        Paragraph(f"Currency: {invoice.currency}", styles["Normal"]),
        Spacer(1, 4 * mm),
    ]
    if invoice.buyer:
        story += [
            Paragraph(f"Bill to: {invoice.buyer}", styles["Normal"]),
            Paragraph(f"Country: {invoice.buyer_country}", styles["Normal"]),
            Spacer(1, 4 * mm),
        ]

    rows = [["Description", "HS Code", "Qty", "Unit Price", "Amount"]]
    for line in invoice.lines:
        quantity, price, amount = str(line.quantity), money(line.unit_price), money(line.amount)
        rows.append([line.description, line.hs_code, quantity, price, amount])
    rows += [
        ["", "", "", "Subtotal", money(invoice.subtotal)],
        ["", "", "", f"VAT {invoice.tax_rate * 100:.0f}%", money(invoice.tax)],
        ["", "", "", f"Total {invoice.currency}", money(invoice.total)],
    ]
    table = Table(rows, colWidths=[70 * mm, 28 * mm, 14 * mm, 30 * mm, 30 * mm])
    table.setStyle(
        TableStyle(
            [
                ("GRID", (0, 0), (-1, len(invoice.lines)), 0.5, colors.grey),
                ("BACKGROUND", (0, 0), (-1, 0), colors.lightgrey),
                ("ALIGN", (2, 1), (-1, -1), "RIGHT"),
            ]
        )
    )
    story.append(table)
    SimpleDocTemplate(str(path), pagesize=A4, title=invoice.number).build(story)


def expected(invoice: Invoice) -> dict[str, object]:
    def plain(value: Decimal) -> str:
        return f"{value:.2f}"

    return {
        "fields": {
            "invoice_number": invoice.number,
            "invoice_date": invoice.date_iso,
            "supplier_name": invoice.supplier,
            "supplier_country": invoice.supplier_country,
            "buyer_name": invoice.buyer,
            "buyer_country": invoice.buyer_country,
            "currency": invoice.currency,
            "line_items": [
                {
                    "description": line.description,
                    "hs_code": "".join(c for c in line.hs_code if c.isalnum()),
                    "quantity": str(line.quantity),
                    "unit_price": plain(line.unit_price),
                    "amount": plain(line.amount),
                }
                for line in invoice.lines
            ],
            "subtotal": plain(invoice.subtotal),
            "tax_total": plain(invoice.tax),
            "total": plain(invoice.total),
        },
        "expected_flags": invoice.expected_flags,
    }


SAMPLES = [
    Invoice(
        name="clean",
        number="AT-2026-1187",
        date_printed="14.10.2026",
        date_iso="2026-10-14",
        supplier="Acme Bohrtechnik GmbH",
        supplier_country="Germany",
        buyer="Gulf Drilling Services LLP",
        buyer_country="India",
        currency="EUR",
        tax_rate=Decimal("0.05"),
        lines=[
            Line("Tungsten carbide drill bit, 8.5 in", "8207.19.10", 6, Decimal("1250.00")),
            Line("Mud pump piston seal kit", "8484.20.00", 12, Decimal("310.50")),
            Line("Drill pipe thread protector", "7326.90.99", 40, Decimal("18.75")),
        ],
    ),
    Invoice(
        name="arithmetic-error",
        number="NS-55021",
        date_printed="October 3, 2026",
        date_iso="2026-10-03",
        supplier="Northsea Valve Co. Ltd",
        supplier_country="United Kingdom",
        buyer="Gulf Drilling Services LLP",
        buyer_country="India",
        currency="GBP",
        tax_rate=Decimal("0"),
        lines=[
            Line("Gate valve, 4 in, API 6A", "8481.80.81", 3, Decimal("2140.00")),
            # 5 x 96.40 = 482.00, but the invoice prints 528.00.
            Line("Valve stem packing set", "8484.90.00", 5, Decimal("96.40"), Decimal("528.00")),
        ],
        expected_flags=[{"field": "line_items[1].amount", "code": "arithmetic_mismatch"}],
    ),
    Invoice(
        name="bad-format",
        number="KX/2026/0098",
        date_printed="2026-09-21",
        date_iso="2026-09-21",
        supplier="Kaveri Hydraulics Pvt Ltd",
        supplier_country="India",
        buyer=None,  # missing on purpose
        buyer_country=None,
        currency="USD",
        tax_rate=Decimal("0"),
        lines=[
            # 6 digits once the dot is removed: valid
            Line("Hydraulic hose assembly, 2 in", "4009.42", 10, Decimal("145.00")),
            Line("Pressure gauge, 10k psi", "90-26-B", 4, Decimal("88.25")),  # invalid HS code
        ],
        expected_flags=[
            {"field": "buyer_name", "code": "missing_required"},
            {"field": "line_items[1].hs_code", "code": "invalid_format"},
        ],
    ),
]


def main() -> None:
    OUTPUT_DIR.mkdir(exist_ok=True)
    for invoice in SAMPLES:
        render(invoice, OUTPUT_DIR / f"{invoice.name}.pdf")
        (OUTPUT_DIR / f"{invoice.name}.expected.json").write_text(
            json.dumps(expected(invoice), indent=2) + "\n", encoding="utf-8"
        )
        print(f"wrote samples/{invoice.name}.pdf")


if __name__ == "__main__":
    main()
