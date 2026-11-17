from functools import lru_cache
from typing import TYPE_CHECKING, Literal, Protocol

import boto3
from botocore.config import Config
from botocore.exceptions import BotoCoreError, ClientError

from docflow.config import get_settings

if TYPE_CHECKING:
    from mypy_boto3_s3 import S3Client


class StorageError(Exception):
    """The object store could not be reached or refused the request. Usually transient."""


class ObjectStorage(Protocol):
    def put(self, key: str, data: bytes, content_type: str) -> None: ...

    def get(self, key: str) -> bytes: ...


class S3Storage:
    def __init__(self, client: "S3Client", bucket: str) -> None:
        self._client = client
        self._bucket = bucket

    def put(self, key: str, data: bytes, content_type: str) -> None:
        try:
            self._client.put_object(
                Bucket=self._bucket, Key=key, Body=data, ContentType=content_type
            )
        except (BotoCoreError, ClientError) as error:
            raise StorageError(f"Could not store {key}") from error

    def get(self, key: str) -> bytes:
        try:
            return self._client.get_object(Bucket=self._bucket, Key=key)["Body"].read()
        except (BotoCoreError, ClientError) as error:
            raise StorageError(f"Could not read {key}") from error

    def ensure_bucket(self) -> bool:
        """Creates the bucket if it is missing. Returns True if it was created."""
        try:
            self._client.head_bucket(Bucket=self._bucket)
            return False
        except ClientError as error:
            if error.response.get("Error", {}).get("Code") not in ("404", "NoSuchBucket"):
                raise
        self._client.create_bucket(Bucket=self._bucket)
        return True


@lru_cache
def get_s3_storage() -> S3Storage:
    settings = get_settings()
    # Local S3-compatible servers need path-style URLs (http://host/bucket/key);
    # real AWS S3 uses the default virtual-hosted style.
    addressing: Literal["path", "auto"] = "path" if settings.s3_endpoint_url else "auto"
    client = boto3.client(
        "s3",
        endpoint_url=settings.s3_endpoint_url,
        region_name=settings.s3_region,
        config=Config(retries={"mode": "standard"}, s3={"addressing_style": addressing}),
    )
    return S3Storage(client, settings.s3_bucket)


def get_storage() -> ObjectStorage:
    """FastAPI dependency, overridden with an in-memory store in tests."""
    return get_s3_storage()
