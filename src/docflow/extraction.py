from dataclasses import dataclass
from typing import Any, Protocol


class PermanentExtractionError(Exception):
    """Extraction failed and retrying will not help (bad input, refusal, misconfiguration)."""


class RetryableExtractionError(Exception):
    """Extraction failed for a reason that may go away (rate limit, outage, timeout)."""


@dataclass(frozen=True)
class ExtractionResult:
    fields: dict[str, Any]


class Extractor(Protocol):
    def extract(self, pdf: bytes) -> ExtractionResult: ...


def get_extractor() -> Extractor:
    """Builds the configured extractor. The OpenAI extractor arrives in M2b."""
    raise PermanentExtractionError("No extraction provider is configured")
