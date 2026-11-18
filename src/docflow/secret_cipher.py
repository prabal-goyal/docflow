"""Encryption at rest for webhook signing secrets. Signing needs the plain value, so the secrets
cannot be hashed; in production they are encrypted with an AWS KMS key the database never sees.

Stored formats:
    kms:v1:<base64 ciphertext>   encrypted with KMS (production)
    plain:<secret>               local development only
    <secret>                     legacy rows from before encryption; still readable
"""

import base64
from functools import lru_cache
from typing import Any, Protocol

import boto3

from docflow.config import get_settings

KMS_PREFIX = "kms:v1:"
PLAIN_PREFIX = "plain:"
# Bound into every ciphertext: KMS refuses to decrypt it under any other context, so a ciphertext
# copied to another use cannot be decrypted there.
ENCRYPTION_CONTEXT = {"purpose": "docflow-webhook-signing-secret"}


class SecretDecryptionError(Exception):
    pass


class SecretCipher(Protocol):
    def encrypt(self, plaintext: str) -> str: ...

    def decrypt(self, stored: str) -> str: ...


class KmsClient(Protocol):
    """The two KMS operations used, so tests can supply a fake."""

    def encrypt(self, **kwargs: Any) -> Any: ...

    def decrypt(self, **kwargs: Any) -> Any: ...


def decode_unencrypted(stored: str) -> str:
    if stored.startswith(KMS_PREFIX):
        raise SecretDecryptionError("Secret is KMS-encrypted but no KMS key is configured")
    return stored.removeprefix(PLAIN_PREFIX)


class PlainCipher:
    """Development only: the settings refuse to start production without a KMS key."""

    def encrypt(self, plaintext: str) -> str:
        return PLAIN_PREFIX + plaintext

    def decrypt(self, stored: str) -> str:
        return decode_unencrypted(stored)


class KmsCipher:
    def __init__(self, client: KmsClient, key_id: str) -> None:
        self._kms = client
        self._key_id = key_id

    def encrypt(self, plaintext: str) -> str:
        response = self._kms.encrypt(
            KeyId=self._key_id,
            Plaintext=plaintext.encode(),
            EncryptionContext=ENCRYPTION_CONTEXT,
        )
        return KMS_PREFIX + base64.b64encode(response["CiphertextBlob"]).decode("ascii")

    def decrypt(self, stored: str) -> str:
        if not stored.startswith(KMS_PREFIX):
            return decode_unencrypted(stored)  # rows written before encryption was enabled
        response = self._kms.decrypt(
            KeyId=self._key_id,
            CiphertextBlob=base64.b64decode(stored.removeprefix(KMS_PREFIX)),
            EncryptionContext=ENCRYPTION_CONTEXT,
        )
        plaintext: bytes = response["Plaintext"]
        return plaintext.decode()


@lru_cache
def get_cipher() -> SecretCipher:
    """KMS when a key is configured, otherwise plain (development). Also a FastAPI dependency."""
    settings = get_settings()
    if settings.webhook_secret_kms_key_id:
        client = boto3.client("kms", region_name=settings.s3_region)
        return KmsCipher(client, settings.webhook_secret_kms_key_id)
    return PlainCipher()
