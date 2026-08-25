import logging

from bot.exchange.paper import PaperExchangeClient
from bot.exchange.polymarket_clob import PolymarketClobExchangeClient
from bot.models import (
    LimitOrderIntent,
    MarketOrderIntent,
    MarketRules,
    OpenOrder,
    OrderBookSnapshot,
    OrderReadiness,
    OrderResult,
    Trade,
)

logger = logging.getLogger(__name__)


class RealPricePaperExchangeClient:
    """Paper mode backed by real Polymarket market data.

    Market-facing reads (order books, mid price, market rules, tick-size
    warmup) go to the live CLOB client so the strategy evaluates real
    liquidity and pricing. Everything that represents the bot's own money —
    balances, open orders, fills, order placement, cancellation — is
    delegated to an internal PaperExchangeClient so trades are simulated
    exactly like standard paper mode, including its running collateral/
    conditional balance bookkeeping. The live client is constructed with
    allow_trading=False, so even a bug that routed a write here to it would
    raise rather than send a real order.
    """

    def __init__(
        self,
        live_client: PolymarketClobExchangeClient,
        paper_client: PaperExchangeClient | None = None,
    ) -> None:
        self._live = live_client
        self._paper = paper_client if paper_client is not None else PaperExchangeClient()

    # --- reads: real market data ---

    def get_mid_price(self, token_id: str) -> float:
        return self._live.get_mid_price(token_id)

    def get_market_rules(self, token_id: str) -> MarketRules | None:
        return self._live.get_market_rules(token_id)

    def get_order_book(self, token_id: str) -> OrderBookSnapshot:
        return self._live.get_order_book(token_id)

    def warm_token_cache(self, token_id: str) -> None:
        self._live.warm_token_cache(token_id)

    # --- reads/writes: simulated account state ---

    def bootstrap_live_trading(self, token_id: str | None = None) -> None:
        self._paper.bootstrap_live_trading(token_id)

    def get_open_orders(self, token_id: str) -> list[OpenOrder]:
        return self._paper.get_open_orders(token_id)

    def get_order(self, order_id: str) -> OpenOrder | None:
        return self._paper.get_order(order_id)

    def place_limit_order(self, order: LimitOrderIntent) -> OrderResult:
        return self._paper.place_limit_order(order)

    def place_market_order(self, order: MarketOrderIntent) -> OrderResult:
        return self._paper.place_market_order(order)

    def get_trades(self, token_id: str, after_timestamp: int | None = None) -> list[Trade]:
        return self._paper.get_trades(token_id, after_timestamp)

    def check_order_readiness(self, order: LimitOrderIntent | MarketOrderIntent) -> OrderReadiness:
        return self._paper.check_order_readiness(order)

    def cancel_order(self, order_id: str) -> bool:
        return self._paper.cancel_order(order_id)

    def cancel_all(self) -> bool:
        return self._paper.cancel_all()

    def prepare_sell(self, token_id: str) -> bool:
        return self._paper.prepare_sell(token_id)

    def get_conditional_balance(self, token_id: str) -> float:
        return self._paper.get_conditional_balance(token_id)

    def get_collateral_balance(self) -> float:
        return self._paper.get_collateral_balance()
