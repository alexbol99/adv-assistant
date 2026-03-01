import hashlib
import hmac
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any


@dataclass(slots=True)
class InboundMessageEvent:
    wamid: str
    operator_phone: str
    timestamp: datetime | None
    raw_message: dict[str, Any]


def normalize_phone_number(raw_phone: str) -> str:
    digits = "".join(ch for ch in raw_phone if ch.isdigit())
    if not digits:
        return raw_phone.strip()
    return f"+{digits}"


def parse_whatsapp_timestamp(raw_timestamp: str | None) -> datetime | None:
    if not raw_timestamp:
        return None
    try:
        return datetime.fromtimestamp(int(raw_timestamp), tz=UTC)
    except (TypeError, ValueError):
        return None


def extract_inbound_messages(payload: dict[str, Any]) -> list[InboundMessageEvent]:
    events: list[InboundMessageEvent] = []
    for entry in payload.get("entry", []):
        for change in entry.get("changes", []):
            value = change.get("value", {})
            messages = value.get("messages", [])
            for message in messages:
                wamid = message.get("id")
                raw_phone = message.get("from")
                if not wamid or not raw_phone:
                    continue
                events.append(
                    InboundMessageEvent(
                        wamid=wamid,
                        operator_phone=normalize_phone_number(raw_phone),
                        timestamp=parse_whatsapp_timestamp(message.get("timestamp")),
                        raw_message=message,
                    )
                )
    return events


def verify_x_hub_signature(
    *,
    app_secret: str | None,
    payload_body: bytes,
    signature_header: str | None,
) -> bool:
    if not app_secret or not signature_header:
        return False
    if not signature_header.startswith("sha256="):
        return False

    provided = signature_header[len("sha256=") :]
    expected = hmac.new(
        app_secret.encode("utf-8"),
        payload_body,
        hashlib.sha256,
    ).hexdigest()
    return hmac.compare_digest(provided, expected)


def is_within_replay_window(
    *,
    message_timestamp: datetime | None,
    now: datetime,
    replay_window_seconds: int,
) -> bool:
    if message_timestamp is None:
        # Spec decision: enforce replay window only when provider timestamp is available.
        return True
    delta_seconds = abs((now - message_timestamp).total_seconds())
    return delta_seconds <= replay_window_seconds
