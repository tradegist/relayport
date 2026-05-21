from typing import Literal

from pydantic import BaseModel, ConfigDict, field_validator

from market_data.utils import parse_string_list

MarketDataTarget = Literal["yahoo"]

_MAX_SYMBOLS = 20


class DividendsUpcomingQuery(BaseModel):
    model_config = ConfigDict(extra="forbid")

    symbol: list[str]
    target: MarketDataTarget

    @field_validator("symbol", mode="before")
    @classmethod
    def parse_symbol(cls, v: object) -> list[str]:
        return parse_string_list(v, max_count=_MAX_SYMBOLS)

    @field_validator("target", mode="before")
    @classmethod
    def validate_target(cls, v: object) -> object:
        if isinstance(v, str):
            return v.strip().lower()
        return v


class DividendsUpcomingItem(BaseModel):
    model_config = ConfigDict(extra="forbid")

    ex_div_date: str | None
    payment_date: str | None
    dps: float | None
    annual_dps: float | None
    are_dates_estimated: bool


class TickerError(BaseModel):
    model_config = ConfigDict(extra="forbid")

    code: str
    message: str


class DividendsUpcomingResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    data: dict[str, DividendsUpcomingItem]
    errors: dict[str, TickerError]
