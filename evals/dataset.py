"""Generates the evaluation dataset: 30 synthetic commercial invoices with known answers.

    uv run python -m evals.dataset

Writes evals/dataset/<id>.pdf, <id>.json (expected values, printed quotes, expected flags) and
manifest.json (ids and split). Deterministic: the same seed produces the same answers. The PDFs
are committed, so results do not depend on the installed PDF library version.
"""

import io
import json
import random
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import Any

import pypdfium2
from reportlab.lib import colors
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import getSampleStyleSheet
from reportlab.lib.units import mm
from reportlab.platypus import (
    Flowable,
    PageBreak,
    Paragraph,
    SimpleDocTemplate,
    Spacer,
    Table,
    TableStyle,
)

SEED = 2026
DATASET_DIR = Path(__file__).resolve().parent / "dataset"
SCAN_DPI = 110
MONTHS = [
    "January", "February", "March", "April", "May", "June",
    "July", "August", "September", "October", "November", "December",
]  # fmt: skip

SUPPLIERS = [
    ("Acme Bohrtechnik GmbH", "Germany"),
    ("Northsea Valve Co. Ltd", "United Kingdom"),
    ("Kaveri Hydraulics Pvt Ltd", "India"),
    ("Lyon Forage SAS", "France"),
    ("Rotterdam Pump Works B.V.", "Netherlands"),
    ("Milano Raccordi S.p.A.", "Italy"),
]
BUYERS = [
    ("Gulf Drilling Services LLP", "India"),
    ("Western Offshore Energy Ltd", "India"),
    ("Krishna Basin Exploration Pvt Ltd", "India"),
]
PRODUCTS = [
    ("Tungsten carbide drill bit, 8.5 in", "82071910", Decimal("1250.00")),
    ("Mud pump piston seal kit", "84842000", Decimal("310.50")),
    ("Drill pipe thread protector", "73269099", Decimal("18.75")),
    ("Gate valve, 4 in, API 6A", "84818081", Decimal("2140.00")),
    ("Valve stem packing set", "84849000", Decimal("96.40")),
    ("Hydraulic hose assembly, 2 in", "40094200", Decimal("145.00")),
    ("Pressure gauge, 10k psi", "90262040", Decimal("88.25")),
    ("Centrifugal pump impeller", "84139100", Decimal("1875.00")),
    ("Blowout preventer ram rubber", "40169390", Decimal("612.00")),
    ("Stainless steel flange, 6 in", "73079100", Decimal("230.80")),
]
CURRENCIES = {"Germany": "EUR", "France": "EUR", "Netherlands": "EUR", "Italy": "EUR",
              "United Kingdom": "GBP", "India": "USD"}  # fmt: skip

# What is wrong with each invoice, and the flag(s) the checks should raise.
ISSUES = (
    ["clean"] * 12
    + ["line_mismatch"] * 3
    + ["subtotal_mismatch"] * 2
    + ["total_mismatch"] * 2
    + ["bad_hs"] * 2
    + ["future_date"]
    + ["missing_buyer"] * 2
    + ["missing_invoice_number"]
)
SCANNED_ISSUES = ["clean", "clean", "line_mismatch", "bad_hs", "missing_buyer"]


@dataclass
class Line:
    description: str
    hs_printed: str
    hs_expected: str
    quantity: int
    unit_price: Decimal
    printed_amount: Decimal

    @property
    def true_amount(self) -> Decimal:
        return self.quantity * self.unit_price


@dataclass
class Spec:
    id: str
    issue: str
    scanned: bool
    layout: str  # table | european | text
    number_style: str  # en | de | fr
    date_style: str  # dmy_dot | long | iso | dmy_slash
    invoice_number: str | None
    issue_date: date
    supplier: str
    supplier_country: str
    buyer: str | None
    buyer_country: str | None
    currency: str
    tax_rate: Decimal
    lines: list[Line]
    printed_subtotal: Decimal
    printed_tax: Decimal
    printed_total: Decimal
    two_pages: bool
    expected_flags: list[dict[str, str]] = field(default_factory=list)
    unscored: list[str] = field(default_factory=list)
    split: str = "dev"


# --- formatting --------------------------------------------------------------------------------


