# CLAUDE.md

## Overview

Fork of `sterlingcrispin/nothing-ever-happens` — a Python bot that sells longshot prediction market contracts. Extended with Kalshi exchange support (QUA-319, complete) and Polymarket paper trading (QUA-317, in progress).

## Project Tracking

- **Linear workspace:** str-labs, team QUA
- **Vault project docs:** `/Users/duncanleung/Documents/obsidian-local-life-manager/projects/kalshi-trading-bot/`
  - `project-brief.md` — vision, problem, audience
  - `2026-08-23-deep-research-findings.md` — deep research on Kalshi auto-trading
  - `fee-breakeven.py` — fee breakeven calculator
- **Vault research wiki:** `/Users/duncanleung/Documents/obsidian-local-life-manager/research/wiki/`
  - `prediction-market-edges.md` — strategy edges
  - `longshot-bias.md` — the core thesis (73.3% of markets resolve No)
  - `kalshi-sports-bot-architecture.md` — Kalshi bot architecture patterns
  - `polymarket-validation-agent.md` — Polymarket validation agent research
  - `data-latency-hierarchy.md` — data latency advantages

## Exchanges

| Exchange | Client | Auth | Status |
|----------|--------|------|--------|
| Polymarket | `bot/exchange/polymarket.py` | EIP-712 + HMAC | Original, working |
| Kalshi | `bot/exchange/kalshi.py` | RSA-PSS (raw HTTP) | Added QUA-319, auth verified |
| Paper | `bot/exchange/paper.py` | None | In-memory, platform-agnostic |

## Key Decisions

- **Raw HTTP for Kalshi** — Kalshi SDK requires Python 3.13+; raw HTTP matches the Polymarket client pattern.
- **Direction mapping** — strategy buys No where No is cheap. On Kalshi: `side="ask"`, `price = 1 - no_price`, `post_only=true`.
- **No-price derivation** — Kalshi book is YES-only. Correct formula: `effective_no_price = 1 - yes_bid_dollars`.
- **Maker orders only** — per QUA-320 fee analysis, taker fees bleed below 7c.

## Credentials

All secrets are in `.env` (gitignored). Required for each exchange:

- **Polymarket:** `PRIVATE_KEY`, `POLYMARKET_API_KEY`, `POLYMARKET_API_KEY_ADDRESS`, `POLYMARKET_WALLET_ADDRESS`
- **Kalshi:** `KALSHI_API_KEY_ID`, `KALSHI_PRIVATE_KEY_PATH` (points to `keys/kalshi_private.pem`)

## Safety

Real order transmission requires all three flags: `BOT_MODE=live`, `LIVE_TRADING_ENABLED=true`, `DRY_RUN=false`. Any missing flag activates `PaperExchangeClient`.

## Tests

```bash
python -m pytest -q    # 201 tests
```
