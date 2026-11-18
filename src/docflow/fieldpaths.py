"""Addresses fields in a stored extraction with paths such as "total" or "line_items[1].amount".
Pure functions on the JSON dict; paths are validated against the InvoiceExtraction schema."""

import copy
import re
from dataclasses import dataclass
from typing import Any

from docflow.invoice import InvoiceExtraction, LineItem

LINE_ITEMS = "line_items"
LINE_ITEM_PATH = re.compile(r"line_items\[(\d+)\]\.(\w+)")
TOP_LEVEL_FIELDS = frozenset(InvoiceExtraction.model_fields) - {LINE_ITEMS}
LINE_ITEM_FIELDS = frozenset(LineItem.model_fields)


class InvalidFieldPathError(ValueError):
    """The path does not name a field of this submission (HTTP 422)."""


@dataclass(frozen=True)
class FieldPath:
    name: str
    line_index: int | None = None  # set for line item fields

    def __str__(self) -> str:
        if self.line_index is None:
            return self.name
        return f"{LINE_ITEMS}[{self.line_index}].{self.name}"


def parse(path: str, fields: dict[str, Any]) -> FieldPath:
    if path in TOP_LEVEL_FIELDS:
        return FieldPath(path)
    match = LINE_ITEM_PATH.fullmatch(path)
    if match is None or match.group(2) not in LINE_ITEM_FIELDS:
        raise InvalidFieldPathError(f"Unknown field {path!r}")
    index = int(match.group(1))
    if index >= len(fields.get(LINE_ITEMS) or []):
        raise InvalidFieldPathError(f"There is no line item {index}")
    return FieldPath(match.group(2), index)


def _field(fields: dict[str, Any], path: FieldPath) -> dict[str, Any]:
    container = fields if path.line_index is None else fields[LINE_ITEMS][path.line_index]
    value: dict[str, Any] = container[path.name]
    return value


def get_value(fields: dict[str, Any], path: FieldPath) -> str | None:
    value: str | None = _field(fields, path)["value"]
    return value


def with_value(fields: dict[str, Any], path: FieldPath, value: str | None) -> dict[str, Any]:
    """Returns a copy with the value replaced. The source quote is cleared: a person entered
    this value, so it was not read from the document."""
    updated = copy.deepcopy(fields)
    target = _field(updated, path)
    target["value"] = value
    target["source_quote"] = None
    return updated
