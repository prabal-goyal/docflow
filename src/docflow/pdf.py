from dataclasses import dataclass
from io import BytesIO

from pypdf import PdfReader

PDF_MAGIC = b"%PDF-"


class InvalidPdfError(Exception):
    """The file cannot be processed. Retrying will not help."""


@dataclass(frozen=True)
class PdfInfo:
    page_count: int


def looks_like_pdf(data: bytes) -> bool:
    """Cheap check on the file signature. Does not parse the file."""
    return data.startswith(PDF_MAGIC)


def inspect_pdf(data: bytes, max_pages: int) -> PdfInfo:
    """Parses an untrusted PDF. Only call this in the worker, never in the API process."""
    try:
        reader = PdfReader(BytesIO(data))
        if reader.is_encrypted:
            raise InvalidPdfError("The PDF is password-protected")
        page_count = len(reader.pages)
    except InvalidPdfError:
        raise
    except Exception as error:
        # Malformed PDFs make pypdf raise many unrelated exception types (PdfReadError,
        # ValueError, KeyError, ...). For an untrusted upload they all mean "unreadable".
        raise InvalidPdfError("The PDF could not be read") from error

    if page_count == 0:
        raise InvalidPdfError("The PDF has no pages")
    if page_count > max_pages:
        raise InvalidPdfError(f"The PDF has {page_count} pages; the limit is {max_pages}")
    return PdfInfo(page_count=page_count)
