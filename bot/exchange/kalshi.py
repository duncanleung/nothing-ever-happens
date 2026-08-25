"""KalshiExchangeClient — implements the ExchangeClient Protocol against Kalshi's trade API v2.

Kalshi has no separate Yes/No token ids — one market ticker trades on a
single, Yes-only order book. This client encodes the Protocol's ``token_id``
as a composite ``"{ticker}:yes"`` / ``"{ticker}:no"`` string (see
bot/kalshi_markets.py, which produces these via KalshiMarket.yes_token_id /
.no_token_id) and translates between our Side.BUY/SELL-on-a-side model and
Kalshi's bid/ask-on-Yes model:

    yes token, BUY  -> kalshi side="bid", price=price          (buy Yes)
    yes token, SELL -> kalshi side="ask", price=price          (sell Yes)
    no token,  BUY  -> kalshi side="ask", price=1-price        (sell Yes == buy No)
    no token,  SELL -> kalshi side="bid", price=1-price        (buy Yes == sell No)

This mirrors bot/strategy/nothing_happens.py's use of the Polymarket client:
it reads no_token_id's order book and buys No when the No ask is cheap
(contrarian longshot). See .ai/status/qua319-research.md section 3.

NOTE: field names for order/trade/position response bodies below follow the
QUA-319 research notes, not a verified live response. bootstrap_live_trading
should be run against the demo API to confirm response shapes before this
client is used for live trading.
"""

from __future__ import annotations

import logging
import time
from typing import Any

from bot.exchange.kalshi_auth import DEMO_BASE_URL, PROD_BASE_URL, KalshiAuthSession
from bot.models import (
    LimitOrderIntent,
    MarketOrderIntent,
    MarketRules,
    OpenOrder,
    OrderBookLevel,
    OrderBookSnapshot,
    OrderReadiness,
    OrderResult,
    Side,
    Trade,
)
from bot.order_status import normalize_order_status

logger = logging.getLogger(__name__)

TICK_SIZE = 0.01
MIN_ORDER_SIZE = 0.01
SELF_TRADE_PREVENTION_TYPE = "taker_at_cross"

# Mirrors nothing_happens.SUCCESS_ORDER_STATUSES (bot/strategy/nothing_happens.py)
# post-normalize_order_status(). Kept local rather than imported to avoid the
# exchange layer depending on the strategy layer.
_SUCCESS_STATUSES = {"matched", "filled", "simulated"}


class KalshiTokenIdError(ValueError):
    pass


def _parse_token_id(token_id: str) -> tuple[str, str]:
    ticker, _, side = token_id.rpartition(":")
    if not ticker or side not in {"yes", "no"}:
        raise KalshiTokenIdError(
            f"Expected token_id of the form '<ticker>:yes' or '<ticker>:no', got {token_id!r}"
        )
    return ticker, side


def _format_price(price: float) -> str:
    return f"{max(0.01, min(0.99, float(price))):.2f}"


