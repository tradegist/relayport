from pydantic import BaseModel, ConfigDict


class DividendInfo(BaseModel):
    model_config = ConfigDict(extra="forbid")

    ex_div_date: str | None
    payment_date: str | None
    dps: float | None
    annual_dps: float | None
    are_dates_estimated: bool


class YahooSession(BaseModel):
    model_config = ConfigDict(extra="forbid")

    cookie_string: str
    crumb: str


class CacheEntry(BaseModel):
    model_config = ConfigDict(extra="forbid")

    data: DividendInfo
    cached_at: float
