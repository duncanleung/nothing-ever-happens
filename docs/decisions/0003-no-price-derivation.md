---
id: ADR-0003
title: No-price derivation from the Yes-only order book
status: accepted
date: 2026-08-26
tags: [kalshi, exchange-client, order-book, pricing]
supersedes: []
related: [0002]
---

# 0003 — No-price derivation from the Yes-only order book

Date: 2026-08-26
Status: Accepted

## Context

Kalshi's market data is Yes-only: `GET /markets/{ticker}` returns
`yes_bid_dollars`/`yes_ask_dollars`, and `GET /markets/{ticker}/orderbook`
returns resting bid levels for `yes` and `no` separately, with no asks on
either side (`.ai/status/qua319-research.md` gotcha #4: "Order book is
bids-only — calculate asks: `yes_ask = 100 - best_no_bid`").

Critically, the `no_bid_dollars`/`no_ask_dollars` fields on the
`/markets/{ticker}` snapshot are not reliable resting prices — per
`_bid_ask_for_side` in `bot/exchange/kalshi.py:322-334`, they sit at
`0.0000`/`1.0000` whenever no one has posted an actual No-side order,
which is nearly always the case. A naive read of those fields would make
every quiet market look like No is worth $0 or $1, which is wrong and
would corrupt any strategy decision based on it.

The correct derivation instead goes through the Yes side, which is where
liquidity actually is:

```
no_bid = 1 - yes_ask
no_ask = 1 - yes_bid
```

This same principle applies in `get_order_book` (`kalshi.py:204-226`): the
order book only carries resting bids on each side, so asks are derived from
the opposite side's bids (`_derive_asks`), e.g. `yes_ask = 1 - best_no_bid`.

## Decision

Never read Kalshi's `no_bid_dollars`/`no_ask_dollars` snapshot fields (or
treat a missing No-side book level as "no interest") as the No price.
Always derive No prices from the Yes side:
`effective_no_bid = 1 - yes_ask_dollars`,
`effective_no_ask = 1 - yes_bid_dollars` (equivalently,
`effective_no_price = 1 - yes_bid_dollars` for the mid/quote case used by
`get_mid_price`). Apply the same bid/ask derivation symmetrically in the
order book endpoint: derive each side's asks from the opposite side's
resting bids.

## Rejected Alternatives

- **Read `no_bid_dollars`/`no_ask_dollars` directly from the market
  snapshot.** Rejected — these fields default to `0.0000`/`1.0000` when no
  one has posted a resting No order, which is the common case; using them
  directly would make quiet markets appear to have a worthless or
  risk-free No side, corrupting price checks and order placement.
- **Treat an empty No-side order book level as "no No liquidity" and skip
  the market.** Rejected — Kalshi's No liquidity is a mirror of Yes
  liquidity by construction (buying No == selling Yes and vice versa), so
  an empty No book does not mean no liquidity exists; it means the Yes
  side must be consulted instead.

## Rationale

- Kalshi's contracts are binary and complementary (Yes + No = $1), so the
  Yes side's bid/ask always implies a valid No bid/ask even when no one has
  traded No directly — deriving from Yes is not an approximation, it is the
  correct price by construction.
- This keeps `KalshiExchangeClient` self-sufficient: callers (the strategy
  layer) get a correct `effective_no_price` without needing to know that
  Kalshi's No-side fields are unreliable.

## Consequences

- Any future Kalshi read path that surfaces a price (new market data
  method, new field in an existing response) must apply this same
  Yes-side derivation for No, or it will silently reintroduce the
  0.0000/1.0000 bug for quiet markets.
- The derivation depends on Kalshi's Yes/No complementary pricing
  invariant (Yes + No = $1) continuing to hold; if Kalshi ever introduces
  fees or spreads that break that invariant at the API level, this formula
  would need to be revisited.
- `_derive_asks` in `get_order_book` rounds to the nearest cent
  (`round(1.0 - level.price, 2)`), which is consistent with Kalshi's
  `TICK_SIZE = 0.01`.

## Revisit Triggers

- Kalshi starts exposing a genuinely reliable No-side book (e.g. real
  resting No bids/asks that reflect actual liquidity, not just
  Yes-mirrored liquidity).
- Kalshi's Yes/No complementary pricing invariant changes (e.g. a market
  type where Yes + No != $1).
- The unverified response-shape caveat in the `kalshi.py` module docstring
  is resolved by a confirmed demo-API run and reveals different field
  names or semantics than assumed here.
