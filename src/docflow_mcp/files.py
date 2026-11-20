"""Reading invoice files for upload, confined to one folder the user chose.

The model picks the file name, and the model may be following instructions hidden in a document
it read earlier. So a file is only read if, after resolving `..` and symlinks, it is still inside
the upload folder, and it really is a PDF within DocFlow's size limit.
"""

import hashlib
from dataclasses import dataclass
from pathlib import Path

# Matches DocFlow's default max_upload_bytes; DocFlow still enforces its own limit.
MAX_UPLOAD_BYTES = 10 * 1024 * 1024
PDF_MAGIC = b"%PDF-"


class FileRejected(Exception):
    """The file cannot be uploaded. The message is shown to the model."""


@dataclass(frozen=True)
class InvoiceFile:
    name: str
    data: bytes

    @property
    def sha256(self) -> str:
        return hashlib.sha256(self.data).hexdigest()


def list_pdfs(upload_dir: Path) -> list[str]:
    root = upload_dir.resolve()
    return sorted(
        str(p.relative_to(root))
        for p in root.rglob("*")
        if p.suffix.lower() == ".pdf" and p.is_file() and p.resolve().is_relative_to(root)
    )


def read_invoice(
    upload_dir: Path, file_name: str, max_bytes: int = MAX_UPLOAD_BYTES
) -> InvoiceFile:
    root = upload_dir.resolve()
    path = (root / file_name).resolve()
    if not path.is_relative_to(root):
        raise FileRejected(
            "Only files inside the upload folder can be uploaded. Use list_invoice_files."
        )
    if path.suffix.lower() != ".pdf":
        raise FileRejected("Only PDF files can be uploaded.")
    if not path.is_file():
        raise FileRejected(f"{file_name} was not found in the upload folder.")
    if path.stat().st_size > max_bytes:
        raise FileRejected(f"{file_name} is larger than {max_bytes // (1024 * 1024)} MB.")
    data = path.read_bytes()
    if not data.startswith(PDF_MAGIC):
        raise FileRejected(f"{file_name} is not a valid PDF.")
    return InvoiceFile(name=path.name, data=data)
