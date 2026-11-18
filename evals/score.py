"""Scores saved extraction outputs against the dataset. No API calls: re-run it as often as you
like, and CI re-runs it to prove the committed results are reproducible.

    uv run python -m evals.score evals/runs/<run_id>

Flags are recomputed from each saved extraction with the current checks code and the PDF's text
layer, exactly as production would compute them.
"""

import json
import statistics
import sys
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

from docflow.checks import run_checks
from docflow.invoice import InvoiceExtraction
from docflow.pdf import inspect_pdf
from evals.dataset import DATASET_DIR

# Fixed "today" so results do not change with the date they are scored on.
EVAL_TODAY = date(2026, 11, 18)
NUMERIC = {"quantity", "unit_price", "amount", "subtotal", "tax_total", "total"}
TEXT = {
    "invoice_number",
    "supplier_name",
    "supplier_country",
    "buyer_name",
    "buyer_country",
    "description",
}
GROUNDING_UNVERIFIABLE = "grounding_unverifiable"

Flag = tuple[str, str]


# --- comparing values --------------------------------------------------------------------------


def as_decimal(value: str | None) -> Decimal | None:
    if value is None:
        return None
    try:
        return Decimal(value.strip())
    except InvalidOperation:
        return None


def normalise_text(value: str | None) -> str | None:
    return None if value is None else " ".join(value.split()).casefold()


def values_match(name: str, expected: str | None, actual: str | None) -> bool:
    """Numbers compare as Decimal (2500 == 2500.00), names ignore case and spacing, HS codes
    compare as digits, everything else exactly. A missing value must be null."""
    if expected is None or actual is None:
        return expected is None and (actual is None or actual.strip() == "")
    if name in NUMERIC:
        a, b = as_decimal(expected), as_decimal(actual)
        return a is not None and a == b
    if name in TEXT:
        return normalise_text(expected) == normalise_text(actual)
    if name == "hs_code":
        return "".join(c for c in expected if c.isdigit()) == "".join(
            c for c in actual if c.isdigit()
        )
    return expected.strip() == actual.strip()


def flatten(fields: dict[str, Any]) -> dict[str, str | None]:
    flat: dict[str, str | None] = {}
    for name, item in fields.items():
        if name == "line_items":
            for index, line in enumerate(item or []):
                for key, value in line.items():
                    flat[f"line_items[{index}].{key}"] = value["value"]
        else:
            flat[name] = item["value"]
    return flat


@dataclass
class FieldResult:
    path: str
    expected: str | None
    actual: str | None
    correct: bool


def score_fields(
    expected: dict[str, Any], actual: dict[str, Any] | None, unscored: list[str]
) -> list[FieldResult]:
    want = flatten(expected)
    got = flatten(actual) if actual is not None else {}
    results = []
    for path, value in want.items():
        if path in unscored:
            continue
        name = path.rsplit(".", 1)[-1]
        results.append(
            FieldResult(path, value, got.get(path), values_match(name, value, got.get(path)))
        )
    return results


# --- one output --------------------------------------------------------------------------------


@dataclass
class InvoiceScore:
    invoice_id: str
    config: str
    repeat: str
    split: str
    issue: str
    scanned: bool
    outcome: str
    fields: list[FieldResult]
    expected_flags: set[Flag]
    actual_flags: set[Flag]
    extra_lines: int
    cost_usd: Decimal | None
    latency_ms: int
    input_tokens: int | None
    output_tokens: int | None
    reasoning_tokens: int | None


_pdf_text_cache: dict[str, str] = {}


def pdf_text(invoice_id: str) -> str:
    if invoice_id not in _pdf_text_cache:
        data = (DATASET_DIR / f"{invoice_id}.pdf").read_bytes()
        _pdf_text_cache[invoice_id] = inspect_pdf(data, max_pages=20).text
    return _pdf_text_cache[invoice_id]


def score_output(output: dict[str, Any], answer: dict[str, Any]) -> InvoiceScore:
    extraction = output.get("invoice")
    actual_flags: set[Flag] = set()
    extra_lines = 0
    if extraction is not None:
        invoice = InvoiceExtraction.model_validate(extraction)
        flags = run_checks(invoice, pdf_text(answer["id"]), EVAL_TODAY)
        actual_flags = {(f.field, f.code.value) for f in flags}
        extra_lines = max(0, len(invoice.line_items) - len(answer["fields"]["line_items"]))
    usage = output.get("usage") or {}
    cost = output.get("cost_usd")
    return InvoiceScore(
        invoice_id=answer["id"],
        config=output["config"],
        repeat=str(output["repeat"]),
        split=answer["split"],
        issue=answer["issue"],
        scanned=answer["scanned"],
        outcome=output["outcome"],
        fields=score_fields(answer["fields"], extraction, answer["unscored"]),
        expected_flags={(f["field"], f["code"]) for f in answer["expected_flags"]},
        actual_flags=actual_flags,
        extra_lines=extra_lines,
        cost_usd=Decimal(cost) if cost is not None else None,
        latency_ms=int(output["latency_ms"]),
        input_tokens=usage.get("input_tokens"),
        output_tokens=usage.get("output_tokens"),
        reasoning_tokens=usage.get("reasoning_tokens"),
    )