def money(value: Decimal, style: str) -> str:
    text = f"{value:,.2f}"  # 1,234.50
    if style == "de":
        return text.replace(",", "_").replace(".", ",").replace("_", ".")  # 1.234,50
    if style == "fr":
        return text.replace(",", " ").replace(".", ",")  # 1 234,50
    return text


def printed_date(day: date, style: str) -> str:
    if style == "dmy_dot":
        return f"{day.day:02d}.{day.month:02d}.{day.year}"
    if style == "long":
        return f"{MONTHS[day.month - 1]} {day.day}, {day.year}"
    if style == "dmy_slash":
        return f"{day.day:02d}/{day.month:02d}/{day.year}"
    return day.isoformat()


def printed_hs(code: str, rng: random.Random) -> str:
    style = rng.choice(["dots", "plain", "spaces"])
    if style == "dots":
        return f"{code[:4]}.{code[4:6]}.{code[6:]}"
    if style == "spaces":
        return f"{code[:4]} {code[4:6]} {code[6:]}"
    return code


# --- building specs ----------------------------------------------------------------------------


def build_spec(index: int, issue: str, scanned: bool, rng: random.Random) -> Spec:
    supplier, supplier_country = rng.choice(SUPPLIERS)
    buyer, buyer_country = rng.choice(BUYERS)
    currency = CURRENCIES[supplier_country]
    tax_rate = rng.choice([Decimal("0"), Decimal("0.05"), Decimal("0.18")])
    date_style = rng.choice(["dmy_dot", "long", "iso", "dmy_slash"])
    day = date(2026, rng.randint(1, 10), rng.randint(13, 28))  # day > 12: dd/mm is unambiguous
    two_pages = index % 7 == 3

    lines = []
    for product, hs, price in rng.sample(PRODUCTS, rng.randint(1, 8 if two_pages else 5)):
        quantity = rng.randint(1, 40)
        lines.append(Line(product, printed_hs(hs, rng), hs, quantity, price, quantity * price))

    spec = Spec(
        id=f"inv-{index:02d}",
        issue=issue,
        scanned=scanned,
        layout=rng.choice(["table", "european", "text"]),
        number_style=rng.choice(["en", "de", "fr"]),
        date_style=date_style,
        invoice_number=f"{supplier[:2].upper()}-2026-{rng.randint(1000, 9999)}",
        issue_date=day,
        supplier=supplier,
        supplier_country=supplier_country,
        buyer=buyer,
        buyer_country=buyer_country,
        currency=currency,
        tax_rate=tax_rate,
        lines=lines,
        printed_subtotal=Decimal(0),
        printed_tax=Decimal(0),
        printed_total=Decimal(0),
        two_pages=two_pages,
    )
    plant(spec, rng)
    return spec


def plant(spec: Spec, rng: random.Random) -> None:
    """Applies the issue, computes the printed totals and records the expected flags. Each issue
    is planted so that exactly its own flag is expected (no knock-on mismatches)."""
    issue = spec.issue
    if issue == "line_mismatch":
        k = rng.randrange(len(spec.lines))
        spec.lines[k].printed_amount = spec.lines[k].true_amount + Decimal(
            rng.choice([10, 46, 100])
        )
        spec.expected_flags.append(flag(f"line_items[{k}].amount", "arithmetic_mismatch"))
    if issue == "bad_hs":
        k = rng.randrange(len(spec.lines))
        code = spec.lines[k].hs_expected
        spec.lines[k].hs_printed = rng.choice(
            [f"{code[:4]}.{code[4:6]}.1O", f"{code[:2]}-{code[2:4]}-B"]
        )
        # The "right" normalised value of a malformed code is debatable; only the flag is scored.
        spec.unscored.append(f"line_items[{k}].hs_code")
        spec.expected_flags.append(flag(f"line_items[{k}].hs_code", "invalid_format"))
    if issue == "future_date":
        spec.issue_date = date(2031, 3, 15)
        spec.expected_flags.append(flag("invoice_date", "invalid_format"))
    if issue == "missing_buyer":
        spec.buyer, spec.buyer_country = None, None
        spec.expected_flags.append(flag("buyer_name", "missing_required"))
    if issue == "missing_invoice_number":
        spec.invoice_number = None
        spec.expected_flags.append(flag("invoice_number", "missing_required"))

    spec.printed_subtotal = sum((line.printed_amount for line in spec.lines), Decimal(0))
    if issue == "subtotal_mismatch":
        spec.printed_subtotal += Decimal(rng.choice([50, 120, 300]))
        spec.expected_flags.append(flag("subtotal", "arithmetic_mismatch"))
    spec.printed_tax = (spec.printed_subtotal * spec.tax_rate).quantize(Decimal("0.01"))
    spec.printed_total = spec.printed_subtotal + spec.printed_tax
    if issue == "total_mismatch":
        spec.printed_total += Decimal(rng.choice([25, 80, 200]))
        spec.expected_flags.append(flag("total", "arithmetic_mismatch"))
    if spec.scanned:
        spec.expected_flags.append(flag("document", "grounding_unverifiable"))


