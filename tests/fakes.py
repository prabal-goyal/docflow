"""Test doubles for the external systems: object storage, the job queue and the extractor."""

import uuid
from io import BytesIO
from typing import Any

from pypdf import PdfWriter

from docflow.extraction import ExtractionResult
from docflow.storage import StorageError

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
        self, fields: dict[str, Any] | None = None, error: Exception | None = None
    ) -> None:
        self.fields = fields if fields is not None else {"invoice_number": {"value": "INV-1"}}
        self.error = error
        self.calls = 0

    def extract(self, pdf: bytes) -> ExtractionResult:
        self.calls += 1
        if self.error is not None:
            raise self.error
        return ExtractionResult(fields=self.fields)


def make_pdf(pages: int = 1, password: str | None = None) -> bytes:
    writer = PdfWriter()
    for _ in range(pages):
        writer.add_blank_page(*LETTER)
    if password is not None:
        writer.encrypt(password)
    buffer = BytesIO()
    writer.write(buffer)
    return buffer.getvalue()
