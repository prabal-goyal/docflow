# Evaluation results: run `baseline`

## Held-out test split (headline)

| Config | Field accuracy | All fields right | Problem recall | Problem precision | False flags / clean invoice | Ungrounded (clean, text) | Failures | $ / invoice | p50 latency | p95 latency | Accuracy per repeat |
|---|---|---|---|---|---|---|---|---|---|---|---|
| luna-low | 100.0% | 100.0% | 100.0% | 83.3% | 0.00 | 0 | 0 | $0.000651 | 9.6s | 11.4s | 100.0%, 100.0% |
| luna-medium | 100.0% | 100.0% | 100.0% | 90.9% | 0.00 | 0 | 0 | $0.000697 | 10.1s | 11.9s | 100.0%, 100.0% |
| sol-medium | 100.0% | 100.0% | 100.0% | 100.0% | 0.00 | 0 | 0 | $0.011167 | 9.2s | 10.8s | 100.0%, 100.0% |
| terra-medium | 100.0% | 100.0% | 100.0% | 100.0% | 0.00 | 0 | 0 | $0.004742 | 9.7s | 11.9s | 100.0%, 100.0% |

## Dev split

| Config | Field accuracy | All fields right | Problem recall | Problem precision | False flags / clean invoice | Ungrounded (clean, text) | Failures | $ / invoice | p50 latency | p95 latency | Accuracy per repeat |
|---|---|---|---|---|---|---|---|---|---|---|---|
| luna-low | 100.0% | 100.0% | 95.5% | 100.0% | 0.00 | 0 | 0 | $0.000600 | 9.5s | 11.7s | 100.0%, 100.0% |
| luna-medium | 100.0% | 100.0% | 100.0% | 100.0% | 0.00 | 0 | 0 | $0.000670 | 9.7s | 14.1s | 100.0%, 100.0% |
| sol-medium | 100.0% | 100.0% | 100.0% | 100.0% | 0.00 | 0 | 0 | $0.010792 | 9.3s | 12.3s | 100.0%, 100.0% |
| terra-medium | 99.9% | 97.5% | 100.0% | 100.0% | 0.00 | 0 | 0 | $0.004541 | 9.1s | 11.2s | 100.0%, 99.8% |

## Field accuracy by field (all invoices)

| Field | luna-low | luna-medium | sol-medium | terra-medium |
|---|---|---|---|---|
| amount | 100.0% | 100.0% | 100.0% | 100.0% |
| buyer_country | 100.0% | 100.0% | 100.0% | 100.0% |
| buyer_name | 100.0% | 100.0% | 100.0% | 100.0% |
| currency | 100.0% | 100.0% | 100.0% | 100.0% |
| description | 100.0% | 100.0% | 100.0% | 100.0% |
| hs_code | 100.0% | 100.0% | 100.0% | 99.5% |
| invoice_date | 100.0% | 100.0% | 100.0% | 100.0% |
| invoice_number | 100.0% | 100.0% | 100.0% | 100.0% |
| quantity | 100.0% | 100.0% | 100.0% | 100.0% |
| subtotal | 100.0% | 100.0% | 100.0% | 100.0% |
| supplier_country | 100.0% | 100.0% | 100.0% | 100.0% |
| supplier_name | 100.0% | 100.0% | 100.0% | 100.0% |
| tax_total | 100.0% | 100.0% | 100.0% | 100.0% |
| total | 100.0% | 100.0% | 100.0% | 100.0% |
| unit_price | 100.0% | 100.0% | 100.0% | 100.0% |

## Flags by type (all invoices): recall / precision

| Flag | luna-low | luna-medium | sol-medium | terra-medium |
|---|---|---|---|---|
| arithmetic_mismatch | 100.0% / 100.0% | 100.0% / 100.0% | 100.0% / 100.0% | 100.0% / 100.0% |
| grounding_unverifiable | 100.0% / 100.0% | 100.0% / 100.0% | 100.0% / 100.0% | 100.0% / 100.0% |
| invalid_format | 87.5% / 100.0% | 100.0% / 100.0% | 100.0% / 100.0% | 100.0% / 100.0% |
| missing_required | 100.0% / 100.0% | 100.0% / 100.0% | 100.0% / 100.0% | 100.0% / 100.0% |
| not_grounded | n/a / 0.0% | n/a / 0.0% | - | - |

## Total cost

- luna-low: $0.0370225
- luna-medium: $0.0407440
- sol-medium: $0.655040
- terra-medium: $0.276475
