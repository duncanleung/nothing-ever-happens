---
id: ADR-0002
title: Direction mapping — buy No via Kalshi ask side
status: accepted
date: 2026-08-26
tags: [kalshi, exchange-client, order-strategy, direction-mapping]
supersedes: []
related: [0003]
---

# 0002 — Direction mapping — buy No via Kalshi ask side

Date: 2026-08-26
Status: Accepted

## Context

The bot's core strategy (`bot/strategy/nothing_happens.py`) is contrarian
longshot selling: it reads a market's No-side order book and buys No when
the No ask is cheap. On Polymarket, Yes and No are separate tradeable
tokens, so "buy No" is just a normal buy order against the No token's book.

Kalshi has no separate Yes/No token ids — a single market ticker trades on
one Yes-only order book (`bot/exchange/kalshi.py` module docstring). There
is no native "No" side to place a bid against. `KalshiExchangeClient`
encodes the Protocol's `token_id` as a composite `"{ticker}:yes"` /
`"{ticker}:no"` string and must translate our `Side.BUY`/`Side.SELL`-on-a-
side model onto Kalshi's bid/ask-on-Yes model before every order.

The forward mapping implemented in `_place_order` (`bot/exchange/kalshi.py:336-398`):

```
yes token, BUY  -> kalshi side="bid", price=price
yes token, SELL -> kalshi side="ask", price=price
no token,  BUY  -> kalshi side="ask", price=1-price   (sell Yes == buy No)
no token,  SELL -> kalshi side="bid", price=1-price   (buy Yes == sell No)
```

For the strategy's actual use case — buying No — this resolves to
`side="ask"`, `price = 1 - no_price`, with `post_only=true` enforced by
`place_limit_order` (the only path the strategy uses to enter positions).

This mapping is not just an order-placement detail: it also has to be
inverted everywhere Kalshi returns order/trade data, because a resting
"bid" is ambiguous between "yes+BUY" and "no+SELL" (and "ask" between
"yes+SELL" and "no+BUY") with no field in Kalshi's response to disambiguate
it. `_parse_open_order` and `_parse_trade` both resolve this using the
caller-supplied `expected_side` (from the token_id being queried), not by
inferring yes/no from the raw Kalshi side — inferring from `kalshi_side`
alone was an earlier bug (MF1/MF7: a resting "bid"/"ask" is genuinely
ambiguous between yes+BUY/no+SELL and yes+SELL/no+BUY with no field in
Kalshi's response to disambiguate; see `tests/test_kalshi.py` MF1/MF7
regression tests).

## Decision

To buy No on Kalshi, place an order with `side="ask"` (sell Yes) at
`price = 1 - no_price`, with `post_only=true`. Apply the symmetric inverse
mapping when parsing Kalshi's returned orders/trades back into our
Side/price model, always keyed off the token_id's yes/no suffix rather than
inferring it from Kalshi's raw side field.

## Rejected Alternatives

- **Infer yes/no from Kalshi's raw `side` field on parse.** Rejected — a
  resting "bid"/"ask" is genuinely ambiguous without knowing which
  conceptual side (yes or no) the caller is asking about; this was tried
  and produced incorrect order/trade side assignment (MF1/MF7, regression
  tests in `tests/test_kalshi.py`).
- **Represent No as a synthetic short position on Yes at the strategy
  layer**, keeping the exchange client Yes-only end to end. Rejected
  because it would require `bot/strategy/nothing_happens.py` (already
  written against a No-token-buy Polymarket mental model) to carry
  Kalshi-specific inversion logic, spreading the yes/no translation across
  two layers instead of containing it in the exchange client where the
  `ExchangeClient` Protocol boundary already exists.

## Rationale

- Keeps the yes/no ↔ bid/ask translation entirely inside
  `KalshiExchangeClient`, so `bot/strategy/nothing_happens.py` can call
  `place_limit_order`/`get_order_book`/etc. with the same
  Side.BUY/Side.SELL-on-a-token-id model it already uses for Polymarket —
  no strategy-layer branching per exchange.
- The composite `"{ticker}:yes"`/`"{ticker}:no"` token_id gives every
  method a caller-supplied signal for which conceptual side is meant,
  which is the only reliable disambiguator available (Kalshi's API does not
  expose one).
- `post_only=true` on the buy-No path is required by
  [[0004-maker-orders-only]] — the direction mapping and the maker-only
  requirement compose at the same call site (`place_limit_order`).

## Consequences

- Every Kalshi order placement and every parse of Kalshi order/trade data
  must apply this same forward/inverse mapping consistently; a mismatch
  between the two (e.g. a new read path that doesn't take
  `expected_side`) reintroduces the MF1/MF7 bug class.
- The mapping is a private implementation detail of
  `KalshiExchangeClient` — the strategy layer must not special-case Kalshi
  side semantics; if it starts to, that is a signal the abstraction has
  leaked and this ADR needs revisiting.
- Kalshi's own order object carries no "conceptual side" tag, so this
  mapping is permanently dependent on the caller always supplying the
  correct token_id/expected_side; there is no way to independently verify a
  parsed order's yes/no side after the fact from Kalshi's data alone.

### Known Exception

`get_order` (`bot/exchange/kalshi.py:124-135`) cannot follow the
`expected_side`-from-token_id pattern this ADR mandates: the `ExchangeClient`
Protocol's `get_order` signature takes only an `order_id`, with no token_id
for the caller to supply. To recover a `token_id` to pass into
`_parse_open_order`, `get_order` falls back to exactly the rejected
alternative above — inferring yes/no from Kalshi's raw `side` field
(`kalshi_side == "bid"` → `"yes"`, else `"no"`). This carries the same
ambiguity risk the Rejected Alternatives section describes: a resting "bid"
is genuinely indistinguishable between yes+BUY and no+SELL from Kalshi's
data alone, so `get_order` can misreport the conceptual side for an order
placed as no+SELL (or yes+SELL misread as no+BUY) when accessed by
order_id. This exception is scoped to `get_order` only; every other read
path (`get_open_orders`, `get_trades`) keys off the token_id-derived
`expected_side` as required.

## Revisit Triggers

- Kalshi adds a native No-side representation or a side-disambiguating
  field to its API responses.
- The strategy layer needs to hold both Yes and No positions on the same
  ticker simultaneously (the current inversion model assumes one
  conceptual side per ticker per caller).
- A new Kalshi read/write path is added that doesn't fit the
  `expected_side`-keyed pattern used by `_parse_open_order`/`_parse_trade`.
