import json
from collections.abc import Awaitable, Callable
from typing import Any, Protocol

from google.cloud import tasks_v2
from pydantic import BaseModel, Field
from starlette.concurrency import run_in_threadpool


class InboundTaskPayload(BaseModel):
    wamid: str
    operator_phone: str
    message_timestamp: str | None = None
    raw_message: dict[str, Any] = Field(default_factory=dict)


class TaskEnqueuer(Protocol):
    async def enqueue_inbound(self, payload: InboundTaskPayload) -> None: ...


class InlineTaskEnqueuer:
    def __init__(
        self,
        process_callback: Callable[[InboundTaskPayload], Awaitable[Any]],
    ) -> None:
        self._process_callback = process_callback

    async def enqueue_inbound(self, payload: InboundTaskPayload) -> None:
        await self._process_callback(payload)


class CloudTasksEnqueuer:
    def __init__(
        self,
        *,
        project_id: str,
        location: str,
        queue_name: str,
        handler_url: str,
        service_account_email: str,
        oidc_audience: str | None = None,
        client: tasks_v2.CloudTasksClient | None = None,
    ) -> None:
        self._handler_url = handler_url
        self._service_account_email = service_account_email
        self._oidc_audience = oidc_audience or handler_url
        self._client = client or tasks_v2.CloudTasksClient()
        self._queue_path = self._client.queue_path(project_id, location, queue_name)

    async def enqueue_inbound(self, payload: InboundTaskPayload) -> None:
        request = tasks_v2.CreateTaskRequest(
            parent=self._queue_path,
            task=tasks_v2.Task(
                http_request=tasks_v2.HttpRequest(
                    http_method=tasks_v2.HttpMethod.POST,
                    url=self._handler_url,
                    headers={"Content-Type": "application/json"},
                    body=json.dumps(payload.model_dump(mode="json")).encode("utf-8"),
                    oidc_token=tasks_v2.OidcToken(
                        service_account_email=self._service_account_email,
                        audience=self._oidc_audience,
                    ),
                )
            ),
        )
        await run_in_threadpool(self._client.create_task, request)
