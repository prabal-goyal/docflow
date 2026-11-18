# 0006: Extraction model choice (M5)

Date: 2026-11-18. Status: accepted.

## Decision

Keep **`gpt-6-luna` at medium reasoning effort** as the default extraction model.

## Evidence

From the baseline evaluation ([evals/results.md](../../evals/results.md)), 30 synthetic invoices,
4 configs, 2 repeats, 240 calls:

- On the held-out test split, all four configs reached 100% field accuracy and 100% recall of
  planted problems, so quality did not separate them there.
- On the dev split, luna-low missed one planted problem (it read a letter O in a scanned HS code as
  a zero, hiding the error). luna-medium, terra-medium and sol-medium caught every planted problem
  on both splits.
- Cost per invoice: luna-low $0.00065, **luna-medium $0.00070**, terra-medium $0.0047 (7x),
  sol-medium $0.011 (16x). Latency was similar for all (p50 9-10 s).

## Departure from the pre-registered rule

The rule fixed before the run was: *the cheapest config that matches the best problem recall and
is within 2 points of the best field accuracy on the test split.* Applied literally, it selects
**luna-low**.

We chose luna-medium instead, and record why:

1. The dev split showed luna-low missing a planted problem, which is the metric the plan named as
   most important (a missed problem is an invalid invoice reaching review unflagged).
2. The saving is $0.00005 per invoice, about $5 per 100,000 invoices.
3. The rule's flaw was evaluating on the test split alone: 10 invoices, all configs at the
   ceiling, so it could not see the difference the dev split showed.

**Lesson for the next evaluation:** pre-register a rule that uses both splits (or a larger,
harder test set), and treat any missed planted problem as disqualifying regardless of split.

## Revisit when

- A harder dataset (photographed, skewed, stamped, low-resolution invoices) is available. The only
  reading errors in this run were on scans, so model differences are most likely to appear there.
- Real customer documents show error patterns the synthetic set lacks.
- Prices change: the model is a setting (`DOCFLOW_LLM_MODEL`), so switching needs no code change.
