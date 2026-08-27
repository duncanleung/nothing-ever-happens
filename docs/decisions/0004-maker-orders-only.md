---
id: ADR-0004
title: Maker orders only on Kalshi (post_only enforced)
status: accepted
date: 2026-08-26
tags: [kalshi, fees, order-strategy]
supersedes: []
related: [0002]
---

# 0004 — Maker orders only on Kalshi (post_only enforced)

Date: 2026-08-26
Status: Accepted

## Context

Kalshi charges taker fees at roughly 4x the maker rate for the price range
this bot targets. Per the QUA-320 fee analysis
(recorded in `.ai/status/qua319-research.md`, "Fee Structure" section):

```
Taker: 7% x p x (1-p) per contract
Maker: 1.75% x p x (1-p) per contract   (25% of taker)
```

At a 7c Yes price (the strategy's longshot-selling target range): taker fee
is $0.0046/contract, maker fee is $0.0011/contract. The strategy sells
longshot contracts for small per-contract edges, so a fee that is 4x larger
at the same fill price can consume a large share (or all) of the edge the
strategy is trying to capture — the CLAUDE.md summary of this is "taker
fees bleed below 7c."

`bot/exchange/kalshi.py` reflects this directly: `place_limit_order`
(`kalshi.py:137-140`) always calls `_place_order` with
`time_in_force="good_till_canceled", post_only=True`, and `_place_order`
only sets `body["post_only"] = True` when the `post_only` flag is set
(`kalshi.py:360-361`). Kalshi's own semantics for `post_only`
(`.ai/status/qua319-research.md` line 219): "Reject if would cross —
maker-only." `place_market_order` (`kalshi.py:142-145`) intentionally does
not set `post_only` (`time_in_force="immediate_or_cancel"`), since a market
order is inherently a taker order by definition — but the strategy's entry
path (`bot/strategy/nothing_happens.py`) uses `place_limit_order`, not
`place_market_order`, for the longshot-selling flow this decision is about.

## Decision

All Kalshi limit orders placed by this bot's strategy path use
`post_only=true`, so an order that would cross the book and take
liquidity is rejected by Kalshi rather than filled at the taker fee rate.
Maker-only is the default and only mode for `place_limit_order`.

## Rejected Alternatives

- **Allow taker fills for speed/certainty of entry.** Rejected — at the
  strategy's target price range (below ~7c), the taker fee rate is high
  enough relative to the per-contract edge that it can erase the edge
  entirely; the QUA-320 analysis shows maker fees are 25% of taker at the
  same price.
- **Make maker-vs-taker a runtime-configurable flag.** Not adopted for the
  current strategy — the fee math is a structural fact of Kalshi's fee
  schedule at the prices this bot trades, not a per-run tuning choice, so
  hardcoding `post_only=true` in the limit-order path avoids a config knob
  that could be set wrong and silently bleed fees.

## Rationale

- The fee differential (4x) is large relative to the edge this strategy
  targets (small per-contract longshot premiums), so eating the taker fee
  is not a viable tradeoff for faster fills.
- `post_only=true` fails closed: if the order would cross and become a
  taker fill, Kalshi rejects it outright rather than silently filling at
  the worse fee rate — the bot cannot accidentally pay taker fees through
  this path.
- Keeping this enforced inside `place_limit_order` (rather than as a
  caller-supplied option) means every current and future caller of the
  strategy's limit-order path gets the maker-only guarantee automatically.

## Consequences

- Limit orders that would cross the book are rejected instead of filled —
  the strategy must be tolerant of orders not filling immediately, and any
  fill-rate/latency expectations must account for maker-only order
  placement not guaranteeing entry.
- If the strategy is ever extended to need guaranteed/immediate entries at
  a specific price (as opposed to resting for a fill), that would require
  `place_market_order` (which is intentionally not maker-only) rather than
  changing `place_limit_order`'s default.
- The fee thresholds "below 7c" cited here are anchored to a specific
  Kalshi fee schedule; if Kalshi changes its maker/taker fee rates, the
  breakeven price point (and possibly the maker-only requirement itself)
  needs to be recalculated using the `fee-breakeven.py` calculator
  referenced in CLAUDE.md.

## Revisit Triggers

- Kalshi changes its maker/taker fee schedule such that the taker penalty
  at the strategy's target price range shrinks enough to no longer erase
  the edge.
- The strategy is extended to a use case that needs guaranteed fills over
  fee optimization (e.g. urgent risk-reduction exits), which would need a
  deliberate, explicit taker path rather than relaxing `place_limit_order`.
- Re-running the fee-breakeven calculation (`fee-breakeven.py`) after any
  Kalshi fee schedule change shows a different price threshold than "7c."