def flag(field_path: str, code: str) -> dict[str, str]:
    return {"field": field_path, "code": code}


def build_specs() -> list[Spec]:
    rng = random.Random(SEED)
    kinds = [(issue, False) for issue in ISSUES] + [(issue, True) for issue in SCANNED_ISSUES]
    rng.shuffle(kinds)
    specs = [build_spec(i + 1, issue, scanned, rng) for i, (issue, scanned) in enumerate(kinds)]
    assign_split(specs)
    return specs


# The held-out test split: 10 invoices covering clean, scanned and most kinds of problem. It is
# never used for tuning; headline results come from it.
TEST_QUOTA: dict[tuple[str, bool], int] = {
    ("clean", False): 4,
    ("clean", True): 1,
    ("line_mismatch", False): 1,
    ("subtotal_mismatch", False): 1,
    ("total_mismatch", False): 1,
    ("bad_hs", False): 1,
    ("missing_buyer", False): 1,
}


def assign_split(specs: list[Spec]) -> None:
    remaining = dict(TEST_QUOTA)
    for spec in specs:
        key = (spec.issue, spec.scanned)
        if remaining.get(key, 0) > 0:
            remaining[key] -= 1
            spec.split = "test"
        else:
            spec.split = "dev"


# --- rendering ---------------------------------------------------------------------------------


def render(spec: Spec) -> bytes:
    styles = getSampleStyleSheet()
    m = spec.number_style
    labels = {"table": "Invoice No:", "european": "Invoice Number:", "text": "Inv #"}
    header: list[Flowable] = [
        Paragraph(f"<b>{spec.supplier}</b>", styles["Title"]),
        Paragraph(
            f"Supplier address: Industrial Estate 4, {spec.supplier_country}", styles["Normal"]
        ),
        Spacer(1, 5 * mm),
        Paragraph("<b>COMMERCIAL INVOICE</b>", styles["Heading2"]),
    ]
    if spec.invoice_number:
        header.append(Paragraph(f"{labels[spec.layout]} {spec.invoice_number}", styles["Normal"]))
    header += [
        Paragraph(
            f"Invoice Date: {printed_date(spec.issue_date, spec.date_style)}", styles["Normal"]
        ),
        Paragraph(f"Currency: {spec.currency}", styles["Normal"]),
        Spacer(1, 4 * mm),
    ]
    if spec.buyer:
        bill_to = f"Bill to: {spec.buyer}, {spec.buyer_country}"
        if spec.layout == "european":
            box = Table([[f"Seller: {spec.supplier}", bill_to]], colWidths=[85 * mm, 85 * mm])
            box.setStyle(TableStyle([("BOX", (0, 0), (-1, -1), 0.5, colors.grey)]))
            header.append(box)
        else:
            header.append(Paragraph(bill_to, styles["Normal"]))
        header.append(Spacer(1, 4 * mm))

    body: list[Flowable] = []
    if spec.layout == "text":
        for line in spec.lines:
            body.append(
                Paragraph(
                    f"{line.quantity} x {line.description} (HS {line.hs_printed}) @ "
                    f"{money(line.unit_price, m)} = {money(line.printed_amount, m)}",
                    styles["Normal"],
                )
            )
    else:
        rows = [["Description", "HS Code", "Qty", "Unit Price", "Amount"]]
        for line in spec.lines:
            rows.append([line.description, line.hs_printed, str(line.quantity),
                         money(line.unit_price, m), money(line.printed_amount, m)])  # fmt: skip
        table = Table(rows, colWidths=[70 * mm, 28 * mm, 14 * mm, 30 * mm, 30 * mm])
        table.setStyle(TableStyle([("GRID", (0, 0), (-1, -1), 0.5, colors.grey),
                                   ("BACKGROUND", (0, 0), (-1, 0), colors.lightgrey)]))  # fmt: skip
        body.append(table)

    if spec.two_pages:
        body += [PageBreak(), Paragraph("Continued: totals", styles["Heading3"])]
    rate = f"{spec.tax_rate * 100:.0f}%"
    body += [
        Spacer(1, 4 * mm),
        Paragraph(f"Subtotal: {money(spec.printed_subtotal, m)}", styles["Normal"]),
        Paragraph(f"VAT {rate}: {money(spec.printed_tax, m)}", styles["Normal"]),
        Paragraph(
            f"<b>Total {spec.currency}: {money(spec.printed_total, m)}</b>", styles["Normal"]
        ),
    ]

    buffer = io.BytesIO()
    SimpleDocTemplate(buffer, pagesize=A4, title=spec.id).build(header + body)
    pdf = buffer.getvalue()
    return rasterise(pdf) if spec.scanned else pdf


