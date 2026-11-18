# 0003: LLM extraction and deterministic checks (M2b)

Date: 2026-11-18. Status: accepted.

## Provider and model

- **OpenAI Responses API** with structured outputs (`responses.parse` + a Pydantic schema), so the
  output always matches `InvoiceExtraction`.
- **Model `gpt-6-luna`, reasoning effort `medium`**: OpenAI's most efficient model, positioned for
  focused, high-volume tasks, at $0.10 / $0.50 per 1M input/output tokens (pricing page,
  2026-11-17). Chosen for cost; the M5 eval compares it with `gpt-6.1-terra` and `gpt-6.1-sol`.
  The model is a setting (`DOCFLOW_LLM_MODEL`), not code.
- The provider sits behind the `Extractor` interface. A customer that requires another provider
  (for example Azure OpenAI in their own tenant) needs one new class, nothing else.

## Extraction design

- **Normalised value, verbatim quote.** Each field has `value` (dates `YYYY-MM-DD`, plain decimals,
  ISO currency, digits-only HS codes) and `source_quote` (exact text from the invoice). Format and
  arithmetic checks use the value; grounding uses the quote. Regional formats such as
  `1.234,50` therefore do not break the checks.
- **Copy, never correct.** The prompt tells the model to extract printed values even when they are
  wrong. Mistakes on the invoice are what the checks exist to find.
- **The model's confidence is not used.** Flags come only from deterministic checks: grounding,
  arithmetic (`Decimal`, 0.01 tolerance, subtotal + tax = total), formats, completeness. One bad
  value produces one flag, not a cascade.
- **Prompt injection**: the prompt treats document content as data, output is constrained to the
  schema, every submission goes to a human reviewer, and model output never triggers an action.

## Failures and cost

| Outcome | Handling |
|---|---|
| Rate limit, connection error, timeout, 5xx | Retryable (SDK retries twice, then Celery with backoff) |
| 429 with a billing code (`credit_balance_exhausted`, `*_spend_limit_exceeded`, `organization_usage_limit_exceeded`) or type `insufficient_quota` | Permanent, message points to billing (OpenAI: retrying will not restore access) |
| Other 4xx | Permanent |
| Refusal, or `status: incomplete` | Permanent; tokens are still logged because they are billed |
| `OPENAI_API_KEY` not set | Permanent with that reason (CI relies on this) |

Every call is stored in `extraction_attempts` with tokens (including reasoning tokens), an
estimated cost from the price table in settings (ignores cached-input discounts; `null` for an
unknown model), latency and the response ID.

## Data handling

- The PDF is sent to OpenAI. `store=False` is set so the response is not kept for later retrieval.
  OpenAI's current API data-retention terms and data-control options (including zero data
  retention eligibility) must be checked against the customer's requirements before handling real
  documents.
- Logs contain response IDs, token counts and errors, never document content or field values.
