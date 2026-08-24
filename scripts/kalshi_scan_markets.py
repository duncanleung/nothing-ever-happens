"""Scan Kalshi demo for longshot No contracts and print how many qualify.

Usage: python -m scripts.kalshi_scan_markets
Public discovery endpoints only — no credentials required. Validates the
QUA-319 premise that >=50 qualifying contracts exist to support 50+
simultaneous positions.
"""
import os
import sys

from dotenv import load_dotenv

from bot.kalshi_markets import DEFAULT_MAX_NO_PRICE, DEMO_BASE_URL, PROD_BASE_URL, KalshiMarketFetchError, fetch_kalshi_markets


def main() -> None:
    load_dotenv()
    environment = os.environ.get("KALSHI_ENVIRONMENT", "demo").strip().lower()
    base_url = PROD_BASE_URL if environment == "production" else DEMO_BASE_URL

    try:
        markets = fetch_kalshi_markets(base_url, max_no_price=DEFAULT_MAX_NO_PRICE)
    except KalshiMarketFetchError as exc:
        print(f"scan failed: {exc}")
        sys.exit(1)

    print(f"environment={environment} base_url={base_url} max_no_price={DEFAULT_MAX_NO_PRICE}")
    print(f"qualifying markets: {len(markets)}")
    for market in markets[:50]:
        print(
            f"  {market.ticker:20s} no={market.no_price:.2f} vol={market.volume:>10.0f} "
            f"cat={market.category:12s} {market.question[:60]}"
        )

    if len(markets) < 50:
        print(f"\nWARNING: only {len(markets)} qualifying markets found (<50) — premise needs revisiting")


if __name__ == "__main__":
    main()
