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
            # Stale/unset No-side fields — Kalshi's book is Yes-only, so
            # these sit at 0.0000/1.0000 whenever nobody has posted a real
            # No-side order. get_mid_price must derive from the Yes side
            # instead of reading these directly.
            "no_bid_dollars": "0.00",
            "no_ask_dollars": "1.00",
        }
    }
    client, _ = _make_client({("GET", "/markets/KXFOO"): market_payload})

    assert client.get_mid_price("KXFOO:yes") == pytest.approx(0.92)
    # no_bid = 1 - yes_ask = 0.06, no_ask = 1 - yes_bid = 0.10 -> mid 0.08
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


def test_get_conditional_balance_propagates_failure_instead_of_returning_zero():
    # MF3: a swallowed exception here reads as "confirmed no position" to
    # fill recovery, which can trigger a duplicate order — must propagate.
    def _raise(kwargs):
        raise RuntimeError("network down")

    client, _ = _make_client({("GET", "/portfolio/positions"): _raise})

    with pytest.raises(RuntimeError, match="network down"):
        client.get_conditional_balance("KXFOO:no")


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


# -- MF1 / MF7: No-token side inversion in the order/trade parsers ---------
#
# Forward mapping (see _place_order / module docstring):
#   no+BUY  -> kalshi side="ask"     no+SELL -> kalshi side="bid"
#   yes+BUY -> kalshi side="bid"     yes+SELL -> kalshi side="ask"
# A resting "bid" is genuinely ambiguous between yes+BUY and no+SELL (same
# for "ask" between yes+SELL and no+BUY) — Kalshi's own order object carries
# no tag to disambiguate, so the parser must interpret every order through
# the caller-supplied `expected_side` (from the token_id it asked about).


def test_get_open_orders_interprets_bid_as_no_sell_for_no_token():
    payload = {"orders": [{"order_id": "o1", "side": "bid", "price": "0.92", "count": "10"}]}
    client, _ = _make_client({("GET", "/portfolio/orders"): payload})

    orders = client.get_open_orders("KXFOO:no")

    assert len(orders) == 1
    assert orders[0].side == Side.SELL
    assert orders[0].price == pytest.approx(0.08)  # 1 - 0.92


def test_get_open_orders_interprets_ask_as_no_buy_for_no_token():
    payload = {"orders": [{"order_id": "o2", "side": "ask", "price": "0.92", "count": "10"}]}
    client, _ = _make_client({("GET", "/portfolio/orders"): payload})

    orders = client.get_open_orders("KXFOO:no")

    assert len(orders) == 1
    assert orders[0].side == Side.BUY
    assert orders[0].price == pytest.approx(0.08)


def test_get_open_orders_interprets_bid_as_yes_buy_for_yes_token():
    payload = {"orders": [{"order_id": "o3", "side": "bid", "price": "0.92", "count": "10"}]}
    client, _ = _make_client({("GET", "/portfolio/orders"): payload})

    orders = client.get_open_orders("KXFOO:yes")

    assert len(orders) == 1
    assert orders[0].side == Side.BUY
    assert orders[0].price == pytest.approx(0.92)


def test_get_trades_interprets_bid_as_no_sell_for_no_token():
    payload = {"orders": [{"order_id": "o1", "side": "bid", "price": "0.92", "filled_count": "10"}]}
    client, _ = _make_client({("GET", "/portfolio/orders"): payload})

    trades = client.get_trades("KXFOO:no")

    assert len(trades) == 1
    assert trades[0].side == Side.SELL
    assert trades[0].price == pytest.approx(0.08)


def test_get_trades_interprets_ask_as_no_buy_for_no_token():
    payload = {"orders": [{"order_id": "o2", "side": "ask", "price": "0.92", "filled_count": "10"}]}
    client, _ = _make_client({("GET", "/portfolio/orders"): payload})

    trades = client.get_trades("KXFOO:no")

    assert len(trades) == 1
    assert trades[0].side == Side.BUY
    assert trades[0].price == pytest.approx(0.08)


# -- MF8: order status / fill-field mapping ---------------------------------


def test_place_order_injects_fill_fields_on_executed_status():
    client, _ = _make_client({
        ("POST", "/portfolio/events/orders"): {
            "order": {"order_id": "o1", "status": "executed", "count": "10"}
        },
    })
    intent = LimitOrderIntent(token_id="KXFOO:no", side=Side.BUY, price=0.08, size=10)

    result = client.place_limit_order(intent)

    assert result.status == "executed"
    assert float(result.raw["takingAmount"]) == pytest.approx(10.0)
    assert float(result.raw["makingAmount"]) == pytest.approx(0.8)
    assert result.raw["_fill_price"] == pytest.approx(0.08)


def test_place_order_uses_reported_filled_count_when_partial():
    client, _ = _make_client({
        ("POST", "/portfolio/events/orders"): {
            "order": {"order_id": "o1", "status": "executed", "count": "10", "filled_count": "4"}
        },
    })
    intent = LimitOrderIntent(token_id="KXFOO:no", side=Side.BUY, price=0.08, size=10)

    result = client.place_limit_order(intent)

    assert result.raw["takingAmount"] == "4.0"


def test_place_order_does_not_inject_fill_fields_when_resting_unfilled():
    client, _ = _make_client({
        ("POST", "/portfolio/events/orders"): {
            "order": {"order_id": "o1", "status": "resting", "count": "10"}
        },
    })
    intent = LimitOrderIntent(token_id="KXFOO:no", side=Side.BUY, price=0.08, size=10)

    result = client.place_limit_order(intent)

    assert "takingAmount" not in result.raw
    assert result.status == "resting"


# -- MF6: get_open_positions --------------------------------------------


def test_get_open_positions_normalizes_and_splits_by_side(monkeypatch):
    payload = {
        "market_positions": [
            {"ticker": "KXFOO", "position": 15},
            {"ticker": "KXBAR", "position": -8},
            {"ticker": "KXBAZ", "position": 0},
        ]
    }
    client, _ = _make_client({("GET", "/portfolio/positions"): payload})
    monkeypatch.setattr(client, "get_mid_price", lambda token_id: 0.5)

    positions = client.get_open_positions()

    assert len(positions) == 2
    by_slug = {p["slug"]: p for p in positions}
    assert by_slug["KXFOO:yes"]["size"] == pytest.approx(15)
    assert by_slug["KXFOO:yes"]["outcome"] == "yes"
    assert by_slug["KXFOO:yes"]["initialValue"] == pytest.approx(7.5)
    assert by_slug["KXBAR:no"]["size"] == pytest.approx(8)
    assert by_slug["KXBAR:no"]["outcome"] == "no"


def test_get_open_positions_degrades_price_to_zero_on_lookup_failure(monkeypatch):
    payload = {"market_positions": [{"ticker": "KXFOO", "position": 5}]}
    client, _ = _make_client({("GET", "/portfolio/positions"): payload})

    def _raise(token_id):
        raise RuntimeError("no market data")

    monkeypatch.setattr(client, "get_mid_price", _raise)

    positions = client.get_open_positions()

    assert len(positions) == 1
    assert positions[0]["avgPrice"] == 0.0
    assert positions[0]["size"] == pytest.approx(5)
