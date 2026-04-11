"""Re-export shared models + poller-specific API types.

!! PUBLIC CONTRACT — every type defined or re-exported here is exported
!! to consumers via the generated TypeScript and Python type packages
!! (make types).  Do NOT add poller-internal helpers or intermediate
!! types here.

Shared models live in ``services/shared/``. This shim re-exports them so
existing ``from poller_models import Fill`` imports keep working.
Service-specific models (RunPollResponse, HealthResponse) live here.
"""

from pydantic import BaseModel, ConfigDict

from shared import AssetClass as AssetClass
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
