from __future__ import annotations

import asyncio
import base64
import logging
import math
import uuid
from collections.abc import Awaitable, Callable
from contextvars import ContextVar
from dataclasses import dataclass
from decimal import Decimal
from enum import StrEnum
from time import monotonic
from typing import Any, Protocol

import httpx

from adv_assistant.media_store import MediaStore, MediaStoreError

logger = logging.getLogger(__name__)

TraceSink = Callable[[str, str | None, dict[str, Any]], Awaitable[None]]


@dataclass(slots=True)
class _TraceContext:
    operator_phone: str
    wamid: str | None
    trace_sink: TraceSink | None


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
    product_brand: str | None = None
    business_name: str | None = None
    logo_url: str | None = None
    brand_colors: list[str] | None = None
    store_type: str | None = None
    creative_guidance: str | None = None


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

    def set_trace_context(
        self,
        *,
        operator_phone: str,
        wamid: str | None,
        trace_sink: TraceSink | None,
    ) -> None:
        return None

    def clear_trace_context(self) -> None:
        return None

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


class GeminiFlashImageAdGenerationService:
    def __init__(
        self,
        *,
        api_key: str,
        media_store: MediaStore,
        model: str = "gemini-3.1-flash-image-preview",
        base_url: str = "https://generativelanguage.googleapis.com/v1beta",
        timeout_seconds: float = 30.0,
        max_submit_attempts: int = 3,
        retry_base_seconds: float = 1.0,
        trace_enabled: bool = False,
        trace_max_chars: int = 4000,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self._api_key = api_key.strip()
        self._model = model.strip()
        self._generate_url = f"{base_url.rstrip('/')}/models/{self._model}:generateContent"
        self._media_store = media_store
        self._max_submit_attempts = max(1, max_submit_attempts)
        self._retry_base_seconds = max(0.0, retry_base_seconds)
        self._owns_client = client is None
        self._client = client or httpx.AsyncClient(timeout=timeout_seconds)
        self._results: dict[str, GenerationPollResult] = {}
        self._trace_enabled = trace_enabled
        self._trace_max_chars = max(200, trace_max_chars)
        self._trace_context: ContextVar[_TraceContext | None] = ContextVar(
            "gemini_trace_context",
            default=None,
        )

    @property
    def enabled(self) -> bool:
        return True

    def set_trace_context(
        self,
        *,
        operator_phone: str,
        wamid: str | None,
        trace_sink: TraceSink | None,
    ) -> None:
        self._trace_context.set(
            _TraceContext(
                operator_phone=operator_phone,
                wamid=wamid,
                trace_sink=trace_sink,
            )
        )

    def clear_trace_context(self) -> None:
        self._trace_context.set(None)

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

        idempotency_key = stable_idempotency_key(
            draft_id=draft.draft_id,
            wamid=wamid,
            mode=mode,
        )
        aspect_ratio = derive_aspect_ratio(width=width, height=height)
        prompt = build_generation_prompt(
            draft=draft,
            mode=mode,
            instruction_text=instruction_text,
        )
        prompt = f"{prompt}\nTarget aspect ratio: {aspect_ratio}."
        payload = await self._build_request_payload(
            prompt=prompt,
            mode=mode,
            draft=draft,
        )
        await self._emit_gemini_trace(
            prompt=prompt,
            mode=mode,
            draft=draft,
            aspect_ratio=aspect_ratio,
            payload=payload,
        )

        response: httpx.Response | None = None
        for attempt in range(1, self._max_submit_attempts + 1):
            try:
                response = await self._client.post(
                    self._generate_url,
                    params={"key": self._api_key},
                    json=payload,
                )
                response.raise_for_status()
                break
            except httpx.HTTPStatusError as exc:
                status_code = exc.response.status_code if exc.response is not None else None
                body_excerpt = _response_excerpt(exc.response)
                detail = body_excerpt or "No response body"
                retryable = _is_retryable_gemini_http_status(status_code)
                if retryable and attempt < self._max_submit_attempts:
                    retry_delay = _retry_delay_seconds(
                        attempt=attempt,
                        base_seconds=self._retry_base_seconds,
                    )
                    logger.warning(
                        "Gemini HTTP error (status=%s, attempt=%s/%s). Retrying in %.2fs. body=%s",
                        status_code,
                        attempt,
                        self._max_submit_attempts,
                        retry_delay,
                        body_excerpt,
                    )
                    await asyncio.sleep(retry_delay)
                    continue
                logger.warning(
                    "Gemini image generation HTTP error (status=%s, body=%s)",
                    status_code,
                    body_excerpt,
                )
                raise AdGenerationError(
                    f"Gemini image generation failed with HTTP {status_code}. {detail}"
                ) from exc
            except httpx.RequestError as exc:
                detail = str(exc).strip() or exc.__class__.__name__
                retryable = _is_retryable_gemini_request_error(exc)
                if retryable and attempt < self._max_submit_attempts:
                    retry_delay = _retry_delay_seconds(
                        attempt=attempt,
                        base_seconds=self._retry_base_seconds,
                    )
                    logger.warning(
                        "Gemini request error (%s, attempt=%s/%s). Retrying in %.2fs. detail=%s",
                        exc.__class__.__name__,
                        attempt,
                        self._max_submit_attempts,
                        retry_delay,
                        detail,
                    )
                    await asyncio.sleep(retry_delay)
                    continue
                logger.warning(
                    "Gemini image generation request error (%s): %s",
                    exc.__class__.__name__,
                    detail,
                )
                raise AdGenerationError(
                    f"Gemini image generation request error ({exc.__class__.__name__}): {detail}"
                ) from exc

        if response is None:
            raise AdGenerationError("Gemini image generation failed before receiving a response")

        try:
            response_payload = response.json()
        except ValueError as exc:
            logger.warning("Gemini image generation returned invalid JSON")
            raise AdGenerationError("Gemini response is not valid JSON") from exc
        if not isinstance(response_payload, dict):
            raise AdGenerationError("Gemini response has unexpected format")

        try:
            image_bytes, mime_type = _extract_gemini_inline_image(response_payload)
        except AdGenerationError as exc:
            # Some Gemini responses can return fileData/file_data URI outputs
            # instead of inline image bytes.
            file_uri, file_mime = _extract_gemini_file_image_uri(response_payload)
            if file_uri is None:
                raise exc
            downloaded_bytes, downloaded_mime = await self._download_reference_image(file_uri)
            image_bytes = downloaded_bytes
            if downloaded_mime.startswith("image/"):
                mime_type = downloaded_mime
            elif file_mime is not None and file_mime.startswith("image/"):
                mime_type = file_mime
            else:
                mime_type = "image/png"

        if not image_bytes:
            raise AdGenerationError("Gemini response returned an empty image payload")

        if not mime_type.startswith("image/"):
            raise AdGenerationError(f"Gemini response returned non-image mime type: {mime_type}")

        try:
            uploaded = await self._media_store.upload_bytes(
                content=image_bytes,
                content_type=mime_type,
                suffix=_image_suffix_for_mime_type(mime_type),
            )
        except MediaStoreError as exc:
            raise AdGenerationError(
                "Gemini generated an image but media storage is not configured. "
                "Configure MEDIA_STORE_MODE=gcs or MEDIA_STORE_MODE=s3 to publish preview images."
            ) from exc

        job_id = str(uuid.uuid4())
        self._results[job_id] = GenerationPollResult(
            status=NanoBananaJobStatus.COMPLETED,
            output_image_url=uploaded.public_url,
        )
        logger.info("Gemini image generation completed (job_id=%s)", job_id)

        return GenerationSubmission(
            job_id=job_id,
            idempotency_key=idempotency_key,
            mode=mode,
            request_payload=payload,
        )

    async def wait_for_completion(self, *, job_id: str) -> GenerationPollResult:
        result = self._results.get(job_id)
        if result is None:
            raise AdGenerationError(f"Unknown Gemini generation job ID: {job_id}")
        return result

    async def close(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    async def _emit_gemini_trace(
        self,
        *,
        prompt: str,
        mode: GenerationMode,
        draft: GenerationDraftInput,
        aspect_ratio: str,
        payload: dict[str, Any],
    ) -> None:
        if not self._trace_enabled:
            return
        context = self._trace_context.get()
        if context is None or context.trace_sink is None:
            return
        metadata = {
            "wamid": context.wamid,
            "provider": "gemini",
            "model": self._model,
            "mode": mode.value,
            "aspect_ratio": aspect_ratio,
            "draft_id": str(draft.draft_id),
            "request_params": {
                "max_submit_attempts": self._max_submit_attempts,
                "retry_base_seconds": self._retry_base_seconds,
            },
            "prompt": self._truncate_for_trace(prompt),
            "payload": self._sanitize_payload_for_trace(payload),
        }
        try:
            await context.trace_sink("gemini_request_debug", context.operator_phone, metadata)
        except Exception:
            logger.warning("Failed to emit Gemini trace event", exc_info=True)

    def _sanitize_payload_for_trace(self, payload: dict[str, Any]) -> dict[str, Any]:
        sanitized = dict(payload)
        contents = payload.get("contents")
        if not isinstance(contents, list):
            return sanitized

        sanitized_contents: list[dict[str, Any]] = []
        for content in contents:
            if not isinstance(content, dict):
                continue
            parts = content.get("parts")
            if not isinstance(parts, list):
                sanitized_contents.append(dict(content))
                continue

            sanitized_parts: list[dict[str, Any]] = []
            for part in parts:
                if not isinstance(part, dict):
                    continue
                if "text" in part and isinstance(part["text"], str):
                    sanitized_parts.append({"text": self._truncate_for_trace(part["text"])})
                    continue
                inline_data = part.get("inline_data")
                if isinstance(inline_data, dict):
                    data_value = inline_data.get("data")
                    data_len = len(data_value) if isinstance(data_value, str) else 0
                    sanitized_parts.append(
                        {
                            "inline_data": {
                                "mime_type": inline_data.get("mime_type"),
                                "data_redacted": True,
                                "data_b64_chars": data_len,
                            }
                        }
                    )
                    continue
                sanitized_parts.append(dict(part))

            content_copy = dict(content)
            content_copy["parts"] = sanitized_parts
            sanitized_contents.append(content_copy)

        sanitized["contents"] = sanitized_contents
        return sanitized

    def _truncate_for_trace(self, text: str) -> str:
        if len(text) <= self._trace_max_chars:
            return text
        return text[: self._trace_max_chars] + "..."

    async def _build_request_payload(
        self,
        *,
        prompt: str,
        mode: GenerationMode,
        draft: GenerationDraftInput,
    ) -> dict[str, Any]:
        parts: list[dict[str, Any]] = [{"text": prompt}]

        # Include operator product photo as inline image data so the model
        # can actually "see" the product.
        if draft.photo_url:
            try:
                photo_content, photo_mime_type = await self._download_reference_image(
                    draft.photo_url
                )
                parts.append(
                    {
                        "inline_data": {
                            "mime_type": photo_mime_type,
                            "data": base64.b64encode(photo_content).decode("ascii"),
                        }
                    }
                )
            except AdGenerationError:
                logger.warning(
                    "Could not download product photo for inline generation (url=%s)",
                    draft.photo_url,
                )

        # Include reference image for REFERENCE mode.
        reference_url = draft.preview_reference_url or draft.rendered_image_url
        if mode == GenerationMode.REFERENCE and reference_url:
            reference_content, reference_mime_type = await self._download_reference_image(
                reference_url
            )
            parts.append(
                {
                    "inline_data": {
                        "mime_type": reference_mime_type,
                        "data": base64.b64encode(reference_content).decode("ascii"),
                    }
                }
            )

        return {
            "contents": [{"parts": parts}],
            "generationConfig": {"responseModalities": ["IMAGE"]},
        }

    async def _download_reference_image(self, url: str) -> tuple[bytes, str]:
        try:
            response = await self._client.get(url)
            response.raise_for_status()
        except httpx.HTTPStatusError as exc:
            raise AdGenerationError(
                f"Failed to fetch reference image for Gemini regeneration: {exc}"
            ) from exc
        except httpx.RequestError as exc:
            raise AdGenerationError(
                f"Failed to fetch reference image for Gemini regeneration: {exc}"
            ) from exc

        content_type_header = response.headers.get("content-type", "image/png")
        mime_type = content_type_header.split(";", 1)[0].strip().lower() or "image/png"
        return response.content, mime_type


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
        self._status_api_url_fallback_template = _derive_status_fallback_template(
            api_url=api_url,
            base_url=base_url,
            current_template=self._status_api_url_template,
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
        image_urls: list[str] = []
        reference_image_url = draft.preview_reference_url or draft.rendered_image_url
        if mode == GenerationMode.REFERENCE and reference_image_url:
            image_urls.append(reference_image_url)
        if draft.photo_url and draft.photo_url not in image_urls:
            image_urls.append(draft.photo_url)
        if image_urls:
            payload["imageUrls"] = image_urls

        try:
            response = await self._client.post(
                self._api_url,
                json=payload,
                headers={"Authorization": f"Bearer {self._api_key}"},
            )
            response.raise_for_status()
        except httpx.HTTPStatusError as exc:
            status_code = exc.response.status_code if exc.response is not None else None
            logger.warning(
                "Nano Banana submission HTTP error (status=%s, url=%s, body=%s)",
                status_code,
                self._api_url,
                _response_excerpt(exc.response),
            )
            raise AdGenerationError(f"Nano Banana submission failed: {exc}") from exc
        except httpx.RequestError as exc:
            logger.warning(
                "Nano Banana submission request error (url=%s, error=%s)",
                self._api_url,
                exc,
            )
            raise AdGenerationError(f"Nano Banana submission failed: {exc}") from exc

        try:
            response_payload = response.json()
        except ValueError as exc:
            logger.warning(
                "Nano Banana submission returned invalid JSON (url=%s, body=%s)",
                self._api_url,
                _response_excerpt(response),
            )
            raise AdGenerationError("Nano Banana response is not valid JSON") from exc
        if not isinstance(response_payload, dict):
            logger.warning(
                "Nano Banana submission returned unexpected payload type (url=%s, payload_type=%s)",
                self._api_url,
                type(response_payload).__name__,
            )
            raise AdGenerationError("Nano Banana response has unexpected format")

        if "code" in response_payload and _coerce_int(response_payload.get("code")) != 200:
            message = response_payload.get("msg")
            logger.warning(
                "Nano Banana generation rejected (url=%s, code=%s, msg=%s)",
                self._api_url,
                response_payload.get("code"),
                message,
            )
            raise AdGenerationError(
                f"Nano Banana generation rejected: {message or 'Unknown error'}"
            )

        job_id = _extract_submission_job_id(response_payload)
        if not isinstance(job_id, str) or not job_id.strip():
            logger.warning(
                "Nano Banana submission response missing task id (url=%s, keys=%s)",
                self._api_url,
                sorted(response_payload.keys()),
            )
            raise AdGenerationError("Nano Banana response missing task ID")
        logger.info(
            "Nano Banana generation submitted (job_id=%s, mode=%s)",
            job_id.strip(),
            mode.value,
        )

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
                if result.status == NanoBananaJobStatus.COMPLETED:
                    logger.info(
                        "Nano Banana generation completed (job_id=%s, output_image_url=%s)",
                        job_id,
                        result.output_image_url,
                    )
                else:
                    logger.warning(
                        "Nano Banana generation failed "
                        "(job_id=%s, error_code=%s, error_message=%s)",
                        job_id,
                        result.error_code,
                        result.error_message,
                    )
                return result

            await asyncio.sleep(interval)
            interval = min(interval * 2, self._poll_max_seconds)

        logger.error(
            "Nano Banana polling timed out (job_id=%s, timeout_seconds=%s)",
            job_id,
            self._poll_timeout_seconds,
        )
        raise AdGenerationError(
            f"Nano Banana polling timed out for job_id={job_id} after "
            f"{self._poll_timeout_seconds:.0f} seconds"
        )

    async def _fetch_job_status(self, *, job_id: str) -> GenerationPollResult:
        status_url = self._status_api_url_template.format(job_id=job_id)
        try:
            response = await self._client.get(
                status_url,
                headers={"Authorization": f"Bearer {self._api_key}"},
            )
            response.raise_for_status()
        except httpx.HTTPStatusError as exc:
            status_code = exc.response.status_code if exc.response is not None else None
            if status_code == 404 and self._status_api_url_fallback_template:
                fallback_url = self._status_api_url_fallback_template.format(job_id=job_id)
                if fallback_url != status_url:
                    logger.info(
                        "Nano Banana polling fallback after 404 "
                        "(job_id=%s, primary_url=%s, fallback_url=%s)",
                        job_id,
                        status_url,
                        fallback_url,
                    )
                    try:
                        response = await self._client.get(
                            fallback_url,
                            headers={"Authorization": f"Bearer {self._api_key}"},
                        )
                        response.raise_for_status()
                        return self._parse_poll_response(response=response, job_id=job_id)
                    except httpx.HTTPStatusError as fallback_exc:
                        fallback_status_code = (
                            fallback_exc.response.status_code
                            if fallback_exc.response is not None
                            else None
                        )
                        if fallback_status_code is not None and (
                            fallback_status_code == 429 or fallback_status_code >= 500
                        ):
                            logger.warning(
                                "Nano Banana fallback polling retryable HTTP status "
                                "(job_id=%s, status=%s, body=%s)",
                                job_id,
                                fallback_status_code,
                                _response_excerpt(fallback_exc.response),
                            )
                            raise _RetryablePollError(
                                f"Retryable fallback poll HTTP status: {fallback_status_code}"
                            ) from fallback_exc
                        logger.warning(
                            "Nano Banana fallback polling non-retryable HTTP status "
                            "(job_id=%s, status=%s, body=%s)",
                            job_id,
                            fallback_status_code,
                            _response_excerpt(fallback_exc.response),
                        )
                        raise AdGenerationError(
                            f"Nano Banana fallback polling failed: {fallback_exc}"
                        ) from fallback_exc
                    except httpx.RequestError as fallback_exc:
                        logger.warning(
                            "Nano Banana fallback polling request error (job_id=%s, error=%s)",
                            job_id,
                            fallback_exc,
                        )
                        raise _RetryablePollError(
                            f"Retryable fallback poll request error: {fallback_exc}"
                        ) from fallback_exc
            if status_code is not None and (status_code == 429 or status_code >= 500):
                logger.warning(
                    "Nano Banana polling retryable HTTP status (job_id=%s, status=%s, body=%s)",
                    job_id,
                    status_code,
                    _response_excerpt(exc.response),
                )
                raise _RetryablePollError(f"Retryable poll HTTP status: {status_code}") from exc
            logger.warning(
                "Nano Banana polling non-retryable HTTP status (job_id=%s, status=%s, body=%s)",
                job_id,
                status_code,
                _response_excerpt(exc.response),
            )
            raise AdGenerationError(f"Nano Banana polling failed: {exc}") from exc
        except httpx.RequestError as exc:
            logger.warning("Nano Banana polling request error (job_id=%s, error=%s)", job_id, exc)
            raise _RetryablePollError(f"Retryable poll request error: {exc}") from exc
        return self._parse_poll_response(response=response, job_id=job_id)

    def _parse_poll_response(
        self,
        *,
        response: httpx.Response,
        job_id: str,
    ) -> GenerationPollResult:

        try:
            payload = response.json()
        except ValueError as exc:
            logger.warning(
                "Nano Banana polling returned invalid JSON (job_id=%s, body=%s)",
                job_id,
                _response_excerpt(response),
            )
            raise AdGenerationError("Nano Banana poll response is not valid JSON") from exc
        if not isinstance(payload, dict):
            logger.warning(
                "Nano Banana polling returned unexpected payload type (job_id=%s, payload_type=%s)",
                job_id,
                type(payload).__name__,
            )
            raise AdGenerationError("Nano Banana poll response has unexpected format")

        success_flag_container = payload
        success_flag = _coerce_int(payload.get("successFlag"))
        data_payload = payload.get("data")
        if success_flag is None and isinstance(data_payload, dict):
            success_flag = _coerce_int(data_payload.get("successFlag"))
            if success_flag is not None:
                success_flag_container = data_payload

        if success_flag == 0:
            return GenerationPollResult(status=NanoBananaJobStatus.RUNNING)
        if success_flag == 1:
            output_image_url = _extract_output_image_url(success_flag_container)
            return GenerationPollResult(
                status=NanoBananaJobStatus.COMPLETED,
                output_image_url=output_image_url,
            )
        if success_flag in {2, 3}:
            error_message = success_flag_container.get("errorMessage") or payload.get(
                "errorMessage"
            )
            return GenerationPollResult(
                status=NanoBananaJobStatus.FAILED,
                error_code=str(success_flag),
                error_message=error_message if isinstance(error_message, str) else None,
            )
        legacy_status = payload.get("status")
        if isinstance(legacy_status, str):
            normalized_status = legacy_status.strip().lower()
            if normalized_status in {"queued", "pending"}:
                return GenerationPollResult(status=NanoBananaJobStatus.QUEUED)
            if normalized_status in {"running", "processing", "in_progress"}:
                return GenerationPollResult(status=NanoBananaJobStatus.RUNNING)
            if normalized_status in {"completed", "success", "succeeded"}:
                return GenerationPollResult(
                    status=NanoBananaJobStatus.COMPLETED,
                    output_image_url=_extract_output_image_url(payload),
                )
            if normalized_status in {"failed", "error"}:
                return GenerationPollResult(
                    status=NanoBananaJobStatus.FAILED,
                    error_code=str(payload.get("error_code") or normalized_status),
                    error_message=(
                        payload.get("error_message")
                        if isinstance(payload.get("error_message"), str)
                        else None
                    ),
                )

        logger.warning(
            "Nano Banana polling payload unsupported (job_id=%s, keys=%s)",
            job_id,
            sorted(payload.keys()),
        )
        current_status = payload.get("status")
        raise AdGenerationError(
            "Unsupported Nano Banana poll response "
            f"(successFlag={success_flag}, status={current_status})"
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
    sections: list[str] = [
        "Generate a professional retail advertisement image.",
        f"Language: {draft.language}.",
        f"Mode: {mode.value}.",
    ]

    # --- PRIMARY: must be large and prominent in the ad ---
    sections.append("")
    sections.append("=== PRIMARY (must be large and prominent in the ad) ===")
    sections.append(f"Product Name: {draft.product_name or 'unknown'}.")
    resolved_brand = draft.product_brand or draft.enriched_brand
    if resolved_brand:
        sections.append(f"Brand: {resolved_brand}.")
    if (
        draft.product_brand
        and draft.enriched_brand
        and draft.product_brand.strip().casefold() != draft.enriched_brand.strip().casefold()
    ):
        sections.append(
            "Brand source note: Use the operator-provided brand as the final displayed brand."
        )
    sections.append(f"Price: {_price_to_text(draft.price, draft.currency)}.")

    # --- BUSINESS BRANDING ---
    has_branding = (
        draft.business_name
        or draft.logo_url
        or draft.brand_colors
        or draft.store_type
        or draft.creative_guidance
    )
    if has_branding:
        sections.append("")
        sections.append("=== BUSINESS BRANDING ===")
        if draft.business_name:
            sections.append(f"Business Name: {draft.business_name}.")
        if draft.store_type:
            sections.append(f"Store Type: {draft.store_type}.")
        if draft.brand_colors:
            colors_text = ", ".join(draft.brand_colors)
            sections.append(
                f"Brand Colors: {colors_text}. Use these as the dominant color palette."
            )
        if draft.logo_url:
            sections.append(f"Business Logo URL: {draft.logo_url}.")
        if draft.creative_guidance:
            sections.append(f"Creative Guidance: {draft.creative_guidance}.")

    # --- SUPPORTING DETAILS ---
    supporting: list[str] = []
    if draft.promo_text:
        supporting.append(f"Promo Text: {draft.promo_text}.")
    if draft.ean:
        supporting.append(f"EAN: {draft.ean}.")
    if draft.photo_url:
        supporting.append(f"Product Photo URL: {draft.photo_url}.")
    if draft.enriched_category:
        supporting.append(f"Category: {draft.enriched_category}.")
    if draft.enriched_description:
        supporting.append(f"Description: {draft.enriched_description}.")
    if supporting:
        sections.append("")
        sections.append("=== SUPPORTING DETAILS ===")
        sections.extend(supporting)

    # --- Reference mode ---
    if mode == GenerationMode.REFERENCE and (
        draft.preview_reference_url or draft.rendered_image_url
    ):
        sections.append("")
        sections.append("Preserve the overall visual style of the previous preview.")

    # --- Operator instruction ---
    cleaned_instruction = " ".join(instruction_text.split()).strip()
    if cleaned_instruction:
        sections.append("")
        sections.append(f"Operator instruction: {cleaned_instruction}.")

    return "\n".join(sections)


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


def _derive_status_fallback_template(
    *,
    api_url: str | None,
    base_url: str | None,
    current_template: str,
) -> str | None:
    if base_url and base_url.strip():
        fallback = f"{base_url.rstrip('/')}/record-info?taskId={{job_id}}"
        return fallback if fallback != current_template else None
    if not api_url or not api_url.strip():
        return None
    normalized_api_url = api_url.strip()
    if "/generate-2" in normalized_api_url:
        prefix = normalized_api_url.split("/generate-2", 1)[0]
        fallback = f"{prefix}/record-info?taskId={{job_id}}"
        return fallback if fallback != current_template else None
    if "/generate" in normalized_api_url:
        prefix = normalized_api_url.split("/generate", 1)[0]
        fallback = f"{prefix}/record-info?taskId={{job_id}}"
        return fallback if fallback != current_template else None
    return None


def _price_to_text(price: Decimal | None, currency: str) -> str:
    if price is None:
        return "not provided"
    return f"{price} {currency}"


def _coerce_int(value: Any) -> int | None:
    if isinstance(value, int):
        return value
    if isinstance(value, str):
        stripped = value.strip()
        if stripped.isdigit():
            return int(stripped)
    return None


def _extract_submission_job_id(payload: dict[str, Any]) -> str | None:
    data = payload.get("data")
    if isinstance(data, dict):
        task_id = data.get("taskId")
        if isinstance(task_id, str) and task_id.strip():
            return task_id
        job_id = data.get("job_id")
        if isinstance(job_id, str) and job_id.strip():
            return job_id
    task_id = payload.get("taskId")
    if isinstance(task_id, str) and task_id.strip():
        return task_id
    job_id = payload.get("job_id")
    if isinstance(job_id, str) and job_id.strip():
        return job_id
    return None


def _extract_output_image_url(payload: dict[str, Any]) -> str | None:
    response_data = payload.get("response")
    if isinstance(response_data, dict):
        result_image_url = response_data.get("resultImageUrl")
        if isinstance(result_image_url, str) and result_image_url.strip():
            return result_image_url
        output_image_url = response_data.get("output_image_url")
        if isinstance(output_image_url, str) and output_image_url.strip():
            return output_image_url
        output_image_url_camel = response_data.get("outputImageUrl")
        if isinstance(output_image_url_camel, str) and output_image_url_camel.strip():
            return output_image_url_camel
    for key in ("resultImageUrl", "output_image_url", "outputImageUrl"):
        value = payload.get(key)
        if isinstance(value, str) and value.strip():
            return value
    return None


def _extract_gemini_inline_image(payload: dict[str, Any]) -> tuple[bytes, str]:
    candidates = payload.get("candidates")
    if not isinstance(candidates, list):
        raise AdGenerationError("Gemini response did not include any candidates")

    for candidate in candidates:
        if not isinstance(candidate, dict):
            continue
        content = candidate.get("content")
        if not isinstance(content, dict):
            continue
        parts = content.get("parts")
        if not isinstance(parts, list):
            continue

        for part in parts:
            if not isinstance(part, dict):
                continue
            inline_data = part.get("inlineData")
            if not isinstance(inline_data, dict):
                inline_data = part.get("inline_data")
            if not isinstance(inline_data, dict):
                continue

            encoded = inline_data.get("data")
            if not isinstance(encoded, str) or not encoded.strip():
                continue

            mime_type_raw = inline_data.get("mimeType") or inline_data.get("mime_type")
            mime_type = (
                mime_type_raw.strip().lower()
                if isinstance(mime_type_raw, str) and mime_type_raw.strip()
                else "image/png"
            )
            try:
                return base64.b64decode(encoded, validate=True), mime_type
            except ValueError as exc:
                raise AdGenerationError(
                    "Gemini response included invalid base64 image data"
                ) from exc

    diagnostics = _summarize_gemini_non_image_response(payload)
    raise AdGenerationError(f"Gemini response did not include an image output. {diagnostics}")


def _extract_gemini_file_image_uri(payload: dict[str, Any]) -> tuple[str | None, str | None]:
    candidates = payload.get("candidates")
    if not isinstance(candidates, list):
        return None, None

    for candidate in candidates:
        if not isinstance(candidate, dict):
            continue
        content = candidate.get("content")
        if not isinstance(content, dict):
            continue
        parts = content.get("parts")
        if not isinstance(parts, list):
            continue
        for part in parts:
            if not isinstance(part, dict):
                continue
            file_data = part.get("fileData")
            if not isinstance(file_data, dict):
                file_data = part.get("file_data")
            if not isinstance(file_data, dict):
                continue

            file_uri = file_data.get("fileUri") or file_data.get("file_uri")
            if not isinstance(file_uri, str) or not file_uri.strip():
                continue

            mime_type_raw = file_data.get("mimeType") or file_data.get("mime_type")
            mime_type = (
                mime_type_raw.strip().lower()
                if isinstance(mime_type_raw, str) and mime_type_raw.strip()
                else None
            )
            return file_uri.strip(), mime_type

    return None, None


def _summarize_gemini_non_image_response(payload: dict[str, Any], *, max_len: int = 240) -> str:
    diagnostics: list[str] = []

    prompt_feedback = payload.get("promptFeedback")
    if isinstance(prompt_feedback, dict):
        block_reason = prompt_feedback.get("blockReason")
        if isinstance(block_reason, str) and block_reason.strip():
            diagnostics.append(f"block_reason={block_reason.strip()}")
        block_message = prompt_feedback.get("blockReasonMessage")
        if isinstance(block_message, str) and block_message.strip():
            diagnostics.append(f"block_message={_clip_text(block_message, max_len=max_len)}")

    candidates = payload.get("candidates")
    if isinstance(candidates, list):
        finish_reasons: list[str] = []
        text_parts: list[str] = []
        has_file_data = False
        for candidate in candidates:
            if not isinstance(candidate, dict):
                continue
            finish_reason = candidate.get("finishReason")
            if isinstance(finish_reason, str) and finish_reason.strip():
                finish_reasons.append(finish_reason.strip())

            content = candidate.get("content")
            if not isinstance(content, dict):
                continue
            parts = content.get("parts")
            if not isinstance(parts, list):
                continue
            for part in parts:
                if not isinstance(part, dict):
                    continue
                has_file_data_part = isinstance(part.get("fileData"), dict) or isinstance(
                    part.get("file_data"), dict
                )
                if has_file_data_part:
                    has_file_data = True
                text = part.get("text")
                if isinstance(text, str) and text.strip():
                    text_parts.append(text.strip())

        if finish_reasons:
            diagnostics.append(f"finish_reason={','.join(finish_reasons)}")
        if has_file_data:
            diagnostics.append("file_data_present=true")
        if text_parts:
            diagnostics.append(f"text={_clip_text(text_parts[0], max_len=max_len)}")

    if diagnostics:
        return "; ".join(diagnostics)
    return "no additional diagnostics in Gemini payload"


def _clip_text(value: str, *, max_len: int) -> str:
    compact = " ".join(value.split()).strip()
    if len(compact) <= max_len:
        return compact
    return f"{compact[:max_len]}..."


def _image_suffix_for_mime_type(mime_type: str) -> str:
    normalized = mime_type.strip().lower()
    if normalized == "image/png":
        return ".png"
    if normalized == "image/jpeg":
        return ".jpg"
    if normalized == "image/webp":
        return ".webp"
    return ".img"


def _response_excerpt(response: httpx.Response | None, *, limit: int = 300) -> str | None:
    if response is None:
        return None
    text = response.text
    if not text:
        return None
    compact = " ".join(text.split())
    if len(compact) > limit:
        return f"{compact[:limit]}..."
    return compact


def _is_retryable_gemini_http_status(status_code: int | None) -> bool:
    return status_code in {408, 429, 500, 502, 503, 504}


def _is_retryable_gemini_request_error(exc: httpx.RequestError) -> bool:
    return isinstance(
        exc,
        (
            httpx.ConnectTimeout,
            httpx.ReadTimeout,
            httpx.WriteTimeout,
            httpx.PoolTimeout,
            httpx.ConnectError,
            httpx.RemoteProtocolError,
        ),
    )


def _retry_delay_seconds(*, attempt: int, base_seconds: float) -> float:
    # Exponential backoff capped to keep inline webhook processing responsive.
    return min(base_seconds * (2 ** (attempt - 1)), 4.0)
