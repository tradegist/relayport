"""Outbound payload contracts for notifier backends.

!! PUBLIC CONTRACT — every type defined here is exported to consumers
!! via the generated TypeScript and Python type packages (make types).
!! Add new payload variants here as new notifier event types are introduced.
"""

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict

from shared import RelayName, Trade


def _require_discriminators(schema: dict[str, Any]) -> None:
    """Keep discriminator fields required in JSON Schema despite defaults."""
    req: list[str] = schema.get("required", [])
    for f in ("relay", "type"):
        if f not in req:
            req.append(f)
    schema["required"] = req


class WebhookPayloadTrades(BaseModel):
    """Webhook payload for trade execution events."""

    model_config = ConfigDict(extra="forbid", json_schema_extra=_require_discriminators)

    relay: RelayName
    type: Literal["trades"] = "trades"
    data: list[Trade]
    errors: list[str]


# Discriminated-union alias — grows as new event types are added.
WebhookPayload = WebhookPayloadTrades
