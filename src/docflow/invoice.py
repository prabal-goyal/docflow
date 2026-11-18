"""The shape of an extracted invoice. Also the JSON schema the model must follow, so every field
is required but nullable (OpenAI structured outputs have no optional fields)."""

from pydantic import BaseModel, Field


class ExtractedField(BaseModel):
    value: str | None = Field(
        description="Normalised value, or null if the invoice does not show it. Formats: "
        "dates YYYY-MM-DD; amounts and quantities as plain decimals with a dot and no "
        "thousands separators (1234.50); currency as an ISO 4217 code; HS codes as digits only."
    )
    source_quote: str | None = Field(
        description="The exact text on the invoice this value was read from, copied character "
        "for character. Null only if value is null."
    )


class LineItem(BaseModel):
    description: ExtractedField
    hs_code: ExtractedField
    quantity: ExtractedField
    unit_price: ExtractedField
    amount: ExtractedField


class InvoiceExtraction(BaseModel):
    invoice_number: ExtractedField
    invoice_date: ExtractedField
    supplier_name: ExtractedField
    supplier_country: ExtractedField
    buyer_name: ExtractedField
    buyer_country: ExtractedField
    currency: ExtractedField
    line_items: list[LineItem]
    subtotal: ExtractedField
    tax_total: ExtractedField
    total: ExtractedField
