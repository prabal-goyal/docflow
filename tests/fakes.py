"""Test doubles for the external systems: object storage, the job queue and the extractor."""

import uuid
from io import BytesIO

from pypdf import PdfWriter

from docflow.extraction import ExtractionResult, Usage
from docflow.invoice import InvoiceExtraction
from docflow.storage import StorageError
from tests.invoices import make_invoice

LETTER = (612, 792)


class InMemoryStorage:
    def __init__(self) -> None:
        self.objects: dict[str, bytes] = {}
        self.fail = False

    def put(self, key: str, data: bytes, content_type: str) -> None:
        if self.fail:
            raise StorageError("storage is down")
        self.objects[key] = data

    def get(self, key: str) -> bytes:
        if self.fail:
            raise StorageError("storage is down")
        return self.objects[key]


class RecordingQueue:
    def __init__(self) -> None:
        self.enqueued: list[uuid.UUID] = []
        self.fail = False

    def __call__(self, submission_id: uuid.UUID) -> None:
        if self.fail:
            raise ConnectionError("broker is down")
        self.enqueued.append(submission_id)


class FakeExtractor:
    def __init__(
        self,
        invoice: InvoiceExtraction | None = None,
        error: Exception | None = None,
        usage: Usage | None = None,
    ) -> None:
        self.invoice = invoice if invoice is not None else make_invoice()
        self.error = error
        self.usage = usage or Usage(
            model="gpt-6-luna", input_tokens=4000, output_tokens=1000, reasoning_tokens=600
        )
        self.calls = 0

    def extract(self, pdf: bytes) -> ExtractionResult:
        self.calls += 1
        if self.error is not None:
            raise self.error
        return ExtractionResult(invoice=self.invoice, usage=self.usage)


def make_pdf(pages: int = 1, password: str | None = None) -> bytes:
    writer = PdfWriter()
    for _ in range(pages):
        writer.add_blank_page(*LETTER)
    if password is not None:
        writer.encrypt(password)
    buffer = BytesIO()
    writer.write(buffer)
    return buffer.getvalue()


class InMemoryLoginLimiter:
    def __init__(self, max_failures: int = 10) -> None:
        self.max_failures = max_failures
        self.failures: dict[tuple[str, str], int] = {}

    def retry_after(self, email: str, ip: str) -> int | None:
        return 900 if self.failures.get((email, ip), 0) >= self.max_failures else None

    def record_failure(self, email: str, ip: str) -> None:
        self.failures[(email, ip)] = self.failures.get((email, ip), 0) + 1

    def reset(self, email: str, ip: str) -> None:
        self.failures.pop((email, ip), None)
