"""Relay API response models and outbound payload contracts.

These define the HTTP response contract for relay routes and the webhook
payload contract sent by notifier backends. All types are exported to
consumers via the generated TypeScript and Python type packages (make types).
"""

from pydantic import BaseModel, ConfigDict

from shared import RelayName, Trade  # noqa: F401 — RelayName re-exported for schema_gen hoisting

from .notifier.models import WebhookPayload as WebhookPayload
from .notifier.models import WebhookPayloadTrades as WebhookPayloadTrades

# ── POST /relays/{relay_name}/poll/{poll_idx} ────────────────────────

class RunPollResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    trades: list[Trade]


# ── GET /health ──────────────────────────────────────────────────────

class HealthResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: str
