"""Relay API response models.

These define the HTTP response contract for relay routes. They are
exported to consumers via the generated TypeScript and Python type
packages (``make types``).
"""

from pydantic import BaseModel, ConfigDict

from shared import Trade

# ── POST /relays/{relay_name}/poll/{poll_idx} ────────────────────────

class RunPollResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    trades: list[Trade]


# ── GET /health ──────────────────────────────────────────────────────

class HealthResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: str
