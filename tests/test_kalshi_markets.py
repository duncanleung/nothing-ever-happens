import pytest
import requests

from bot.kalshi_markets import (
    KalshiMarket,
    build_kalshi_market,
    fetch_kalshi_markets,
    qualifies,
)


def test_build_kalshi_market_parses_dollar_fields():
    raw = {
        "ticker": "KXFOO-25-BAR",
        "event_ticker": "KXFOO-25",
        "title": "Will something happen?",
        "yes_ask_dollars": "0.93",
        "no_ask_dollars": "0.08",
        "volume_24h": "1200",
        "open_interest": "500",
        "close_time": "2026-09-01T00:00:00Z",
    }

    market = build_kalshi_market(raw, series_ticker="KXFOO", category="politics")

    assert market.ticker == "KXFOO-25-BAR"
    assert market.yes_price == pytest.approx(0.93)
    assert market.no_price == pytest.approx(0.08)
    assert market.volume == pytest.approx(1200)
    assert market.open_interest == pytest.approx(500)
    assert market.yes_token_id == "KXFOO-25-BAR:yes"
    assert market.no_token_id == "KXFOO-25-BAR:no"


def test_build_kalshi_market_returns_none_without_ticker():
    assert build_kalshi_market({}, series_ticker="KXFOO", category="politics") is None


def test_qualifies_filters_on_price_and_volume():
    cheap = KalshiMarket(
        ticker="A", event_ticker="", series_ticker="", question="",
        yes_price=0.92, no_price=0.08, volume=100, open_interest=0,
        close_time="", category="politics",
    )
    expensive = KalshiMarket(
        ticker="B", event_ticker="", series_ticker="", question="",
        yes_price=0.5, no_price=0.5, volume=100, open_interest=0,
        close_time="", category="politics",
    )
    zero_no = KalshiMarket(
        ticker="C", event_ticker="", series_ticker="", question="",
        yes_price=1.0, no_price=0.0, volume=100, open_interest=0,
        close_time="", category="politics",
    )
    low_volume = KalshiMarket(
        ticker="D", event_ticker="", series_ticker="", question="",
        yes_price=0.92, no_price=0.08, volume=1, open_interest=0,
        close_time="", category="politics",
    )

    assert qualifies(cheap, max_no_price=0.10, min_volume=10) is True
    assert qualifies(expensive, max_no_price=0.10, min_volume=10) is False
    assert qualifies(zero_no, max_no_price=0.10, min_volume=10) is False
    assert qualifies(low_volume, max_no_price=0.10, min_volume=10) is False


class _FakeResponse:
    def __init__(self, payload):
        self._payload = payload

    def raise_for_status(self):
        return None

    def json(self):
        return self._payload


def test_fetch_kalshi_markets_filters_categories_and_price(monkeypatch):
    responses = {
        "/series": {"series": [
            {"ticker": "KXPOL", "category": "Politics"},
            {"ticker": "KXCRYPTO", "category": "Crypto"},
            {"ticker": "KXUNKNOWN", "category": "UnknownCategory"},
        ], "cursor": ""},
        "/events": {"events": [
            {"markets": [
                {"ticker": "KXPOL-A", "event_ticker": "KXPOL-EVT", "title": "Q1",
                 "yes_ask_dollars": "0.95", "no_ask_dollars": "0.05", "volume_24h": "200"},
                {"ticker": "KXPOL-B", "event_ticker": "KXPOL-EVT", "title": "Q2",
                 "yes_ask_dollars": "0.50", "no_ask_dollars": "0.50", "volume_24h": "200"},
            ]}
        ], "cursor": ""},
    }

    def fake_get(url, params=None, timeout=None):
        for path, payload in responses.items():
            if url.endswith(path):
                return _FakeResponse(payload)
        raise AssertionError(f"unexpected url {url}")

    monkeypatch.setattr("bot.kalshi_markets.requests.get", fake_get)

    markets = fetch_kalshi_markets("https://example.test", max_no_price=0.10, min_volume=0.0)

    assert [m.ticker for m in markets] == ["KXPOL-A"]
    assert markets[0].category == "politics"


def test_fetch_kalshi_markets_skips_series_on_fetch_error(monkeypatch):
    def fake_get(url, params=None, timeout=None):
        if url.endswith("/series"):
            return _FakeResponse({"series": [{"ticker": "KXPOL", "category": "Politics"}], "cursor": ""})
        raise requests.RequestException("boom")

    monkeypatch.setattr("bot.kalshi_markets.requests.get", fake_get)

    markets = fetch_kalshi_markets("https://example.test")

    assert markets == []
