import asyncio

import pytest
import requests

from bot.kalshi_markets import (
    KalshiMarket,
    build_kalshi_market,
    fetch_kalshi_markets,
    fetch_kalshi_markets_via_series_walk,
    kalshi_market_to_standalone,
    make_kalshi_market_fetcher,
    qualifies,
)


def test_build_kalshi_market_derives_no_price_from_yes_bid():
    raw = {
        "ticker": "KXFOO-25-BAR",
        "event_ticker": "KXFOO-25",
        "title": "Will something happen?",
        "yes_bid_dollars": "0.92",
        "yes_ask_dollars": "0.95",
        # A stale/unset no_ask_dollars — Kalshi's book is Yes-only, so this
        # field sits at 1.0000 whenever nobody has posted a real No ask. It
        # must NOT be read directly.
        "no_ask_dollars": "1.0000",
        "volume_24h": "1200",
        "open_interest": "500",
        "close_time": "2026-09-01T00:00:00Z",
    }

    market = build_kalshi_market(raw, series_ticker="KXFOO", category="politics")

    assert market.ticker == "KXFOO-25-BAR"
    assert market.yes_price == pytest.approx(0.95)
    assert market.no_price == pytest.approx(0.08)  # 1 - yes_bid_dollars
    assert market.volume == pytest.approx(1200)
    assert market.open_interest == pytest.approx(500)
    assert market.yes_token_id == "KXFOO-25-BAR:yes"
    assert market.no_token_id == "KXFOO-25-BAR:no"


def test_build_kalshi_market_returns_none_without_ticker():
    assert build_kalshi_market({}, series_ticker="KXFOO", category="politics") is None


def test_build_kalshi_market_skips_mve_parlay_tickers():
    raw = {
        "ticker": "KXMVECROSSCATEGORY-25-ABC",
        "event_ticker": "KXMVECROSSCATEGORY-25",
        "title": "Combo parlay",
        "yes_bid_dollars": "0.00",
        "yes_ask_dollars": "0.00",
        "no_ask_dollars": "1.0000",
    }

    assert build_kalshi_market(raw, series_ticker="KXMVECROSSCATEGORY", category="politics") is None


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
    def __init__(self, payload, status_code=200):
        self._payload = payload
        self.status_code = status_code

    def raise_for_status(self):
        return None

    def json(self):
        return self._payload


def test_fetch_kalshi_markets_paginates_events_endpoint(monkeypatch):
    responses = {
        "/events": {
            "events": [
                {
                    "ticker": "KXPOL-EVT",
                    "series_ticker": "KXPOL",
                    "category": "Politics",
                    "markets": [
                        {
                            "ticker": "KXPOL-A",
                            "event_ticker": "KXPOL-EVT",
                            "title": "Q1",
                            "yes_bid_dollars": "0.92",
                            "yes_ask_dollars": "0.95",
                            "volume_24h": "200",
                        },
                        {
                            "ticker": "KXPOL-B",
                            "event_ticker": "KXPOL-EVT",
                            "title": "Q2",
                            "yes_bid_dollars": "0.50",
                            "yes_ask_dollars": "0.50",
                            "volume_24h": "200",
                        },
                        {
                            "ticker": "KXMVECROSSCATEGORY-C",
                            "event_ticker": "KXMVECROSSCATEGORY-EVT",
                            "title": "Combo",
                            "yes_bid_dollars": "0.00",
                            "yes_ask_dollars": "0.00",
                            "volume_24h": "0",
                        },
                    ],
                },
            ],
            "cursor": "",
        },
    }
    seen_urls = []

    def fake_get(url, params=None, timeout=None):
        seen_urls.append((url, params))
        for path, payload in responses.items():
            if url.endswith(path):
                return _FakeResponse(payload)
        raise AssertionError(f"unexpected url {url}")

    monkeypatch.setattr("bot.kalshi_markets.requests.get", fake_get)

    result = fetch_kalshi_markets("https://example.test", max_no_price=0.10, min_volume=0.0)

    assert result.is_complete is True
    assert [m.ticker for m in result.markets] == ["KXPOL-A"]
    assert result.markets[0].no_price == pytest.approx(0.08)
    assert result.markets[0].category == "politics"
    assert all(url.endswith("/events") for url, _ in seen_urls)
    assert any(params.get("with_nested_markets") == "true" for _, params in seen_urls)


def test_fetch_kalshi_markets_flags_incomplete_on_fetch_error(monkeypatch):
    def fake_get(url, params=None, timeout=None):
        raise requests.RequestException("boom")

    monkeypatch.setattr("bot.kalshi_markets.requests.get", fake_get)

    result = fetch_kalshi_markets("https://example.test")

    assert result.markets == []
    assert result.is_complete is False


def test_fetch_kalshi_markets_via_series_walk_filters_categories_and_price(monkeypatch):
    responses = {
        "/series": {"series": [
            {"ticker": "KXPOL", "category": "Politics"},
            {"ticker": "KXCRYPTO", "category": "Crypto"},
            {"ticker": "KXUNKNOWN", "category": "UnknownCategory"},
        ], "cursor": ""},
        "/events": {"events": [
            {"markets": [
                {"ticker": "KXPOL-A", "event_ticker": "KXPOL-EVT", "title": "Q1",
                 "yes_bid_dollars": "0.92", "yes_ask_dollars": "0.95", "volume_24h": "200"},
                {"ticker": "KXPOL-B", "event_ticker": "KXPOL-EVT", "title": "Q2",
                 "yes_bid_dollars": "0.50", "yes_ask_dollars": "0.50", "volume_24h": "200"},
            ]}
        ], "cursor": ""},
    }

    def fake_get(url, params=None, timeout=None):
        for path, payload in responses.items():
            if url.endswith(path):
                return _FakeResponse(payload)
        raise AssertionError(f"unexpected url {url}")

    monkeypatch.setattr("bot.kalshi_markets.requests.get", fake_get)

    result = fetch_kalshi_markets_via_series_walk("https://example.test", max_no_price=0.10, min_volume=0.0)

    assert result.is_complete is True
    assert [m.ticker for m in result.markets] == ["KXPOL-A"]
    assert result.markets[0].category == "politics"


