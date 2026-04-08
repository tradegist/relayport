"""Re-export shared models + poller-specific API types.

Shared models live in ``services/shared/``. This shim re-exports them so
existing ``from models_poller import Fill`` imports keep working.
Service-specific models (RunPollResponse, HealthResponse) live here.
"""

from pydantic import BaseModel, ConfigDict

from shared import BuySell as BuySell
from shared import Fill as Fill
from shared import OrderType as OrderType
from shared import Source as Source
from shared import Trade as Trade
from shared import WebhookPayloadTrades as WebhookPayloadTrades

# ── POST /ibkr/poller/run ────────────────────────────────────────────

class RunPollResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    trades: list[Trade]


# ── GET /health ──────────────────────────────────────────────────────

class HealthResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: str


# ── Schema export (used by schema_gen.py → make types) ──────────────

SCHEMA_MODELS: list[type[BaseModel]] = [
    RunPollResponse,
    HealthResponse,
]
