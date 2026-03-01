from __future__ import annotations

import hashlib
import hmac
import math
import uuid
from dataclasses import dataclass
from decimal import Decimal
from enum import StrEnum
from typing import Any, Protocol

import httpx
from pydantic import BaseModel, Field


class AdGenerationError(RuntimeError):
    """Raised when generation submission or callback handling fails."""


class GenerationMode(StrEnum):
    FRESH = "fresh"
    REFERENCE = "reference"


class NanoBananaCallbackStatus(StrEnum):
    QUEUED = "queued"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"


@dataclass(slots=True)
class GenerationDraftInput:
    draft_id: uuid.UUID
    operator_phone: str
    language: str
    product_name: str | None
    price: Decimal | None
    currency: str
    promo_text: str | None
    ean: str | None
    photo_url: str | None
    enriched_brand: str | None
    enriched_category: str | None
    enriched_description: str | None
    preview_reference_url: str | None
    rendered_image_url: str | None


@dataclass(slots=True)
class GenerationSubmission:
    job_id: str
    idempotency_key: str
    mode: GenerationMode
    request_payload: dict[str, Any]


class NanoBananaCallbackMetadata(BaseModel):
    draft_id: str
    operator_phone: str


class NanoBananaCallbackPayload(BaseModel):
    job_id: str = Field(min_length=1, max_length=256)
    status: NanoBananaCallbackStatus
    output_image_url: str | None = None
    error_code: str | None = None
    error_message: str | None = None
    metadata: NanoBananaCallbackMetadata


class AdGenerationService(Protocol):
    @property
    def enabled(self) -> bool: ...

    async def submit_for_draft(
        self,
        *,
        draft: GenerationDraftInput,
        mode: GenerationMode,
        instruction_text: str,
        wamid: str,
        width: int,
        height: int,
    ) -> GenerationSubmission: ...

    async def close(self) -> None: ...


class NoopAdGenerationService:
    @property
    def enabled(self) -> bool:
        return False

    async def submit_for_draft(
        self,
        *,
        draft: GenerationDraftInput,
        mode: GenerationMode,
        instruction_text: str,
        wamid: str,
        width: int,
        height: int,
    ) -> GenerationSubmission:
        raise AdGenerationError("Ad generation service is not configured")

    async def close(self) -> None:
        return None