def rasterise(pdf: bytes) -> bytes:
    """An image-only PDF, like a scanned paper invoice: no text layer at all."""
    document = pypdfium2.PdfDocument(pdf)
    images = [page.render(scale=SCAN_DPI / 72).to_pil().convert("L") for page in document]
    document.close()
    buffer = io.BytesIO()
    first, *rest = images
    first.save(buffer, format="PDF", save_all=True, append_images=rest, resolution=SCAN_DPI)
    return buffer.getvalue()


# --- expected answers --------------------------------------------------------------------------


def plain(value: Decimal) -> str:
    return f"{value:.2f}"


def expected(spec: Spec) -> dict[str, Any]:
    """Expected normalised values plus the text as printed (a perfect source_quote)."""
    m = spec.number_style

    def entry(value: str | None, quote: str | None) -> dict[str, str | None]:
        return {"value": value, "source_quote": quote}

    fields: dict[str, Any] = {
        "invoice_number": entry(spec.invoice_number, spec.invoice_number),
        "invoice_date": entry(
            spec.issue_date.isoformat(), printed_date(spec.issue_date, spec.date_style)
        ),
        "supplier_name": entry(spec.supplier, spec.supplier),
        "supplier_country": entry(spec.supplier_country, spec.supplier_country),
        "buyer_name": entry(spec.buyer, spec.buyer),
        "buyer_country": entry(spec.buyer_country, spec.buyer_country),
        "currency": entry(spec.currency, spec.currency),
        "line_items": [
            {
                "description": entry(line.description, line.description),
                "hs_code": entry(line.hs_expected, line.hs_printed),
                "quantity": entry(str(line.quantity), str(line.quantity)),
                "unit_price": entry(plain(line.unit_price), money(line.unit_price, m)),
                "amount": entry(plain(line.printed_amount), money(line.printed_amount, m)),
            }
            for line in spec.lines
        ],
        "subtotal": entry(plain(spec.printed_subtotal), money(spec.printed_subtotal, m)),
        "tax_total": entry(plain(spec.printed_tax), money(spec.printed_tax, m)),
        "total": entry(plain(spec.printed_total), money(spec.printed_total, m)),
    }
    return {
        "id": spec.id,
        "split": spec.split,
        "issue": spec.issue,
        "scanned": spec.scanned,
        "layout": spec.layout,
        "number_style": spec.number_style,
        "date_style": spec.date_style,
        "fields": fields,
        "expected_flags": spec.expected_flags,
        "unscored": spec.unscored,
    }


def main() -> None:
    DATASET_DIR.mkdir(exist_ok=True)
    specs = build_specs()
    for spec in specs:
        (DATASET_DIR / f"{spec.id}.pdf").write_bytes(render(spec))
        (DATASET_DIR / f"{spec.id}.json").write_text(
            json.dumps(expected(spec), indent=2) + "\n", encoding="utf-8"
        )
    manifest = {
        "seed": SEED,
        "invoices": [
            {"id": s.id, "split": s.split, "issue": s.issue, "scanned": s.scanned} for s in specs
        ],
    }
    (DATASET_DIR / "manifest.json").write_text(
        json.dumps(manifest, indent=2) + "\n", encoding="utf-8"
    )
    counts = {split: sum(s.split == split for s in specs) for split in ("dev", "test")}
    print(f"wrote {len(specs)} invoices to {DATASET_DIR} ({counts})")


if __name__ == "__main__":
    main()
