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
from .models import FxRateSource as FxRateSource
from .models import OptionContract as OptionContract
from .models import OrderType as OrderType
from .models import RelayName as RelayName
from .models import Source as Source
from .models import Trade as Trade

# ── Internal utilities (re-exported for sibling services) ────────────
from .time_format import normalize_timestamp as normalize_timestamp
from .time_format import parse_timezone as parse_timezone
from .time_format import to_epoch as to_epoch
from .utilities import aggregate_fills as aggregate_fills

# ── Constants ────────────────────────────────────────────────────────
DEDUP_DB_PATH = "/data/dedup/fills.db"
