"""The evaluation dataset itself must be right before any model is judged with it."""

import json
from datetime import date
from pathlib import Path
from typing import Any

import pytest

from docflow.checks import run_checks
from docflow.invoice import InvoiceExtraction
from docflow.pdf import inspect_pdf
from evals.dataset import DATASET_DIR, build_specs, expected

EVAL_TODAY = date(2026, 11, 18)
MANIFEST = json.loads((DATASET_DIR / "manifest.json").read_text(encoding="utf-8"))
IDS = [entry["id"] for entry in MANIFEST["invoices"]]


def load(invoice_id: str) -> dict[str, Any]:
    data: dict[str, Any] = json.loads(
        (DATASET_DIR / f"{invoice_id}.json").read_text(encoding="utf-8")
    )
    return data


@pytest.mark.parametrize("invoice_id", IDS)
def test_perfect_extraction_raises_exactly_the_expected_flags(invoice_id: str) -> None:
    """Run the real checks on the expected values, quoting exactly what is printed, against the
    real PDF text layer. Anything else than the expected flags means the dataset is wrong."""
    answer = load(invoice_id)
    pdf_text = inspect_pdf(Path(DATASET_DIR / f"{invoice_id}.pdf").read_bytes(), max_pages=20).text
    perfect = InvoiceExtraction.model_validate(answer["fields"])

    flags = run_checks(perfect, pdf_text, EVAL_TODAY)

    actual = {(f.field, f.code.value) for f in flags}
    wanted = {(f["field"], f["code"]) for f in answer["expected_flags"]}
    assert actual == wanted


def test_scanned_invoices_have_no_text_layer() -> None:
    for entry in MANIFEST["invoices"]:
        text = inspect_pdf((DATASET_DIR / f"{entry['id']}.pdf").read_bytes(), max_pages=20).text
        assert (len(text.strip()) == 0) == entry["scanned"], entry["id"]


def test_generation_is_deterministic_and_matches_the_committed_answers() -> None:
    for spec in build_specs():
        assert expected(spec) == load(spec.id), spec.id


def test_split_is_10_test_and_20_dev() -> None:
    splits = [entry["split"] for entry in MANIFEST["invoices"]]
    assert splits.count("test") == 10
    assert splits.count("dev") == 20
