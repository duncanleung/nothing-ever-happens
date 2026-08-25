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

logger = logging.getLogger(__name__)

TICK_SIZE = 0.01
MIN_ORDER_SIZE = 0.01
SELF_TRADE_PREVENTION_TYPE = "taker_at_cross"


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
        ticker, side = _parse_token_id(token_id)
        try:
            payload = self._session.request_json("GET", "/portfolio/positions", params={"ticker": ticker})
        except Exception as exc:
            logger.warning("get_conditional_balance failed", extra={"token_id": token_id, "error": str(exc)})
            return 0.0
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

        logger.info(
            "kalshi_post_order_response",
            extra={"order_id": order_id, "status": status, "ticker": ticker, "side": kalshi_side},
        )
        return OrderResult(order_id=order_id, status=status, raw=response)

    def _parse_open_order(self, raw: dict[str, Any], *, token_id: str, expected_side: str) -> OpenOrder | None:
        kalshi_side = str(raw.get("side") or "").lower()
        raw_side = "yes" if kalshi_side == "bid" else "no"
        if raw_side != expected_side:
            return None

        order_id = str(raw.get("order_id") or raw.get("id") or "")
        if not order_id:
            return None

        raw_price = _dollar_field(raw, "yes_price_dollars" if kalshi_side == "bid" else "no_price_dollars", "price")
        our_price = raw_price if expected_side == "yes" else (1.0 - raw_price if raw_price else 0.0)
        our_side_enum = Side.BUY if kalshi_side == "bid" else Side.SELL

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
        kalshi_side = str(raw.get("side") or "").lower()
        raw_side = "yes" if kalshi_side == "bid" else "no"
        if raw_side != expected_side:
            return None

        order_id = str(raw.get("order_id") or raw.get("id") or "")
        if not order_id:
            return None

        raw_price = _dollar_field(raw, "yes_price_dollars" if kalshi_side == "bid" else "no_price_dollars", "price")
        our_price = raw_price if expected_side == "yes" else (1.0 - raw_price if raw_price else 0.0)
        size = float(raw.get("filled_count", raw.get("count", 0)) or 0)
        if size <= 0:
            return None

        return Trade(
            trade_id=str(raw.get("trade_id") or order_id),
            order_id=order_id,
            token_id=token_id,
            side=Side.BUY if kalshi_side == "bid" else Side.SELL,
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