# --- aggregate metrics -------------------------------------------------------------------------


@dataclass
class FlagCounts:
    tp: int = 0
    fp: int = 0
    fn: int = 0

    def add(self, expected: set[Flag], actual: set[Flag]) -> None:
        self.tp += len(expected & actual)
        self.fp += len(actual - expected)
        self.fn += len(expected - actual)

    @property
    def precision(self) -> float | None:
        return self.tp / (self.tp + self.fp) if self.tp + self.fp else None

    @property
    def recall(self) -> float | None:
        return self.tp / (self.tp + self.fn) if self.tp + self.fn else None


def without_unverifiable(flags: set[Flag]) -> set[Flag]:
    """grounding_unverifiable is decided by the PDF alone, not the model, so it is excluded from
    the 'planted problem' metrics."""
    return {f for f in flags if f[1] != GROUNDING_UNVERIFIABLE}


def percentile(values: list[int], q: float) -> int:
    if not values:
        return 0
    ordered = sorted(values)
    return ordered[min(len(ordered) - 1, round(q * (len(ordered) - 1)))]


def ratio(numerator: int, denominator: int) -> float | None:
    return numerator / denominator if denominator else None


def summarise(scores: list[InvoiceScore]) -> dict[str, Any]:
    fields = [f for s in scores for f in s.fields]
    problems = FlagCounts()
    all_flags = FlagCounts()
    by_code: dict[str, FlagCounts] = defaultdict(FlagCounts)
    per_field: dict[str, list[bool]] = defaultdict(list)
    for s in scores:
        problems.add(without_unverifiable(s.expected_flags), without_unverifiable(s.actual_flags))
        all_flags.add(s.expected_flags, s.actual_flags)
        for code in {c for _, c in s.expected_flags | s.actual_flags}:
            by_code[code].add(
                {f for f in s.expected_flags if f[1] == code},
                {f for f in s.actual_flags if f[1] == code},
            )
        for f in s.fields:
            per_field[f.path.rsplit(".", 1)[-1]].append(f.correct)

    clean = [s for s in scores if s.issue == "clean"]
    clean_text = [s for s in clean if not s.scanned]
    costs = [s.cost_usd for s in scores if s.cost_usd is not None]
    accuracy_by_repeat: dict[str, list[bool]] = defaultdict(list)
    for s in scores:
        accuracy_by_repeat[s.repeat].extend(f.correct for f in s.fields)

    return {
        "invoices": len(scores),
        "failures": sum(s.outcome != "succeeded" for s in scores),
        "field_accuracy": ratio(sum(f.correct for f in fields), len(fields)),
        "invoices_fully_correct": ratio(
            sum(all(f.correct for f in s.fields) for s in scores), len(scores)
        ),
        "field_accuracy_by_repeat": {
            r: ratio(sum(v), len(v)) for r, v in sorted(accuracy_by_repeat.items())
        },
        "per_field_accuracy": {k: ratio(sum(v), len(v)) for k, v in sorted(per_field.items())},
        "problem_recall": problems.recall,
        "problem_precision": problems.precision,
        "flag_precision": all_flags.precision,
        "flag_recall": all_flags.recall,
        "per_code": {
            code: {"tp": c.tp, "fp": c.fp, "fn": c.fn, "precision": c.precision, "recall": c.recall}
            for code, c in sorted(by_code.items())
        },
        "false_flags_per_clean_invoice": ratio(
            sum(len(without_unverifiable(s.actual_flags) - s.expected_flags) for s in clean),
            len(clean),
        ),
        "ungrounded_on_clean_text": sum(
            sum(1 for _, code in s.actual_flags if code == "not_grounded") for s in clean_text
        ),
        "extra_lines": sum(s.extra_lines for s in scores),
        "cost_total_usd": str(sum(costs, Decimal(0))),
        "cost_per_invoice_usd": str(
            (sum(costs, Decimal(0)) / len(costs)).quantize(Decimal("0.000001"))
        )
        if costs
        else None,
        "latency_p50_ms": percentile([s.latency_ms for s in scores], 0.5),
        "latency_p95_ms": percentile([s.latency_ms for s in scores], 0.95),
        "mean_input_tokens": statistics.fmean(
            [s.input_tokens for s in scores if s.input_tokens] or [0]
        ),
        "mean_output_tokens": statistics.fmean(
            [s.output_tokens for s in scores if s.output_tokens] or [0]
        ),
        "mean_reasoning_tokens": statistics.fmean(
            [s.reasoning_tokens for s in scores if s.reasoning_tokens is not None] or [0]
        ),
    }


@dataclass
class RunResults:
    by_config: dict[str, dict[str, dict[str, Any]]] = field(default_factory=dict)  # config -> split
    misses: list[dict[str, Any]] = field(default_factory=list)


