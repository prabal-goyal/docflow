"""Webhook secret encryption with a fake KMS (no AWS calls)."""

from contextlib import nullcontext
from typing import Any

import httpx2
import pytest
from botocore.exceptions import EndpointConnectionError
from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.orm import Session

from docflow.config import Settings, get_settings
from docflow.dispatch import dispatch_due
from docflow.models import DeliveryStatus, WebhookDelivery, WebhookEndpoint
from docflow.secret_cipher import (
    ENCRYPTION_CONTEXT,
    KmsCipher,
    PlainCipher,
    SecretDecryptionError,
)
from docflow.webhooks import WebhookEventType, emit, verify

PUBLIC_DNS = ["93.184.216.34"]


class FakeKms:
    """Reversible stand-in for KMS that enforces the encryption context, as KMS does."""

    def __init__(self, fail: bool = False) -> None:
        self.fail = fail

    @staticmethod
    def _context(kwargs: dict[str, Any]) -> bytes:
        return repr(sorted(kwargs["EncryptionContext"].items())).encode()

    def encrypt(self, **kwargs: Any) -> dict[str, bytes]:
        return {"CiphertextBlob": self._context(kwargs) + b"|" + kwargs["Plaintext"][::-1]}

    def decrypt(self, **kwargs: Any) -> dict[str, bytes]:
        if self.fail:
            raise EndpointConnectionError(endpoint_url="https://kms.ap-south-1.amazonaws.com")
        context, _, data = kwargs["CiphertextBlob"].partition(b"|")
        if context != self._context(kwargs):
            raise ValueError("InvalidCiphertextException: wrong encryption context")
        return {"Plaintext": data[::-1]}


def test_kms_round_trip_hides_the_secret() -> None:
    cipher = KmsCipher(FakeKms(), "alias/docflow")

    stored = cipher.encrypt("whsec_abc")

    assert stored.startswith("kms:v1:")
    assert "whsec_abc" not in stored
    assert cipher.decrypt(stored) == "whsec_abc"


def test_ciphertext_is_bound_to_its_purpose() -> None:
    kms = FakeKms()
    blob = kms.encrypt(Plaintext=b"whsec_abc", EncryptionContext={"purpose": "something-else"})

    with pytest.raises(ValueError, match="encryption context"):
        kms.decrypt(CiphertextBlob=blob["CiphertextBlob"], EncryptionContext=ENCRYPTION_CONTEXT)


@pytest.mark.parametrize("stored", ["whsec_legacy", "plain:whsec_legacy"])
def test_unencrypted_secrets_written_before_kms_still_work(stored: str) -> None:
    assert KmsCipher(FakeKms(), "k").decrypt(stored) == "whsec_legacy"
    assert PlainCipher().decrypt(stored) == "whsec_legacy"


def test_kms_secret_without_a_key_is_an_error() -> None:
    with pytest.raises(SecretDecryptionError):
        PlainCipher().decrypt("kms:v1:AAAA")


def test_production_refuses_to_start_without_a_kms_key() -> None:
    base = get_settings().model_dump()
    with pytest.raises(ValidationError, match="KMS_KEY_ID"):
        Settings(**{**base, "environment": "production", "webhook_secret_kms_key_id": None})


def test_production_refuses_private_webhook_targets() -> None:
    base = get_settings().model_dump()
    with pytest.raises(ValidationError, match="private_targets"):
        Settings(
            **{
                **base,
                "environment": "production",
                "webhook_secret_kms_key_id": "alias/docflow",
                "webhook_allow_private_targets": True,
            }
        )


def queue_with_encrypted_secret(db: Session, cipher: KmsCipher) -> WebhookDelivery:
    endpoint = WebhookEndpoint(
        url="https://hooks.customer.example/x",
        secret=cipher.encrypt("whsec_real"),
        event_types=[WebhookEventType.SUBMISSION_APPROVED.value],
    )
    db.add(endpoint)
    db.commit()
    emit(db, WebhookEventType.SUBMISSION_APPROVED, {"submission_id": "s"})
    db.commit()
    query = select(WebhookDelivery).where(WebhookDelivery.endpoint_id == endpoint.id)
    return db.scalars(query).one()


def test_dispatcher_signs_with_the_decrypted_secret(db: Session) -> None:
    cipher = KmsCipher(FakeKms(), "alias/docflow")
    queue_with_encrypted_secret(db, cipher)
    requests: list[httpx2.Request] = []

    def handler(request: httpx2.Request) -> httpx2.Response:
        requests.append(request)
        return httpx2.Response(204)

    client = httpx2.Client(transport=httpx2.MockTransport(handler))
    dispatch_due(lambda: nullcontext(db), client, get_settings(), cipher, lambda h: PUBLIC_DNS)

    [request] = requests
    assert verify(
        "whsec_real",
        request.headers["DocFlow-Timestamp"],
        request.headers["DocFlow-Signature"],
        request.content,
        now=int(request.headers["DocFlow-Timestamp"]),
    )


def test_kms_outage_is_retried_not_failed(db: Session) -> None:
    delivery = queue_with_encrypted_secret(db, KmsCipher(FakeKms(), "alias/docflow"))
    broken = KmsCipher(FakeKms(fail=True), "alias/docflow")
    client = httpx2.Client(transport=httpx2.MockTransport(lambda r: httpx2.Response(204)))

    dispatch_due(lambda: nullcontext(db), client, get_settings(), broken, lambda h: PUBLIC_DNS)

    db.refresh(delivery)
    assert delivery.status == DeliveryStatus.PENDING
    assert "KMS error" in (delivery.last_error or "")
