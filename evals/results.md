# Extraction evaluation: results and analysis

Run `baseline`, 2026-11-18. Full tables: [runs/baseline/results.md](runs/baseline/results.md).
Raw model outputs are committed under `runs/baseline/`; `python -m evals.score evals/runs/baseline`
re-derives every number without API calls, and CI checks that it does.

## Setup

- **Dataset:** 30 synthetic commercial invoices ([dataset.py](dataset.py)), 20 dev / 10 held-out
  test. Three layouts, three number formats (`1,234.50`, `1.234,50`, `1 234,50`), four date
  formats, 1-8 line items, some two-page, 5 image-only scans. 13 carry a planted problem
  (line, subtotal or total arithmetic, malformed HS code, future date, missing buyer or invoice
  number), each with exactly one expected flag. A test proves that a perfect extraction raises
  exactly the expected flags against each PDF's real text layer.
- **Configs:** `gpt-6-luna` (medium and low reasoning effort), `gpt-6.1-terra`, `gpt-6.1-sol`
  (medium). Each run twice over all 30 invoices: 240 calls, **all succeeded, $1.01 in total**.
- **Scoring:** the production extractor and checks code; numbers compared as `Decimal`, names
  ignoring case and spacing; missing line items count as wrong.

## Headline (held-out test split)

| Config | Field accuracy | Problem recall | Problem precision | $ / invoice | p50 / p95 latency |
|---|---|---|---|---|---|
| **luna-medium (chosen)** | 100% | 100% | 90.9% | $0.00070 | 10.1s / 11.9s |
| luna-low | 100% | 100% | 83.3% | $0.00065 | 9.6s / 11.4s |
| terra-medium | 100% | 100% | 100% | $0.0047 | 9.7s / 11.9s |
| sol-medium | 100% | 100% | 100% | $0.011 | 9.2s / 10.8s |

On the dev split, luna-low's problem recall was 95.5%; every other config was 100%. No config
raised a false flag on a clean invoice, and none invented a value on a clean text invoice.

## Every miss (5 of 240 extractions)

| Config | Invoice | What happened | Matters? |
|---|---|---|---|
| luna-low (x2), luna-medium (x1) | inv-01, test | Value right (`15632.30`) but the quote was rewritten from the printed `15 632,30` to `15 632.30`, so grounding flagged it | Reviewer noise; the check correctly caught a non-verbatim quote |
| luna-low (x1) | inv-09, dev, scanned | Read the printed `8484.90.1O` (letter O) as `8484.90.10`, so the malformed code looked valid and **the planted error went unflagged** | **Yes**: an invalid invoice reaches review unflagged |
| terra-medium (x1) | inv-18, dev, scanned | Misread HS code `90262040` as `90268040` | Yes, a wrong value (OCR-style digit error) |

All value-reading errors happened on scanned invoices.

## Decision

Keep **`gpt-6-luna` at medium effort** (decision record
[0006](../docs/decisions/0006-model-choice.md)). It matched the larger models on every measured
quality metric at 7x (Terra) and 16x (Sol) lower cost, and unlike luna-low it caught every planted
problem on both splits.

## Limitations

- **The dataset is easy.** Clean, computer-generated PDFs put every config at or near 100%, so
  the eval proves the pipeline is reliable on well-formed invoices but cannot rank models on real
  documents. The only reading errors were on scans: the next dataset should add photographed,
  skewed, stamped and low-resolution invoices.
- **Small test split.** 10 invoices cannot separate configs that are all near the ceiling.
- **No prompt changes.** The only repeated issue (rewritten numeric quotes) appeared on a test
  invoice. Tuning on it would contaminate the held-out set, so the prompt was left unchanged.
- **Synthetic only.** Real supplier invoices may contain layouts and wording this generator does
  not produce.
