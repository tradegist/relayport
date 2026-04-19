"""Shared models and utilities — barrel re-exports.

Models (public contract) live in ``models.py``.
Internal utilities live in ``utilities.py``.

All existing ``from shared import X`` imports continue to work via
this barrel.
"""

# ── Public types (re-exported for consumers) ─────────────────────────
from .models import AssetClass as AssetClass
from .models import BuySell as BuySell
from .models import Fill as Fill
from .models import OrderType as OrderType
from .models import RelayName as RelayName
from .models import Source as Source
from .models import Trade as Trade

# ── Internal utilities (re-exported for sibling services) ────────────
from .utilities import aggregate_fills as aggregate_fills

# ── Constants ────────────────────────────────────────────────────────
DEDUP_DB_PATH = "/data/dedup/fills.db"
