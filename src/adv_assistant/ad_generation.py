from __future__ import annotations

import asyncio
import math
import uuid
from dataclasses import dataclass
from decimal import Decimal
from enum import StrEnum
from time import monotonic
from typing import Any, Protocol

import httpx


class AdGenerationError(RuntimeError):
    """Raised when generation submission or polling fails."""


class _RetryablePollError(AdGenerationError):
    """Internal marker for transient poll errors."""


class GenerationMode(StrEnum):
    FRESH = "fresh"
    REFERENCE = "reference"


class NanoBananaJobStatus(StrEnum):
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


@dataclass(slots=True)
class GenerationPollResult:
    status: NanoBananaJobStatus
    output_image_url: str | None = None
    error_code: str | None = None
    error_message: str | None = None


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

    async def wait_for_completion(self, *, job_id: str) -> GenerationPollResult: ...

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

    async def wait_for_completion(self, *, job_id: str) -> GenerationPollResult:
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
        status_api_url_template: str | None = None,
        model: str = "nanobanana-2",
        generation_type: str = "TEXTTOIAMGE",
        num_images: int = 1,
        watermark: bool | None = None,
        timeout_seconds: float = 20.0,
        poll_initial_seconds: float = 2.0,
        poll_max_seconds: float = 10.0,
        poll_timeout_seconds: float = 900.0,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self._api_key = api_key
        self._api_url = _resolve_generation_api_url(api_url=api_url, base_url=base_url)
        self._status_api_url_template = _resolve_status_api_url_template(
            status_api_url_template=status_api_url_template,
            base_url=base_url,
        )
        self._model = model
        self._generation_type = generation_type
        self._num_images = max(1, num_images)
        self._watermark = watermark
        self._poll_initial_seconds = max(0.1, poll_initial_seconds)
        self._poll_max_seconds = max(self._poll_initial_seconds, poll_max_seconds)
        self._poll_timeout_seconds = max(1.0, poll_timeout_seconds)
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
            "prompt": build_generation_prompt(
                draft=draft,
                mode=mode,
                instruction_text=instruction_text,
            ),
            "type": self._generation_type,
            "numImages": self._num_images,
            "watermark": self._watermark,
            "model": self._model,
            "aspect_ratio": aspect_ratio,
            "metadata": {
                "draft_id": str(draft.draft_id),
                "operator_phone": draft.operator_phone,
                "idempotency_key": idempotency_key,
                "size": {"width": width, "height": height},
            },
        }
        reference_image_url = draft.preview_reference_url or draft.rendered_image_url
        if mode == GenerationMode.REFERENCE and reference_image_url:
            payload["imageUrls"] = [reference_image_url]

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
        if int(response_payload.get("code", 0)) != 200:
            message = response_payload.get("msg")
            raise AdGenerationError(
                f"Nano Banana generation rejected: {message or 'Unknown error'}"
            )
        data = response_payload.get("data")
        if not isinstance(data, dict):
            raise AdGenerationError("Nano Banana response missing data object")
        job_id = data.get("taskId")
        if not isinstance(job_id, str) or not job_id.strip():
            raise AdGenerationError("Nano Banana response missing data.taskId")

        return GenerationSubmission(
            job_id=job_id.strip(),
            idempotency_key=idempotency_key,
            mode=mode,
            request_payload=payload,
        )

    async def wait_for_completion(self, *, job_id: str) -> GenerationPollResult:
        interval = self._poll_initial_seconds
        deadline = monotonic() + self._poll_timeout_seconds

        while monotonic() <= deadline:
            try:
                result = await self._fetch_job_status(job_id=job_id)
            except _RetryablePollError:
                await asyncio.sleep(interval)
                interval = min(interval * 2, self._poll_max_seconds)
                continue

            if result.status in {NanoBananaJobStatus.COMPLETED, NanoBananaJobStatus.FAILED}:
                return result

            await asyncio.sleep(interval)
            interval = min(interval * 2, self._poll_max_seconds)

        raise AdGenerationError(
            f"Nano Banana polling timed out for job_id={job_id} after "
            f"{self._poll_timeout_seconds:.0f} seconds"
        )

    async def _fetch_job_status(self, *, job_id: str) -> GenerationPollResult:
        try:
            response = await self._client.get(
                self._status_api_url_template.format(job_id=job_id),
                headers={"Authorization": f"Bearer {self._api_key}"},
            )
            response.raise_for_status()
        except httpx.HTTPStatusError as exc:
            status_code = exc.response.status_code if exc.response is not None else None
            if status_code is not None and (status_code == 429 or status_code >= 500):
                raise _RetryablePollError(f"Retryable poll HTTP status: {status_code}") from exc
            raise AdGenerationError(f"Nano Banana polling failed: {exc}") from exc
        except httpx.RequestError as exc:
            raise _RetryablePollError(f"Retryable poll request error: {exc}") from exc

        try:
            payload = response.json()
        except ValueError as exc:
            raise AdGenerationError("Nano Banana poll response is not valid JSON") from exc
        if not isinstance(payload, dict):
            raise AdGenerationError("Nano Banana poll response has unexpected format")

        success_flag = payload.get("successFlag")
        if not isinstance(success_flag, int):
            raise AdGenerationError("Nano Banana poll response missing successFlag")

        if success_flag == 0:
            return GenerationPollResult(status=NanoBananaJobStatus.RUNNING)
        if success_flag == 1:
            response_data = payload.get("response")
            output_image_url: str | None = None
            if isinstance(response_data, dict):
                result_image = response_data.get("resultImageUrl")
                if isinstance(result_image, str):
                    output_image_url = result_image
            return GenerationPollResult(
                status=NanoBananaJobStatus.COMPLETED,
                output_image_url=output_image_url,
            )
        if success_flag in {2, 3}:
            error_message = payload.get("errorMessage")
            return GenerationPollResult(
                status=NanoBananaJobStatus.FAILED,
                error_code=str(success_flag),
                error_message=error_message if isinstance(error_message, str) else None,
            )
        raise AdGenerationError(f"Unsupported Nano Banana successFlag: {success_flag}")

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


def _resolve_generation_api_url(*, api_url: str | None, base_url: str | None) -> str:
    if api_url and api_url.strip():
        return api_url.strip()
    if base_url and base_url.strip():
        return f"{base_url.rstrip('/')}/generate"
    raise ValueError("Either api_url or base_url is required")


def _resolve_status_api_url_template(
    *,
    status_api_url_template: str | None,
    base_url: str | None,
) -> str:
    if status_api_url_template and status_api_url_template.strip():
        if "{job_id}" not in status_api_url_template:
            raise ValueError("status_api_url_template must include '{job_id}' placeholder")
        return status_api_url_template.strip()
    if base_url and base_url.strip():
        return f"{base_url.rstrip('/')}/record-info?taskId={{job_id}}"
    raise ValueError("status_api_url_template is required when base_url is not provided")


def _price_to_text(price: Decimal | None, currency: str) -> str:
    if price is None:
        return "not provided"
    return f"{price} {currency}"
