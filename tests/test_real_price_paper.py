from bot.exchange.paper import PaperExchangeClient
from bot.exchange.real_price_paper import RealPricePaperExchangeClient
from bot.models import MarketOrderIntent, MarketRules, OrderBookSnapshot, Side


class _StubLiveClient:
    def __init__(self):
        self.get_mid_price_calls = []
        self.get_market_rules_calls = []
        self.get_order_book_calls = []
        self.warm_token_cache_calls = []

    def get_mid_price(self, token_id):
        self.get_mid_price_calls.append(token_id)
        return 0.12

    def get_market_rules(self, token_id):
        self.get_market_rules_calls.append(token_id)
        return MarketRules(tick_size=0.01, min_order_size=5.0)

    def get_order_book(self, token_id):
        self.get_order_book_calls.append(token_id)
        return OrderBookSnapshot(
            token_id=token_id,
            bids=(),
            asks=(),
            tick_size=0.01,
            min_order_size=5.0,
            timestamp=123,
        )

    def warm_token_cache(self, token_id):
        self.warm_token_cache_calls.append(token_id)

    def place_market_order(self, order):
        raise AssertionError("real order placement must never be called in paper_real_prices mode")

    def place_limit_order(self, order):
        raise AssertionError("real order placement must never be called in paper_real_prices mode")

    def cancel_order(self, order_id):
        raise AssertionError("real cancel must never be called in paper_real_prices mode")


def test_reads_delegate_to_live_client():
    live = _StubLiveClient()
    hybrid = RealPricePaperExchangeClient(live)

    assert hybrid.get_mid_price("tok") == 0.12
    assert hybrid.get_market_rules("tok").tick_size == 0.01
    book = hybrid.get_order_book("tok")
    assert book.timestamp == 123
    hybrid.warm_token_cache("tok")

    assert live.get_mid_price_calls == ["tok"]
    assert live.get_market_rules_calls == ["tok"]
    assert live.get_order_book_calls == ["tok"]
    assert live.warm_token_cache_calls == ["tok"]


def test_place_market_order_simulates_fill_without_touching_live_client():
    live = _StubLiveClient()
    hybrid = RealPricePaperExchangeClient(live, paper_client=PaperExchangeClient(initial_collateral_balance=100.0))

    order = MarketOrderIntent(token_id="tok", side=Side.BUY, amount=10.0, reference_price=0.10)
    result = hybrid.place_market_order(order)

    assert result.status == "matched"
    assert hybrid.get_collateral_balance() == 90.0
    assert hybrid.get_conditional_balance("tok") == 100.0


def test_place_limit_order_and_cancel_use_paper_client():
    from bot.models import LimitOrderIntent

    live = _StubLiveClient()
    hybrid = RealPricePaperExchangeClient(live)

    order = LimitOrderIntent(token_id="tok", side=Side.BUY, price=0.05, size=20.0)
    result = hybrid.place_limit_order(order)

    assert result.status == "simulated"
    assert hybrid.get_order(result.order_id) is not None
    assert hybrid.cancel_order(result.order_id) is True
    assert result.order_id not in [o.order_id for o in hybrid.get_open_orders("tok")]


def test_check_order_readiness_always_ready_like_paper_mode():
    live = _StubLiveClient()
    hybrid = RealPricePaperExchangeClient(live)

    order = MarketOrderIntent(token_id="tok", side=Side.BUY, amount=10.0, reference_price=0.10)
    readiness = hybrid.check_order_readiness(order)

    assert readiness.ready is True


def test_bootstrap_live_trading_is_a_noop():
    live = _StubLiveClient()
    hybrid = RealPricePaperExchangeClient(live)

    # Must not raise and must not call anything on the live client.
    hybrid.bootstrap_live_trading("tok")