def load_scores(run_dir: Path) -> list[InvoiceScore]:
    answers = {
        p.stem: json.loads(p.read_text(encoding="utf-8")) for p in DATASET_DIR.glob("inv-*.json")
    }
    scores = []
    for path in sorted(run_dir.glob("*/r*/inv-*.json")):
        output = json.loads(path.read_text(encoding="utf-8"))
        scores.append(score_output(output, answers[output["invoice_id"]]))
    return scores


def score_run(run_dir: Path) -> RunResults:
    scores = load_scores(run_dir)
    results = RunResults()
    for config in sorted({s.config for s in scores}):
        mine = [s for s in scores if s.config == config]
        results.by_config[config] = {
            split: summarise([s for s in mine if split == "all" or s.split == split])
            for split in ("dev", "test", "all")
        }
    for s in scores:
        missed = without_unverifiable(s.expected_flags) - s.actual_flags
        false = without_unverifiable(s.actual_flags) - s.expected_flags
        wrong = [f for f in s.fields if not f.correct]
        if missed or false or wrong or s.outcome != "succeeded":
            results.misses.append(
                {
                    "config": s.config,
                    "repeat": s.repeat,
                    "invoice": s.invoice_id,
                    "split": s.split,
                    "issue": s.issue + (" (scanned)" if s.scanned else ""),
                    "outcome": s.outcome,
                    "missed_flags": sorted(missed),
                    "false_flags": sorted(false),
                    "wrong_fields": [(f.path, f.expected, f.actual) for f in wrong],
                }
            )
    return results


def results_document(run_dir: Path) -> dict[str, Any]:
    """The JSON written to results.json. Round-tripped through JSON so it compares equal to a
    loaded file (CI checks that re-scoring the committed outputs reproduces it)."""
    results = score_run(run_dir)
    out = {"run": run_dir.name, "results": results.by_config, "misses": results.misses}
    loaded: dict[str, Any] = json.loads(json.dumps(out, default=str))
    return loaded


def pct(value: float | None) -> str:
    return "n/a" if value is None else f"{value * 100:.1f}%"


def render_markdown(document: dict[str, Any]) -> str:
    results: dict[str, dict[str, dict[str, Any]]] = document["results"]
    configs = list(results)
    lines = [f"# Evaluation results: run `{document['run']}`", ""]
    for split, title in (("test", "Held-out test split (headline)"), ("dev", "Dev split")):
        lines += [
            f"## {title}",
            "",
            "| Config | Field accuracy | All fields right | Problem recall | Problem precision "
            "| False flags / clean invoice | Ungrounded (clean, text) | Failures | $ / invoice "
            "| p50 latency | p95 latency | Accuracy per repeat |",
            "|---|---|---|---|---|---|---|---|---|---|---|---|",
        ]
        for config in configs:
            r = results[config][split]
            per_repeat = ", ".join(pct(v) for v in r["field_accuracy_by_repeat"].values())
            false_flags = r["false_flags_per_clean_invoice"]
            lines.append(
                f"| {config} | {pct(r['field_accuracy'])} | {pct(r['invoices_fully_correct'])} "
                f"| {pct(r['problem_recall'])} | {pct(r['problem_precision'])} "
                f"| {'n/a' if false_flags is None else f'{false_flags:.2f}'} "
                f"| {r['ungrounded_on_clean_text']} | {r['failures']} "
                f"| ${r['cost_per_invoice_usd']} | {r['latency_p50_ms'] / 1000:.1f}s "
                f"| {r['latency_p95_ms'] / 1000:.1f}s | {per_repeat} |"
            )
        lines.append("")

    fields = sorted({f for c in configs for f in results[c]["all"]["per_field_accuracy"]})
    lines += [
        "## Field accuracy by field (all invoices)",
        "",
        "| Field | " + " | ".join(configs) + " |",
    ]
    lines.append("|---|" + "---|" * len(configs))
    for name in fields:
        cells = [pct(results[c]["all"]["per_field_accuracy"].get(name)) for c in configs]
        lines.append(f"| {name} | " + " | ".join(cells) + " |")

    codes = sorted({code for c in configs for code in results[c]["all"]["per_code"]})
    lines += ["", "## Flags by type (all invoices): recall / precision", ""]
    lines += ["| Flag | " + " | ".join(configs) + " |", "|---|" + "---|" * len(configs)]
    for code in codes:
        cells = []
        for c in configs:
            stats = results[c]["all"]["per_code"].get(code)
            cells.append(
                "-" if stats is None else f"{pct(stats['recall'])} / {pct(stats['precision'])}"
            )
        lines.append(f"| {code} | " + " | ".join(cells) + " |")

    lines += ["", "## Total cost", ""]
    for config in configs:
        lines.append(f"- {config}: ${results[config]['all']['cost_total_usd']}")
    return "\n".join(lines) + "\n"


def main() -> None:
    run_dir = Path(sys.argv[1])
    document = results_document(run_dir)
    (run_dir / "results.json").write_text(json.dumps(document, indent=2) + "\n", encoding="utf-8")
    (run_dir / "results.md").write_text(render_markdown(document), encoding="utf-8")
    print(render_markdown(document))


if __name__ == "__main__":
    main()
