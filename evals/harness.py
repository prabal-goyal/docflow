"""Runs the extractor on the dataset with several model configurations. CALLS OPENAI: costs money.

    uv run python -m evals.harness --run-id baseline --configs luna-medium,luna-low \\
        --repeats 2 --split all --budget 3.00
    uv run python -m evals.harness ... --dry-run     # shows what would run and the cost bound

Each output is saved to evals/runs/<run_id>/<config>/r<repeat>/<invoice>.json, and existing
outputs are skipped, so an interrupted run resumes without paying twice. Scoring is separate
(evals.score) and free.
"""

import argparse
import json
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, dataclass
from decimal import Decimal
from pathlib import Path
from typing import Any

import openai

from docflow.config import Settings, get_settings
from docflow.extraction import PermanentExtractionError, RetryableExtractionError, Usage
from docflow.openai_extractor import OpenAIExtractor
from docflow.pipeline import estimate_cost
from evals.dataset import DATASET_DIR

RUNS_DIR = Path(__file__).resolve().parent / "runs"
WORKERS = 4


@dataclass(frozen=True)
class Config:
    model: str
    effort: str
    # Expected cost per invoice: Luna measured in the M2b live test (~$0.0006), others scaled
    # by their price ratio and rounded up. Used for the dry-run estimate.
    typical_cost_per_call: Decimal
    # Upper bound per invoice, reserved before each call so parallel calls cannot overshoot the cap.
    max_cost_per_call: Decimal


CONFIGS = {
    "luna-medium": Config("gpt-6-luna", "medium", Decimal("0.0008"), Decimal("0.005")),
    "luna-low": Config("gpt-6-luna", "low", Decimal("0.0006"), Decimal("0.005")),
    "terra-medium": Config("gpt-6.1-terra", "medium", Decimal("0.008"), Decimal("0.05")),
    "sol-medium": Config("gpt-6.1-sol", "medium", Decimal("0.016"), Decimal("0.10")),
}


class BudgetExceededError(Exception):
    pass


class Budget:
    """Reserves the worst-case cost before each call and settles the real cost after, so
    parallel calls can never push spending past the cap."""

    def __init__(self, cap: Decimal) -> None:
        self.cap = cap
        self.spent = Decimal(0)
        self.reserved = Decimal(0)
        self._lock = threading.Lock()

    def reserve(self, amount: Decimal) -> None:
        with self._lock:
            if self.spent + self.reserved + amount > self.cap:
                raise BudgetExceededError(
                    f"Budget ${self.cap} would be exceeded (spent ${self.spent})"
                )
            self.reserved += amount

    def settle(self, reserved: Decimal, actual: Decimal) -> None:
        with self._lock:
            self.reserved -= reserved
            self.spent += actual


def output_path(run_id: str, config: str, repeat: int, invoice_id: str) -> Path:
    return RUNS_DIR / run_id / config / f"r{repeat}" / f"{invoice_id}.json"


def run_one(
    extractor: OpenAIExtractor,
    settings: Settings,
    config_name: str,
    repeat: int,
    invoice_id: str,
    budget: Budget,
) -> dict[str, Any]:
    config = CONFIGS[config_name]
    budget.reserve(config.max_cost_per_call)
    pdf = (DATASET_DIR / f"{invoice_id}.pdf").read_bytes()
    started = time.monotonic()
    invoice: dict[str, Any] | None = None
    usage: Usage | None = None
    error: str | None = None
    outcome = "succeeded"
    try:
        result = extractor.extract(pdf)
        invoice, usage = result.invoice.model_dump(mode="json"), result.usage
    except PermanentExtractionError as exc:
        outcome, error, usage = "failed", str(exc), exc.usage
    except RetryableExtractionError as exc:
        outcome, error = "error", str(exc)
    latency_ms = round((time.monotonic() - started) * 1000)
    cost = estimate_cost(usage, settings) if usage else None
    budget.settle(config.max_cost_per_call, cost or Decimal(0))
    return {
        "invoice_id": invoice_id,
        "config": config_name,
        "model": config.model,
        "effort": config.effort,
        "repeat": repeat,
        "outcome": outcome,
        "error": error,
        "invoice": invoice,
        "usage": asdict(usage) if usage else None,
        "cost_usd": str(cost) if cost is not None else None,
        "latency_ms": latency_ms,
    }


def main() -> None:
    parser = argparse.ArgumentParser(prog="evals.harness")
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--configs", default=",".join(CONFIGS))
    parser.add_argument("--repeats", type=int, default=1)
    parser.add_argument("--split", choices=["dev", "test", "all"], default="all")
    parser.add_argument("--budget", type=Decimal, default=Decimal("3.00"))
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    manifest = json.loads((DATASET_DIR / "manifest.json").read_text(encoding="utf-8"))
    ids = [e["id"] for e in manifest["invoices"] if args.split in ("all", e["split"])]
    configs = args.configs.split(",")
    unknown = set(configs) - set(CONFIGS)
    if unknown:
        parser.error(f"unknown configs: {sorted(unknown)}")

    jobs = [
        (c, r, i)
        for c in configs
        for r in range(1, args.repeats + 1)
        for i in ids
        if not output_path(args.run_id, c, r, i).exists()
    ]
    typical = sum((CONFIGS[c].typical_cost_per_call for c, _, _ in jobs), Decimal(0))
    bound = sum((CONFIGS[c].max_cost_per_call for c, _, _ in jobs), Decimal(0))
    print(
        f"{len(jobs)} calls to make: expected about ${typical:.2f} "
        f"(if every call hit its upper bound: ${bound:.2f}); "
        f"spending stops at the ${args.budget} cap"
    )
    if args.dry_run or not jobs:
        return

    settings = get_settings()
    if settings.openai_api_key is None or not settings.openai_api_key.get_secret_value():
        raise SystemExit("OPENAI_API_KEY is not set")
    client = openai.OpenAI(
        api_key=settings.openai_api_key.get_secret_value(), timeout=settings.llm_timeout_seconds
    )
    extractors = {}
    per_config_settings = {}
    for name in configs:
        cfg = CONFIGS[name]
        per_config_settings[name] = settings.model_copy(
            update={"llm_model": cfg.model, "llm_reasoning_effort": cfg.effort}
        )
        extractors[name] = OpenAIExtractor(client, per_config_settings[name])

    budget = Budget(args.budget)

    def work(job: tuple[str, int, str]) -> str:
        config, repeat, invoice_id = job
        result = run_one(
            extractors[config], per_config_settings[config], config, repeat, invoice_id, budget
        )
        path = output_path(args.run_id, config, repeat, invoice_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
        return f"{config} r{repeat} {invoice_id}: {result['outcome']} ${result['cost_usd']}"

    with ThreadPoolExecutor(max_workers=WORKERS) as pool:
        for line in pool.map(work, jobs):
            print(line, flush=True)
    print(f"spent ${budget.spent} of ${args.budget}")


if __name__ == "__main__":
    main()