class NanoBananaAdGenerationService:
    def __init__(
        self,
        *,
        api_key: str,
        api_url: str | None = None,
        base_url: str | None = None,
        callback_url: str,
        model: str = "nanobanana-2",
        timeout_seconds: float = 20.0,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self._api_key = api_key
        self._api_url = _resolve_generation_api_url(api_url=api_url, base_url=base_url)
        self._callback_url = callback_url
        self._model = model
        self._owns_client = client is None
        self._client = client or httpx.AsyncClient(timeout=timeout_seconds)

    @property
    def enabled(self) -> bool:
        return True

    async def submit_for_draft(
        self,
        *,
        draft: GenerationDraftInput,
        mode: GenerationMode,
        instruction_text: str,
        wamid: str,
        width: int,
        height: int,
    ) -> GenerationSubmission:
        if mode == GenerationMode.REFERENCE and not (
            draft.preview_reference_url or draft.rendered_image_url
        ):
            raise AdGenerationError("Reference regeneration requires an existing preview image")

        aspect_ratio = derive_aspect_ratio(width=width, height=height)
        idempotency_key = stable_idempotency_key(
            draft_id=draft.draft_id,
            wamid=wamid,
            mode=mode,
        )
        payload = {
            "model": self._model,
            "prompt": build_generation_prompt(
                draft=draft,
                mode=mode,
                instruction_text=instruction_text,
            ),
            "size": {"width": width, "height": height},
            "aspect_ratio": aspect_ratio,
            "callback_url": self._callback_url,
            "metadata": {
                "draft_id": str(draft.draft_id),
                "operator_phone": draft.operator_phone,
            },
            "idempotency_key": idempotency_key,
        }
        reference_image_url = draft.preview_reference_url or draft.rendered_image_url
        if mode == GenerationMode.REFERENCE and reference_image_url:
            payload["reference_image_url"] = reference_image_url

        try:
            response = await self._client.post(
                self._api_url,
                json=payload,
                headers={"Authorization": f"Bearer {self._api_key}"},
            )
            response.raise_for_status()
        except (httpx.RequestError, httpx.HTTPStatusError) as exc:
            raise AdGenerationError(f"Nano Banana submission failed: {exc}") from exc

        try:
            response_payload = response.json()
        except ValueError as exc:
            raise AdGenerationError("Nano Banana response is not valid JSON") from exc
        if not isinstance(response_payload, dict):
            raise AdGenerationError("Nano Banana response has unexpected format")
        job_id = response_payload.get("job_id")
        if not isinstance(job_id, str) or not job_id.strip():
            raise AdGenerationError("Nano Banana response missing job_id")

        return GenerationSubmission(
            job_id=job_id.strip(),
            idempotency_key=idempotency_key,
            mode=mode,
            request_payload=payload,
        )

    async def close(self) -> None:
        if self._owns_client:
            await self._client.aclose()


def stable_idempotency_key(
    *,
    draft_id: uuid.UUID,
    wamid: str,
    mode: GenerationMode,
) -> str:
    stable = f"{draft_id}:{wamid}:{mode.value}"
    return str(uuid.uuid5(uuid.NAMESPACE_URL, stable))


def derive_aspect_ratio(*, width: int, height: int) -> str:
    if width <= 0 or height <= 0:
        raise ValueError("width and height must be positive integers")
    divisor = math.gcd(width, height)
    return f"{width // divisor}:{height // divisor}"


def build_generation_prompt(
    *,
    draft: GenerationDraftInput,
    mode: GenerationMode,
    instruction_text: str,
) -> str:
    lines = [
        "Generate a retail ad image.",
        f"Language: {draft.language}.",
        f"Mode: {mode.value}.",
        f"Product: {draft.product_name or 'unknown'}.",
        f"Price: {_price_to_text(draft.price, draft.currency)}.",
        f"Promo: {draft.promo_text or 'none'}.",
    ]
    if draft.ean:
        lines.append(f"EAN: {draft.ean}.")
    if draft.photo_url:
        lines.append(f"Product photo URL: {draft.photo_url}.")
    if draft.enriched_brand:
        lines.append(f"Brand: {draft.enriched_brand}.")
    if draft.enriched_category:
        lines.append(f"Category: {draft.enriched_category}.")
    if draft.enriched_description:
        lines.append(f"Description: {draft.enriched_description}.")
    if mode == GenerationMode.REFERENCE and (
        draft.preview_reference_url or draft.rendered_image_url
    ):
        lines.append("Preserve the overall visual style of the previous preview.")

    cleaned_instruction = " ".join(instruction_text.split()).strip()
    if cleaned_instruction:
        lines.append(f"Operator instruction: {cleaned_instruction}.")
    return "\n".join(lines)


def verify_nana_banana_signature(
    *,
    callback_secret: str | None,
    payload_body: bytes,
    signature_header: str | None,
) -> bool:
    if not callback_secret or not signature_header:
        return False
    if not signature_header.startswith("sha256="):
        return False
    provided = signature_header[len("sha256=") :]
    expected = hmac.new(
        callback_secret.encode("utf-8"),
        payload_body,
        hashlib.sha256,
    ).hexdigest()
    return hmac.compare_digest(provided, expected)


def _price_to_text(price: Decimal | None, currency: str) -> str:
    if price is None:
        return "not provided"
    return f"{price} {currency}"


def _resolve_generation_api_url(*, api_url: str | None, base_url: str | None) -> str:
    if api_url and api_url.strip():
        return api_url.strip()
    if base_url and base_url.strip():
        return f"{base_url.rstrip('/')}/v1/generate"
    raise ValueError("Either api_url or base_url is required")
