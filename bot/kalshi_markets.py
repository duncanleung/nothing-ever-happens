"""Kalshi market discovery — scans for longshot 'No' contracts in emotional categories.

Mirrors bot/standalone_markets.py's role for Polymarket. The default path
(``fetch_kalshi_markets``) paginates GET /markets directly; ``fetch_kalshi_markets_via_series_walk``
walks Kalshi's category -> series -> event -> market hierarchy instead and is
kept as a category-filtered fallback (see its docstring for why it isn't the
default). GET /series, /events, and /markets are public discovery endpoints
and do not require RSA-PSS signing (unlike bot/exchange/kalshi_auth.py, which
signs the authenticated portfolio/order endpoints).

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

# Kalshi's actual series/event category taxonomy — the original slugs here
# ("weather", "geopolitics", "culture", "finance") never matched anything
# Kalshi returns, so the series-walk fallback silently scanned zero series.
TARGET_CATEGORIES = {
    "politics",
    "entertainment",
    "sports",
    "social",
    "climate and weather",
    "world",
    "elections",
    "economics",
}
EXCLUDED_CATEGORIES = {"financials", "crypto"}

DEFAULT_MAX_NO_PRICE = 0.10
DEFAULT_MIN_VOLUME = 0.0

# Multi-variate-event combo/parlay tickers (e.g. "KXMVECROSSCATEGORY-...").
# These bundle several legs into one contract rather than trading as a single
# binary market, so their book is permanently quoted at no_ask=1.0000 /
# yes_ask=0.0000 — not a real price, just "no liquidity on this combo leg."
MVE_TICKER_PREFIX = "KXMVECROSSCATEGORY"


class KalshiMarketFetchError(RuntimeError):
    pass


@dataclass(frozen=True)
class KalshiMarket:
    """A single Kalshi binary market snapshot.

    Kalshi's order book is Yes-only: there is no resting "No ask" to read.
    ``no_price`` is therefore derived, not read directly, as
    ``1 - yes_bid_dollars`` — buying No is economically identical to selling
    Yes at the current Yes bid, so that bid (inverted) is the effective No
    cost. Reading ``no_ask_dollars`` directly is wrong: that field sits at
    1.0000 whenever nobody has posted an actual No-side ask, which is nearly
    always.
    """

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


def _is_mve_ticker(ticker: str) -> bool:
    return ticker.startswith(MVE_TICKER_PREFIX)


def build_kalshi_market(market: dict[str, Any], *, series_ticker: str, category: str) -> KalshiMarket | None:
    ticker = str(market.get("ticker") or "")
    if not ticker or _is_mve_ticker(ticker):
        return None
    yes_bid = _parse_dollar_field(market, "yes_bid_dollars", "yes_bid")
    return KalshiMarket(
        ticker=ticker,
        event_ticker=str(market.get("event_ticker") or ""),
        series_ticker=series_ticker,
        question=str(market.get("title") or market.get("subtitle") or ""),
        yes_price=_parse_dollar_field(market, "yes_ask_dollars", "yes_ask"),
        no_price=round(1.0 - yes_bid, 4),
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
    """Scan open Kalshi markets for cheap No positions.

    Paginates GET /markets?status=open directly — one call chain regardless
    of how many series/events exist. The series-walk approach
    (``fetch_kalshi_markets_via_series_walk``) instead calls /events once per
    target series; with 13k+ series on Kalshi (8k+ in target categories),
    that's thousands of calls and hits rate limits immediately.

    /markets doesn't carry a per-market category (category lives on the
    series, one more call away), so this path does not category-filter —
    every open, non-MVE, priced-within-range market qualifies regardless of
    category. Use the series-walk fallback if category filtering is required
    and the call volume is acceptable.
    """
    markets: list[KalshiMarket] = []
    try:
        raw_markets = _paginate(base_url, "/markets", {"status": "open", "limit": PAGE_LIMIT}, "markets")
        for raw_market in raw_markets:
            series_ticker = str(raw_market.get("series_ticker") or raw_market.get("event_ticker") or "")
            built = build_kalshi_market(raw_market, series_ticker=series_ticker, category="")
            if built is not None and qualifies(built, max_no_price=max_no_price, min_volume=min_volume):
                markets.append(built)
    except KalshiMarketFetchError as exc:
        logger.warning("kalshi_markets_scan_failed", extra={"error": str(exc)})

    markets.sort(key=lambda m: m.volume, reverse=True)
    return markets


def fetch_kalshi_markets_via_series_walk(
    base_url: str = DEMO_BASE_URL,
    *,
    max_no_price: float = DEFAULT_MAX_NO_PRICE,
    min_volume: float = DEFAULT_MIN_VOLUME,
) -> list[KalshiMarket]:
    """Category-filtered fallback: /series -> /events?series_ticker=X per target series.

    Kept for when category filtering matters more than call volume. Not the
    default — see ``fetch_kalshi_markets``'s docstring.
    """
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
