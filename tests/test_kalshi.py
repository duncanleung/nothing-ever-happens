import pytest

from bot.exchange.kalshi import KalshiExchangeClient, KalshiTokenIdError, _parse_token_id
from bot.models import LimitOrderIntent, MarketOrderIntent, Side


class _FakeSession:
    def __init__(self, responses=None):
        self.responses = responses or {}
        self.calls = []

    def request_json(self, method, path, **kwargs):
        self.calls.append((method, path, kwargs))
        key = (method, path)
        if key in self.responses:
            value = self.responses[key]
            return value(kwargs) if callable(value) else value
        raise AssertionError(f"unexpected call {key}")


def _make_client(responses=None, allow_trading=True) -> tuple[KalshiExchangeClient, _FakeSession]:
    client = object.__new__(KalshiExchangeClient)
    client.allow_trading = allow_trading
    client.environment = "demo"
    session = _FakeSession(responses)
    client._session = session
    return client, session


def test_parse_token_id_valid():
    assert _parse_token_id("KXFOO-25:yes") == ("KXFOO-25", "yes")
    assert _parse_token_id("KXFOO-25:no") == ("KXFOO-25", "no")


def test_parse_token_id_rejects_malformed():
    with pytest.raises(KalshiTokenIdError):
        _parse_token_id("KXFOO-25")
    with pytest.raises(KalshiTokenIdError):
        _parse_token_id("KXFOO-25:maybe")


def test_get_mid_price_for_yes_and_no_sides():
    market_payload = {
        "market": {
            "yes_bid_dollars": "0.90",
            "yes_ask_dollars": "0.94",
            "no_bid_dollars": "0.06",
            "no_ask_dollars": "0.10",
        }
    }
    client, _ = _make_client({("GET", "/markets/KXFOO"): market_payload})

    assert client.get_mid_price("KXFOO:yes") == pytest.approx(0.92)
    assert client.get_mid_price("KXFOO:no") == pytest.approx(0.08)


def test_get_market_rules_returns_none_on_failure():
    client, _ = _make_client({})
    assert client.get_market_rules("KXFOO:yes") is None


def test_place_limit_order_buy_no_translates_to_sell_yes():
    client, session = _make_client({
        ("POST", "/portfolio/events/orders"): {"order": {"order_id": "o1", "status": "resting"}},
    })
    intent = LimitOrderIntent(token_id="KXFOO:no", side=Side.BUY, price=0.08, size=10)

    result = client.place_limit_order(intent)

    assert result.order_id == "o1"
    method, path, kwargs = session.calls[0]
    body = kwargs["json"]
    assert body["side"] == "ask"
    assert body["price"] == "0.92"
    assert body["time_in_force"] == "good_till_canceled"
    assert body["post_only"] is True


def test_place_limit_order_buy_yes_translates_directly():
    client, session = _make_client({
        ("POST", "/portfolio/events/orders"): {"order": {"order_id": "o2", "status": "resting"}},
    })
    intent = LimitOrderIntent(token_id="KXFOO:yes", side=Side.BUY, price=0.92, size=10)

    client.place_limit_order(intent)

    body = session.calls[0][2]["json"]
    assert body["side"] == "bid"
    assert body["price"] == "0.92"


def test_place_limit_order_sell_no_translates_to_buy_yes():
    client, session = _make_client({
        ("POST", "/portfolio/events/orders"): {"order": {"order_id": "o3", "status": "resting"}},
    })
    intent = LimitOrderIntent(token_id="KXFOO:no", side=Side.SELL, price=0.08, size=5)

    client.place_limit_order(intent)

    body = session.calls[0][2]["json"]
    assert body["side"] == "bid"
    assert body["price"] == "0.92"


def test_place_market_order_uses_ioc_without_post_only():
    client, session = _make_client({
        ("POST", "/portfolio/events/orders"): {"order": {"order_id": "o4", "status": "executed"}},
    })
    intent = MarketOrderIntent(token_id="KXFOO:no", side=Side.BUY, amount=1.0, reference_price=0.08)

    client.place_market_order(intent)

    body = session.calls[0][2]["json"]
    assert body["time_in_force"] == "immediate_or_cancel"
    assert "post_only" not in body


def test_place_order_raises_when_trading_disabled():
    client, _ = _make_client({}, allow_trading=False)
    intent = LimitOrderIntent(token_id="KXFOO:yes", side=Side.BUY, price=0.5, size=1)
    with pytest.raises(RuntimeError, match="disabled"):
        client.place_limit_order(intent)


def test_get_order_book_derives_asks_from_opposite_bids():
    orderbook_payload = {
        "orderbook": {
            "yes": [[90, 100], [89, 50]],
            "no": [[6, 200]],
        }
    }
    client, _ = _make_client({("GET", "/markets/KXFOO/orderbook"): orderbook_payload})

    book = client.get_order_book("KXFOO:no")

    assert book.bids[0].price == pytest.approx(0.06)
    assert book.bids[0].size == pytest.approx(200)
    assert book.asks[0].price == pytest.approx(0.10)
    assert book.asks[1].price == pytest.approx(0.11)


def test_get_collateral_balance_prefers_dollar_field():
    client, _ = _make_client({("GET", "/portfolio/balance"): {"balance_dollars": "42.50", "balance": 4250}})
    assert client.get_collateral_balance() == pytest.approx(42.50)


def test_get_collateral_balance_falls_back_to_cents():
    client, _ = _make_client({("GET", "/portfolio/balance"): {"balance": 4250}})
    assert client.get_collateral_balance() == pytest.approx(42.50)


def test_get_conditional_balance_splits_by_side():
    payload = {"market_positions": [{"ticker": "KXFOO", "position": -30}]}
    client, _ = _make_client({("GET", "/portfolio/positions"): payload})

    assert client.get_conditional_balance("KXFOO:no") == pytest.approx(30.0)
    assert client.get_conditional_balance("KXFOO:yes") == pytest.approx(0.0)


def test_cancel_order_respects_allow_trading():
    client, session = _make_client({("DELETE", "/portfolio/events/orders/o1"): {}}, allow_trading=False)
    assert client.cancel_order("o1") is False
    assert session.calls == []


def test_check_order_readiness_insufficient_balance():
    client, _ = _make_client({("GET", "/portfolio/balance"): {"balance_dollars": "1.00"}})
    intent = LimitOrderIntent(token_id="KXFOO:yes", side=Side.BUY, price=0.5, size=10)

    readiness = client.check_order_readiness(intent)

    assert readiness.ready is False
    assert readiness.balance == pytest.approx(1.0)
