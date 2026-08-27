---
id: ADR-0001
title: Raw HTTP for Kalshi instead of the official SDK
status: accepted
date: 2026-08-26
tags: [kalshi, exchange-client, http, sdk]
supersedes: []
related: []
---

# 0001 — Raw HTTP for Kalshi instead of the official SDK

Date: 2026-08-26
Status: Accepted

## Context

`bot/exchange/kalshi.py` implements `KalshiExchangeClient`, one of four
`ExchangeClient` implementations in this repo (`polymarket_clob.py`,
`kalshi.py`, `paper.py`, `real_price_paper.py`). The Polymarket client
(`PolymarketClobExchangeClient`) uses the official `py_clob_client` SDK
(`py_clob_client.client.ClobClient`) for all exchange interaction — the SDK
handles EIP-712/HMAC signing, CLOB order building, balance-allowance sync,
and conditional token approvals.

Kalshi publishes an official Python SDK (`kalshi-python`, v2.1.4 on PyPI),
which requires Python >=3.9. This project pins Python 3.12
(`.python-version`), so the SDK is version-compatible. The question is
whether adopting it is worth the added dependency given the bot's narrow use
of Kalshi's API.

`KalshiAuthSession` (`bot/exchange/kalshi_auth.py`, 136 lines) implements
Kalshi's RSA-PSS request signing and exposes `request_json()`.
`KalshiExchangeClient` uses this for every endpoint. The bot touches 9
distinct Kalshi REST endpoints:

1. `GET /portfolio/balance`
2. `GET /portfolio/orders` (with query params)
3. `GET /portfolio/orders/{order_id}`
4. `GET /portfolio/positions` (with and without ticker param)
5. `GET /markets/{ticker}`
6. `GET /markets/{ticker}/orderbook`
7. `POST /portfolio/events/orders`
8. `DELETE /portfolio/events/orders/{order_id}`
9. `DELETE /portfolio/events/orders/batched`

## Decision

Talk to Kalshi's trade API v2 over raw HTTP with hand-rolled RSA-PSS request
signing (`KalshiAuthSession`). Do not take a dependency on Kalshi's official
Python SDK (`kalshi-python`).

## Rejected Alternatives

- **Adopt the official Kalshi Python SDK (`kalshi-python`).** Rejected
  because the bot uses only 9 REST endpoints and the RSA-PSS signing logic
  is 136 lines of self-contained code. The SDK would add a dependency (with
  its own transitive dependencies) for convenience that raw HTTP already
  provides at this scale. The SDK's generated client also exposes Kalshi's
  full API surface — portfolio management, settlement, account endpoints —
  none of which this bot uses, so the SDK's type coverage would be mostly
  dead weight.
- **Vendor a community Kalshi SDK.** Not pursued — it would introduce a
  third-party HTTP/signing abstraction alongside the existing 136-line
  auth module, increasing the number of distinct request-plumbing
  implementations to maintain for no functional gain at the current API
  surface size.

## Rationale

- **Small API surface.** The bot uses 9 of Kalshi's REST endpoints. At that
  scale, `request_json(method, path)` calls are straightforward and carry
  no meaningful maintenance burden.
- **Compact auth implementation.** `KalshiAuthSession` is 136 lines,
  including error handling and the `requests.Session` wrapper. The core
  RSA-PSS signing is ~20 lines. This is small enough to own directly.
- **Fewer dependencies.** The raw HTTP approach uses only `requests` and
  `cryptography` — both already in the project's dependency tree. Adding
  `kalshi-python` would introduce another package (and its transitive
  dependencies) for marginal convenience.
- **Direct control over response parsing.** The bot maps Kalshi responses
  to its own internal models (`OrderResult`, `OpenOrder`, `Trade`, etc.)
  with exchange-specific translation (Yes/No token mapping, price
  inversion). Raw HTTP gives full control over this mapping without an
  SDK's intermediate types.

Note: the Polymarket client takes the opposite approach — it uses the
official `py_clob_client` SDK. That is the correct tradeoff for Polymarket,
whose API surface is larger and whose auth (EIP-712 signing, proxy wallet
approvals, balance-allowance sync) benefits from SDK support. Kalshi's
simpler REST+RSA-PSS auth does not carry the same complexity.

## Consequences

- Any new Kalshi endpoint needs a manually-added `request_json()` call and,
  where response shapes are unverified, a docstring note flagging that (see
  the module docstring in `kalshi.py`: response field names follow PRE-2
  research notes, not a verified live response, until confirmed against the
  demo API).
- RSA-PSS signing logic in `KalshiAuthSession` is project-maintained code,
  not SDK code — bugs or Kalshi API changes to the signing scheme must be
  caught and fixed here rather than picked up via an SDK version bump.
- If the number of Kalshi endpoints grows substantially, the maintenance
  cost of raw HTTP rises and the SDK tradeoff shifts — this ADR would need
  to be revisited.

## Revisit Triggers

- Kalshi's REST surface used by this bot grows beyond ~15 endpoints, at
  which point SDK-generated types and methods reduce repetitive boilerplate.
- Kalshi changes its signing scheme in a way that is nontrivial to
  replicate by hand.
- The bot needs Kalshi WebSocket streaming or complex multi-leg order
  support, where an SDK's abstractions add more value than raw HTTP calls.
