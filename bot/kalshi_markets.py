"""Kalshi market discovery — scans for longshot 'No' contracts in emotional categories.

Mirrors bot/standalone_markets.py's role for Polymarket, but walks Kalshi's
category -> series -> event -> market hierarchy. GET /series, /events, and
/markets are public discovery endpoints and do not require RSA-PSS signing
(unlike bot/exchange/kalshi_auth.py, which signs the authenticated
portfolio/order endpoints).

Direction mapping: bot/strategy/nothing_happens.py enters a position when
``no_ask <= max_entry_price`` (buy No while it's cheap — a contrarian
longshot bet). This scanner filters the same way: qualifying markets have
``no_price <= max_no_price``.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Iterable

import requests

logger = logging.getLogger(__name__)

DEMO_BASE_URL = "https://external-api.demo.kalshi.co/trade-api/v2"
PROD_BASE_URL = "https://external-api.kalshi.com/trade-api/v2"
DEFAULT_TIMEOUT_SEC = 10.0
PAGE_LIMIT = 200

# Kalshi's own series categories. Sports is included per QUA-319 research —
# unlike the Polymarket scanner (bot/standalone_markets.py), which excludes
# sports because fast-resolving in-game markets behave differently.
TARGET_CATEGORIES = {
    "politics",
    "entertainment",
    "sports",
    "culture",
    "weather",
    "geopolitics",
}
EXCLUDED_CATEGORIES = {"finance", "crypto"}

DEFAULT_MAX_NO_PRICE = 0.10
DEFAULT_MIN_VOLUME = 0.0


class KalshiMarketFetchError(RuntimeError):
    pass


@dataclass(frozen=True)
class KalshiMarket:
    ticker: str
    event_ticker: str
    series_ticker: str
    question: str
    yes_price: float
    no_price: float
    volume: float
    open_interest: float
    close_time: str
    category: str

    @property
    def yes_token_id(self) -> str:
        return f"{self.ticker}:yes"

    @property
    def no_token_id(self) -> str:
        return f"{self.ticker}:no"


def _get_json(
    base_url: str,
    path: str,
    params: dict[str, Any] | None = None,
    timeout: float = DEFAULT_TIMEOUT_SEC,
) -> dict[str, Any]:
    try:
        response = requests.get(f"{base_url}{path}", params=params, timeout=timeout)
        response.raise_for_status()
    except requests.RequestException as exc:
        raise KalshiMarketFetchError(f"kalshi_fetch_failed path={path} err={exc}") from exc
    return response.json()


def _paginate(base_url: str, path: str, params: dict[str, Any], list_key: str) -> Iterable[dict[str, Any]]:
    cursor = ""
    while True:
        page_params = dict(params)
        if cursor:
            page_params["cursor"] = cursor
        payload = _get_json(base_url, path, params=page_params)
        items = payload.get(list_key) or []
        yield from items
        cursor = str(payload.get("cursor") or "")
        if not cursor or not items:
            return


def fetch_series(base_url: str = DEMO_BASE_URL) -> list[dict[str, Any]]:
    return list(_paginate(base_url, "/series", {"limit": PAGE_LIMIT}, "series"))


def _series_category(series: dict[str, Any]) -> str:
    return str(series.get("category") or "").strip().lower()


def _target_series_tickers(all_series: list[dict[str, Any]]) -> list[str]:
    tickers: list[str] = []
    for series in all_series:
        category = _series_category(series)
        if category in EXCLUDED_CATEGORIES:
            continue
        if category not in TARGET_CATEGORIES:
            continue
        ticker = series.get("ticker")
        if ticker:
            tickers.append(str(ticker))
    return tickers


def _parse_dollar_field(market: dict[str, Any], *keys: str) -> float:
    for key in keys:
        raw = market.get(key)
        if raw is None:
            continue
        try:
            return float(raw)
        except (TypeError, ValueError):
            continue
    return 0.0


def build_kalshi_market(market: dict[str, Any], *, series_ticker: str, category: str) -> KalshiMarket | None:
    ticker = str(market.get("ticker") or "")
    if not ticker:
        return None
    return KalshiMarket(
        ticker=ticker,
        event_ticker=str(market.get("event_ticker") or ""),
        series_ticker=series_ticker,
        question=str(market.get("title") or market.get("subtitle") or ""),
        yes_price=_parse_dollar_field(market, "yes_ask_dollars", "yes_ask"),
        no_price=_parse_dollar_field(market, "no_ask_dollars", "no_ask"),
        volume=_parse_dollar_field(market, "volume_24h", "volume_24h_fp", "volume"),
        open_interest=_parse_dollar_field(market, "open_interest_fp", "open_interest"),
        close_time=str(market.get("close_time") or ""),
        category=category,
    )


def qualifies(market: KalshiMarket, *, max_no_price: float, min_volume: float) -> bool:
    if market.no_price <= 0 or market.no_price > max_no_price:
        return False
    if market.volume < min_volume:
        return False
    return True


def fetch_kalshi_markets(
    base_url: str = DEMO_BASE_URL,
    *,
    max_no_price: float = DEFAULT_MAX_NO_PRICE,
    min_volume: float = DEFAULT_MIN_VOLUME,
) -> list[KalshiMarket]:
    """Scan open Kalshi markets in target categories for cheap No positions."""
    all_series = fetch_series(base_url)
    series_by_ticker = {str(s.get("ticker")): s for s in all_series if s.get("ticker")}
    target_tickers = _target_series_tickers(all_series)

    markets: list[KalshiMarket] = []
    for series_ticker in target_tickers:
        category = _series_category(series_by_ticker.get(series_ticker, {}))
        try:
            events = _paginate(
                base_url,
                "/events",
                {
                    "series_ticker": series_ticker,
                    "status": "open",
                    "with_nested_markets": "true",
                    "limit": PAGE_LIMIT,
                },
                "events",
            )
            for event in events:
                for raw_market in event.get("markets") or []:
                    built = build_kalshi_market(raw_market, series_ticker=series_ticker, category=category)
                    if built is not None and qualifies(built, max_no_price=max_no_price, min_volume=min_volume):
                        markets.append(built)
        except KalshiMarketFetchError as exc:
            logger.warning("kalshi_series_scan_failed", extra={"series_ticker": series_ticker, "error": str(exc)})
            continue

    markets.sort(key=lambda m: m.volume, reverse=True)
    return markets
