from dataclasses import dataclass
from typing import Protocol

from docflow.invoice import InvoiceExtraction


@dataclass(frozen=True)
class Usage:
    """What one provider call cost us, recorded even when the call did not succeed."""

    model: str
    input_tokens: int | None = None
    output_tokens: int | None = None  # includes reasoning tokens
    reasoning_tokens: int | None = None
    response_id: str | None = None


class PermanentExtractionError(Exception):
    """Extraction failed and retrying will not help (bad input, refusal, misconfiguration)."""

    def __init__(self, message: str, usage: Usage | None = None) -> None:
        super().__init__(message)
        self.usage = usage


class RetryableExtractionError(Exception):
    """Extraction failed for a reason that may go away (rate limit, outage, timeout)."""


@dataclass(frozen=True)
class ExtractionResult:
    invoice: InvoiceExtraction
    usage: Usage | None = None


class Extractor(Protocol):
    def extract(self, pdf: bytes) -> ExtractionResult: ...
