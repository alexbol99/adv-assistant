from __future__ import annotations

import asyncio
import uuid
from dataclasses import dataclass
from typing import Any, Protocol
from urllib.parse import quote

import boto3
from google.cloud import storage


class MediaStoreError(RuntimeError):
    """Raised when media storage operations fail."""


@dataclass(slots=True)
class MediaUpload:
    object_name: str
    public_url: str


class MediaStore(Protocol):
    async def upload_bytes(
        self,
        *,
        content: bytes,
        content_type: str,
        object_name: str | None = None,
        suffix: str | None = None,
    ) -> MediaUpload: ...

    async def has_delete_lifecycle_rule(self, *, days: int) -> bool: ...

    async def close(self) -> None: ...


class NoopMediaStore:
    async def upload_bytes(
        self,
        *,
        content: bytes,
        content_type: str,
        object_name: str | None = None,
        suffix: str | None = None,
    ) -> MediaUpload:
        raise MediaStoreError("Media store is not configured")

    async def has_delete_lifecycle_rule(self, *, days: int) -> bool:
        return False

    async def close(self) -> None:
        return None


class GCSMediaStore:
    def __init__(
        self,
        *,
        bucket_name: str,
        project_id: str | None = None,
        public_base_url: str = "https://storage.googleapis.com",
        object_prefix: str = "operator-photos",
        client: storage.Client | None = None,
    ) -> None:
        if not bucket_name.strip():
            raise ValueError("bucket_name is required")

        self._bucket_name = bucket_name.strip()
        self._project_id = project_id
        self._public_base_url = public_base_url.rstrip("/")
        self._object_prefix = object_prefix.strip().strip("/")
        self._client = client or storage.Client(project=project_id)

    async def upload_bytes(
        self,
        *,
        content: bytes,
        content_type: str,
        object_name: str | None = None,
        suffix: str | None = None,
    ) -> MediaUpload:
        resolved_object_name = object_name or self._build_object_name(suffix=suffix)
        await asyncio.to_thread(
            self._upload_sync,
            resolved_object_name,
            content,
            content_type,
        )
        return MediaUpload(
            object_name=resolved_object_name,
            public_url=self._build_public_url(resolved_object_name),
        )

    async def has_delete_lifecycle_rule(self, *, days: int) -> bool:
        return await asyncio.to_thread(self._has_delete_lifecycle_rule_sync, days)

    async def close(self) -> None:
        return None

    def _build_object_name(self, *, suffix: str | None) -> str:
        extension = (suffix or "").strip().lower()
        generated = f"{uuid.uuid4()}{extension}"
        if self._object_prefix:
            return f"{self._object_prefix}/{generated}"
        return generated

    def _build_public_url(self, object_name: str) -> str:
        escaped = quote(object_name, safe="/")
        return f"{self._public_base_url}/{self._bucket_name}/{escaped}"

    def _upload_sync(self, object_name: str, content: bytes, content_type: str) -> None:
        bucket = self._client.bucket(self._bucket_name)
        blob = bucket.blob(object_name)
        blob.upload_from_string(content, content_type=content_type)

    def _has_delete_lifecycle_rule_sync(self, days: int) -> bool:
        bucket = self._client.bucket(self._bucket_name)
        bucket.reload()
        rules = bucket.lifecycle_rules or []
        for rule in rules:
            action = (rule.get("action", {}) or {}).get("type")
            condition = rule.get("condition", {}) or {}
            age = condition.get("age")
            if str(action).lower() == "delete" and age == days:
                return True
        return False


class S3MediaStore:
    def __init__(
        self,
        *,
        bucket_name: str,
        region_name: str | None = None,
        public_base_url: str | None = None,
        object_prefix: str = "operator-photos",
        endpoint_url: str | None = None,
        client: Any | None = None,
    ) -> None:
        if not bucket_name.strip():
            raise ValueError("bucket_name is required")

        self._bucket_name = bucket_name.strip()
        self._region_name = (region_name or "").strip() or None
        self._public_prefix = self._resolve_public_prefix(
            bucket_name=self._bucket_name,
            region_name=self._region_name,
            public_base_url=public_base_url,
        )
        self._object_prefix = object_prefix.strip().strip("/")
        self._client = client or boto3.client(
            "s3",
            region_name=self._region_name,
            endpoint_url=(endpoint_url or "").strip() or None,
        )

    async def upload_bytes(
        self,
        *,
        content: bytes,
        content_type: str,
        object_name: str | None = None,
        suffix: str | None = None,
    ) -> MediaUpload:
        resolved_object_name = object_name or self._build_object_name(suffix=suffix)
        await asyncio.to_thread(
            self._upload_sync,
            resolved_object_name,
            content,
            content_type,
        )
        return MediaUpload(
            object_name=resolved_object_name,
            public_url=self._build_public_url(resolved_object_name),
        )

    async def has_delete_lifecycle_rule(self, *, days: int) -> bool:
        return await asyncio.to_thread(self._has_delete_lifecycle_rule_sync, days)

    async def close(self) -> None:
        return None

    def _build_object_name(self, *, suffix: str | None) -> str:
        extension = (suffix or "").strip().lower()
        generated = f"{uuid.uuid4()}{extension}"
        if self._object_prefix:
            return f"{self._object_prefix}/{generated}"
        return generated

    def _build_public_url(self, object_name: str) -> str:
        escaped = quote(object_name, safe="/")
        return f"{self._public_prefix}/{escaped}"

    @staticmethod
    def _resolve_public_prefix(
        *,
        bucket_name: str,
        region_name: str | None,
        public_base_url: str | None,
    ) -> str:
        explicit = (public_base_url or "").strip()
        if explicit:
            return explicit.rstrip("/")
        if region_name and region_name != "us-east-1":
            return f"https://{bucket_name}.s3.{region_name}.amazonaws.com"
        return f"https://{bucket_name}.s3.amazonaws.com"

    def _upload_sync(self, object_name: str, content: bytes, content_type: str) -> None:
        self._client.put_object(
            Bucket=self._bucket_name,
            Key=object_name,
            Body=content,
            ContentType=content_type,
        )

    def _has_delete_lifecycle_rule_sync(self, days: int) -> bool:
        try:
            response = self._client.get_bucket_lifecycle_configuration(Bucket=self._bucket_name)
        except Exception as exc:  # pragma: no cover - provider-specific error surface
            error_code = _extract_aws_error_code(exc)
            if error_code == "NoSuchLifecycleConfiguration":
                return False
            raise MediaStoreError(
                f"Failed to fetch S3 lifecycle rules for bucket '{self._bucket_name}'"
            ) from exc

        rules = response.get("Rules", []) if isinstance(response, dict) else []
        for rule in rules:
            if not isinstance(rule, dict):
                continue
            status = str(rule.get("Status", "")).strip().lower()
            expiration = rule.get("Expiration")
            expiration_days = expiration.get("Days") if isinstance(expiration, dict) else None
            if status == "enabled" and expiration_days == days:
                return True
        return False


def _extract_aws_error_code(exc: Exception) -> str | None:
    response = getattr(exc, "response", None)
    if not isinstance(response, dict):
        return None
    error = response.get("Error")
    if not isinstance(error, dict):
        return None
    code = error.get("Code")
    if code is None:
        return None
    return str(code)
