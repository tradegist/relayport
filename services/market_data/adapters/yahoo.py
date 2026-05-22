from market_data.adapters import MarketDataAdapter
from market_data.models.dividends import DividendsUpcomingItem, TickerError
from market_data.yahoo_client import YahooClient


class YahooAdapter(MarketDataAdapter):
    def __init__(self) -> None:
        self._client = YahooClient()

    def get_dividends_upcoming(
        self, symbols: list[str]
    ) -> tuple[dict[str, DividendsUpcomingItem], dict[str, TickerError]]:
        data_info, errors_raw = self._client.get_dividends_info(symbols)
        data = {
            symbol: DividendsUpcomingItem(
                ex_div_date=info.ex_div_date,
                payment_date=info.payment_date,
                dps=info.dps,
                annual_dps=info.annual_dps,
                are_dates_estimated=info.are_dates_estimated,
            )
            for symbol, info in data_info.items()
        }
        errors = {
            symbol: TickerError(code=str(exc.code), message=exc.args[0])
            for symbol, exc in errors_raw.items()
        }
        return data, errors