def test_fetch_kalshi_markets_via_series_walk_flags_incomplete_on_series_fetch_error(monkeypatch):
    def fake_get(url, params=None, timeout=None):
        if url.endswith("/series"):
            return _FakeResponse({"series": [{"ticker": "KXPOL", "category": "Politics"}], "cursor": ""})
        raise requests.RequestException("boom")

    monkeypatch.setattr("bot.kalshi_markets.requests.get", fake_get)

    result = fetch_kalshi_markets_via_series_walk("https://example.test")

    assert result.markets == []
    assert result.is_complete is False


def test_fetch_kalshi_markets_via_series_walk_flags_incomplete_when_series_list_fails(monkeypatch):
    def fake_get(url, params=None, timeout=None):
        raise requests.RequestException("boom")

    monkeypatch.setattr("bot.kalshi_markets.requests.get", fake_get)

    result = fetch_kalshi_markets_via_series_walk("https://example.test")

    assert result.markets == []
    assert result.is_complete is False


# --- KalshiMarket → StandaloneMarket adapter tests ---


def _sample_kalshi_market(**overrides) -> KalshiMarket:
    defaults = dict(
        ticker="KXPOL-26-ABC",
        event_ticker="KXPOL-26",
        series_ticker="KXPOL",
        question="Will something happen?",
        yes_price=0.92,
        no_price=0.08,
        volume=1500.0,
        open_interest=400.0,
        close_time="2026-09-01T23:59:00Z",
        category="politics",
    )
    defaults.update(overrides)
    return KalshiMarket(**defaults)


def test_kalshi_market_to_standalone_maps_all_fields():
    km = _sample_kalshi_market()
    sm = kalshi_market_to_standalone(km)

    assert sm.slug == "KXPOL-26-ABC"
    assert sm.condition_id == "KXPOL-26-ABC"
    assert sm.question == "Will something happen?"
    assert sm.yes_token_id == "KXPOL-26-ABC:yes"
    assert sm.no_token_id == "KXPOL-26-ABC:no"
    assert sm.yes_price == pytest.approx(0.92)
    assert sm.no_price == pytest.approx(0.08)
    assert sm.volume == pytest.approx(1500.0)
    assert sm.liquidity == 0.0
    assert sm.min_order_size == 1.0
    assert sm.end_date == "2026-09-01T23:59:00Z"
    assert sm.end_ts > 0
    assert sm.category == "politics"
    assert sm.event_slug == "KXPOL-26"


def test_kalshi_market_to_standalone_handles_empty_close_time():
    km = _sample_kalshi_market(close_time="")
    sm = kalshi_market_to_standalone(km)
    assert sm.end_ts == 0.0
    assert sm.end_date == ""


def test_make_kalshi_market_fetcher_returns_standalone_markets(monkeypatch):
    km = _sample_kalshi_market()

    def fake_get(url, params=None, timeout=None):
        return _FakeResponse({"events": [
            {
                "ticker": km.event_ticker,
                "series_ticker": km.series_ticker,
                "category": "Politics",
                "markets": [
                    {
                        "ticker": km.ticker,
                        "event_ticker": km.event_ticker,
                        "series_ticker": km.series_ticker,
                        "title": km.question,
                        "yes_bid_dollars": "0.92",
                        "yes_ask_dollars": "0.92",
                        "volume_24h": "1500",
                        "open_interest": "400",
                        "close_time": km.close_time,
                        "status": "open",
                    },
                ],
            },
        ], "cursor": ""})

    monkeypatch.setattr("bot.kalshi_markets.requests.get", fake_get)

    fetcher = make_kalshi_market_fetcher("https://example.test", max_no_price=0.10)
    markets = asyncio.run(fetcher(None))

    assert len(markets) == 1
    assert markets[0].slug == "KXPOL-26-ABC"
    assert markets[0].yes_token_id == "KXPOL-26-ABC:yes"


def test_make_kalshi_market_fetcher_returns_empty_when_no_qualifying(monkeypatch):
    def fake_get(url, params=None, timeout=None):
        return _FakeResponse({"events": [
            {
                "ticker": "KXFOO-26",
                "series_ticker": "KXFOO",
                "category": "Politics",
                "markets": [
                    {
                        "ticker": "KXFOO-26-X",
                        "event_ticker": "KXFOO-26",
                        "title": "Expensive market",
                        "yes_bid_dollars": "0.10",
                        "yes_ask_dollars": "0.15",
                        "volume_24h": "100",
                        "open_interest": "50",
                        "close_time": "2026-09-01T00:00:00Z",
                    },
                ],
            },
        ], "cursor": ""})

    monkeypatch.setattr("bot.kalshi_markets.requests.get", fake_get)

    fetcher = make_kalshi_market_fetcher("https://example.test", max_no_price=0.10)
    markets = asyncio.run(fetcher(None))

    assert markets == []
