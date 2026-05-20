from market_data.adapters import MarketDataAdapter
from market_data.models.dividends import DividendsUpcomingItem
from market_data.yahoo_client import YahooClient


class YahooAdapter(MarketDataAdapter):
    def __init__(self) -> None:
        self._client = YahooClient()

    def get_dividends_upcoming(
        self, symbols: list[str]
    ) -> tuple[dict[str, DividendsUpcomingItem], dict[str, str]]:
        data_info, errors = self._client.get_dividends_info(symbols)
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
        return data, errors