class KalshiExchangeClient:
    def __init__(self, config: Any, allow_trading: bool) -> None:
        self.allow_trading = allow_trading
        self.environment = getattr(config, "environment", "demo")
        base_url = getattr(config, "base_url", None) or (
            PROD_BASE_URL if self.environment == "production" else DEMO_BASE_URL
        )
        self._session = KalshiAuthSession(
            api_key_id=config.api_key_id,
            private_key_path=config.private_key_path,
            base_url=base_url,
        )

    # -- Protocol methods ------------------------------------------------

    def bootstrap_live_trading(self, token_id: str | None = None) -> None:
        _ = token_id
        self._session.request_json("GET", "/portfolio/balance")

    def get_mid_price(self, token_id: str) -> float:
        _, side = _parse_token_id(token_id)
        market = self._get_market(token_id)
        bid, ask = self._bid_ask_for_side(market, side)
        return (bid + ask) / 2.0

    def get_market_rules(self, token_id: str) -> MarketRules | None:
        try:
            self._get_market(token_id)
        except Exception as exc:
            logger.warning("get_market_rules failed", extra={"token_id": token_id, "error": str(exc)})
            return None
        return MarketRules(tick_size=TICK_SIZE, min_order_size=MIN_ORDER_SIZE)

    def get_open_orders(self, token_id: str) -> list[OpenOrder]:
        ticker, side = _parse_token_id(token_id)
        try:
            payload = self._session.request_json(
                "GET", "/portfolio/orders", params={"ticker": ticker, "status": "resting"}
            )
        except Exception as exc:
            logger.warning("get_open_orders failed", extra={"token_id": token_id, "error": str(exc)})
            return []
        orders = []
        for raw in payload.get("orders") or []:
            parsed = self._parse_open_order(raw, token_id=token_id, expected_side=side)
            if parsed is not None:
                orders.append(parsed)
        return orders

    def get_order(self, order_id: str) -> OpenOrder | None:
        try:
            payload = self._session.request_json("GET", f"/portfolio/orders/{order_id}")
        except Exception as exc:
            logger.warning("get_order failed", extra={"order_id": order_id, "error": str(exc)})
            return None
        raw = payload.get("order", payload)
        ticker = str(raw.get("ticker") or "")
        kalshi_side = str(raw.get("side") or "").lower()
        our_side = "yes" if kalshi_side == "bid" else "no"
        token_id = f"{ticker}:{our_side}"
        return self._parse_open_order(raw, token_id=token_id, expected_side=our_side)

    def place_limit_order(self, order: LimitOrderIntent) -> OrderResult:
        if not self.allow_trading:
            raise RuntimeError("Order transmission is disabled")
        return self._place_order(order, time_in_force="good_till_canceled", post_only=True)

    def place_market_order(self, order: MarketOrderIntent) -> OrderResult:
        if not self.allow_trading:
            raise RuntimeError("Order transmission is disabled")
        return self._place_order(order, time_in_force="immediate_or_cancel", post_only=False)

    def get_trades(self, token_id: str, after_timestamp: int | None = None) -> list[Trade]:
        ticker, side = _parse_token_id(token_id)
        params: dict[str, Any] = {"ticker": ticker, "status": "executed"}
        try:
            payload = self._session.request_json("GET", "/portfolio/orders", params=params)
        except Exception as exc:
            logger.warning("get_trades failed", extra={"token_id": token_id, "error": str(exc)})
            return []

        trades: list[Trade] = []
        for raw in payload.get("orders") or []:
            trade = self._parse_trade(raw, token_id=token_id, expected_side=side)
            if trade is None:
                continue
            if after_timestamp is not None and trade.timestamp is not None:
                try:
                    if float(trade.timestamp) <= after_timestamp:
                        continue
                except (TypeError, ValueError):
                    pass
            trades.append(trade)
        return trades

    def check_order_readiness(self, order: LimitOrderIntent | MarketOrderIntent) -> OrderReadiness:
        try:
            balance = self.get_collateral_balance()
        except Exception as exc:
            logger.warning("readiness_check_failed", extra={"error": str(exc)})
            return OrderReadiness(False, "Could not verify Kalshi balance")

        required = order.notional if order.side == Side.BUY else 0.0
        if balance + 1e-9 < required:
            return OrderReadiness(False, "Insufficient balance for order", balance=balance)
        return OrderReadiness(True, "ok", balance=balance)

    def cancel_order(self, order_id: str) -> bool:
        if not self.allow_trading:
            return False
        try:
            self._session.request_json("DELETE", f"/portfolio/events/orders/{order_id}")
            return True
        except Exception as exc:
            logger.warning("cancel_order failed", extra={"order_id": order_id, "error": str(exc)})
            return False

    def cancel_all(self) -> bool:
        if not self.allow_trading:
            return False
        try:
            self._session.request_json("DELETE", "/portfolio/events/orders/batched")
            return True
        except Exception as exc:
            logger.warning("cancel_all failed", extra={"error": str(exc)})
            return False

    # -- Duck-typed extras -------------------------------------------------

    def get_order_book(self, token_id: str) -> OrderBookSnapshot:
        ticker, side = _parse_token_id(token_id)
        payload = self._session.request_json("GET", f"/markets/{ticker}/orderbook")
        book = payload.get("orderbook", payload)
        yes_bids = _parse_cents_levels(book.get("yes"))
        no_bids = _parse_cents_levels(book.get("no"))

        # The book only carries resting bids on each side — asks are derived
        # from the opposite side's bids (see research doc gotcha #4:
        # yes_ask = 1 - best_no_bid).
        if side == "yes":
            bids, asks = yes_bids, _derive_asks(no_bids)
        else:
            bids, asks = no_bids, _derive_asks(yes_bids)

        return OrderBookSnapshot(
            token_id=token_id,
            bids=tuple(sorted(bids, key=lambda level: level.price, reverse=True)),
            asks=tuple(sorted(asks, key=lambda level: level.price)),
            tick_size=TICK_SIZE,
            min_order_size=MIN_ORDER_SIZE,
            timestamp=int(time.time() * 1000),
        )

    def get_collateral_balance(self) -> float:
        payload = self._session.request_json("GET", "/portfolio/balance")
        if "balance_dollars" in payload:
            return float(payload["balance_dollars"])
        return float(payload.get("balance", 0)) / 100.0

    def get_conditional_balance(self, token_id: str) -> float:
        # Deliberately does not catch/return 0.0 on failure — a swallowed
        # exception here reads as "confirmed no position" to fill recovery
        # (bot/live_recovery.py:_process_ambiguous_row), which can trigger a
        # duplicate order for a position that actually exists (see MF3 in
        # .ai/status/qua319-review.md). Propagate and let the caller's own
        # try/except decide (it already treats failure as "retry later").
        ticker, side = _parse_token_id(token_id)
        payload = self._session.request_json("GET", "/portfolio/positions", params={"ticker": ticker})
        positions = payload.get("market_positions") or payload.get("positions") or []
        net_yes = 0.0
        for raw in positions:
            if str(raw.get("ticker") or "") != ticker:
                continue
            net_yes = float(raw.get("position", raw.get("position_fp", 0)) or 0)
            break
        return max(net_yes, 0.0) if side == "yes" else max(-net_yes, 0.0)

    def warm_token_cache(self, token_id: str) -> None:
        _ = token_id

    def prepare_sell(self, token_id: str) -> bool:
        _ = token_id
        return True

    def get_open_positions(self) -> list[dict[str, Any]]:
        """All open Kalshi positions, normalized to the dict shape
        bot.strategy.nothing_happens._position_snapshot_from_api expects.

        Used by NothingHappensRuntime._sync_positions in place of the
        Polymarket-only _fetch_open_positions/data-api path, so existing
        Kalshi holdings aren't invisible after a restart (see MF6 in
        .ai/status/qua319-review.md).

        avg_price/current_price come from a live get_mid_price() quote, not
        the original fill price — Kalshi's position endpoint doesn't expose
        an entry-price/PnL field we've verified live (see module docstring),
        so PnL is left at 0.0 rather than guessed. size and exposure
        (initialValue) are real and safe to use for duplicate-order
        prevention and risk-controller exposure tracking.
        """
        payload = self._session.request_json("GET", "/portfolio/positions")
        positions = payload.get("market_positions") or payload.get("positions") or []
        result: list[dict[str, Any]] = []
        for raw in positions:
            ticker = str(raw.get("ticker") or "")
            if not ticker:
                continue
            net_yes = float(raw.get("position", raw.get("position_fp", 0)) or 0)
            if net_yes == 0:
                continue
            side = "yes" if net_yes > 0 else "no"
            size = abs(net_yes)
            token_id = f"{ticker}:{side}"
            try:
                price = self.get_mid_price(token_id)
            except Exception as exc:
                logger.warning(
                    "get_open_positions_price_lookup_failed",
                    extra={"token_id": token_id, "error": str(exc)},
                )
                price = 0.0
            result.append(
                {
                    "slug": token_id,
                    "title": ticker,
                    "outcome": side,
                    "asset": token_id,
                    "conditionId": ticker,
                    "size": size,
                    "avgPrice": price,
                    "initialValue": size * price,
                    "curPrice": price,
                    "currentValue": size * price,
                    "cashPnl": 0.0,
                    "percentPnl": 0.0,
                    "endDate": "",
                }
            )
        return result

    # -- Internal helpers --------------------------------------------------

    def _get_market(self, token_id: str) -> dict[str, Any]:
        ticker, _ = _parse_token_id(token_id)
        payload = self._session.request_json("GET", f"/markets/{ticker}")
        return payload.get("market", payload)

    @staticmethod
    def _bid_ask_for_side(market: dict[str, Any], side: str) -> tuple[float, float]:
        # Kalshi's order book is Yes-only — no_bid_dollars/no_ask_dollars on
        # the /markets/{ticker} snapshot are not real resting prices (they
        # sit at 0.0000/1.0000 whenever no one has posted an actual No-side
        # order, which is nearly always). Derive them from the Yes side
        # instead: buying No == selling Yes, so no_bid = 1 - yes_ask and
        # no_ask = 1 - yes_bid.
        yes_bid = _dollar_field(market, "yes_bid_dollars", "yes_bid")
        yes_ask = _dollar_field(market, "yes_ask_dollars", "yes_ask")
        if side == "yes":
            return yes_bid, yes_ask
        return 1.0 - yes_ask, 1.0 - yes_bid

    def _place_order(
        self,
        order: LimitOrderIntent | MarketOrderIntent,
        *,
        time_in_force: str,
        post_only: bool,
    ) -> OrderResult:
        ticker, token_side = _parse_token_id(order.token_id)
        price, size = order.price, order.size
        if token_side == "yes":
            kalshi_side = "bid" if order.side == Side.BUY else "ask"
            kalshi_price = price
        else:
            kalshi_side = "ask" if order.side == Side.BUY else "bid"
            kalshi_price = 1.0 - price

        body: dict[str, Any] = {
            "ticker": ticker,
            "side": kalshi_side,
            "count": f"{size:.2f}",
            "price": _format_price(kalshi_price),
            "time_in_force": time_in_force,
            "self_trade_prevention_type": SELF_TRADE_PREVENTION_TYPE,
        }
        if post_only:
            body["post_only"] = True

        response = self._session.request_json("POST", "/portfolio/events/orders", json=body)
        raw = response.get("order", response)
        order_id = str(raw.get("order_id") or raw.get("id") or "")
        status = str(raw.get("status") or "submitted")

        # The strategy layer (bot/strategy/nothing_happens.py) reads
        # OrderResult.raw for Polymarket-shaped fill fields (`makingAmount`/
        # `takingAmount`, `_fill_price`), not Kalshi's. Inject best-effort
        # equivalents in `order`'s (our) price/side terms, not Kalshi's
        # Yes-denominated ones, so a real fill is recognized instead of
        # falling into the "unknown status" quarantine path. Kalshi's exact
        # fill-quantity field name is unverified (see module docstring) — try
        # a few plausible ones, then fall back to "fully filled" on a
        # success status rather than reporting zero shares for a real fill.
        filled = _dollar_field(raw, "filled_count", "fill_count")
        if filled <= 0:
            remaining = raw.get("remaining_count")
            initial = raw.get("initial_count", raw.get("count"))
            if remaining is not None and initial is not None:
                try:
                    filled = max(0.0, float(initial) - float(remaining))
                except (TypeError, ValueError):
                    filled = 0.0
        if filled <= 0 and normalize_order_status(status) in _SUCCESS_STATUSES:
            filled = size
        if filled > 0:
            response["takingAmount"] = str(filled)
            response["makingAmount"] = str(round(filled * price, 6))
            response["_fill_price"] = price
            response["_market_price"] = price

        logger.info(
            "kalshi_post_order_response",
            extra={"order_id": order_id, "status": status, "ticker": ticker, "side": kalshi_side, "filled": filled},
        )
        return OrderResult(order_id=order_id, status=status, raw=response)

    def _parse_open_order(self, raw: dict[str, Any], *, token_id: str, expected_side: str) -> OpenOrder | None:
        # Kalshi's book is Yes-only: a resting "bid" order IS ambiguous
        # between "yes+BUY" and "no+SELL" (and "ask" between "yes+SELL" and
        # "no+BUY") — see the module docstring's forward mapping, which
        # _place_order implements. Kalshi's own order object carries no
        # "conceptual side" tag to disambiguate; the caller-supplied
        # `expected_side` (from the token_id it asked about) is the only
        # signal available, so every order returned for this ticker is
        # interpreted through that lens rather than filtered by trying to
        # infer yes/no from kalshi_side alone (that inference was the bug —
        # see MF1/MF7 in .ai/status/qua319-review.md).
        kalshi_side = str(raw.get("side") or "").lower()
        if kalshi_side not in {"bid", "ask"}:
            return None

        order_id = str(raw.get("order_id") or raw.get("id") or "")
        if not order_id:
            return None

        raw_price = _dollar_field(raw, "yes_price_dollars", "price")
        our_price = raw_price if expected_side == "yes" else (1.0 - raw_price if raw_price else 0.0)
        if expected_side == "yes":
            our_side_enum = Side.BUY if kalshi_side == "bid" else Side.SELL
        else:
            our_side_enum = Side.SELL if kalshi_side == "bid" else Side.BUY

        remaining = raw.get("remaining_count")
        initial = raw.get("initial_count", raw.get("count"))
        size_matched = None
        if remaining is not None and initial is not None:
            try:
                size_matched = float(initial) - float(remaining)
            except (TypeError, ValueError):
                size_matched = None

        return OpenOrder(
            order_id=order_id,
            token_id=token_id,
            side=our_side_enum,
            price=our_price,
            size_matched=size_matched,
            original_size=_optional_float(initial),
            status=str(raw.get("status")) if raw.get("status") is not None else None,
        )

    def _parse_trade(self, raw: dict[str, Any], *, token_id: str, expected_side: str) -> Trade | None:
        # Same No-token inversion as _parse_open_order — see its comment.
        kalshi_side = str(raw.get("side") or "").lower()
        if kalshi_side not in {"bid", "ask"}:
            return None

        order_id = str(raw.get("order_id") or raw.get("id") or "")
        if not order_id:
            return None

        raw_price = _dollar_field(raw, "yes_price_dollars", "price")
        our_price = raw_price if expected_side == "yes" else (1.0 - raw_price if raw_price else 0.0)
        size = float(raw.get("filled_count", raw.get("count", 0)) or 0)
        if size <= 0:
            return None

        if expected_side == "yes":
            our_side_enum = Side.BUY if kalshi_side == "bid" else Side.SELL
        else:
            our_side_enum = Side.SELL if kalshi_side == "bid" else Side.BUY

        return Trade(
            trade_id=str(raw.get("trade_id") or order_id),
            order_id=order_id,
            token_id=token_id,
            side=our_side_enum,
            price=our_price,
            size=size,
            fee=float(raw.get("taker_fees_dollars", raw.get("fees", 0)) or 0),
            timestamp=raw.get("last_update_time") or raw.get("created_time"),
        )


def _dollar_field(d: dict[str, Any], *keys: str) -> float:
    for key in keys:
        raw = d.get(key)
        if raw is None:
            continue
        try:
            return float(raw)
        except (TypeError, ValueError):
            continue
    return 0.0


def _optional_float(value: Any) -> float | None:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _parse_cents_levels(levels: Any) -> list[OrderBookLevel]:
    parsed: list[OrderBookLevel] = []
    for level in levels or []:
        try:
            price_cents, size = level[0], level[1]
            parsed.append(OrderBookLevel(price=float(price_cents) / 100.0, size=float(size)))
        except (TypeError, ValueError, IndexError):
            continue
    return parsed


def _derive_asks(opposite_bids: list[OrderBookLevel]) -> list[OrderBookLevel]:
    return [OrderBookLevel(price=round(1.0 - level.price, 2), size=level.size) for level in opposite_bids]
