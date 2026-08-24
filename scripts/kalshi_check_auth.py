"""Verify Kalshi RSA-PSS credentials against GET /portfolio/balance.

Usage: python -m scripts.kalshi_check_auth
Requires KALSHI_API_KEY_ID and KALSHI_PRIVATE_KEY_PATH in the environment
(.env). Defaults to the demo environment unless KALSHI_ENVIRONMENT=production.
"""
import os
import sys

from dotenv import load_dotenv

from bot.exchange.kalshi_auth import DEMO_BASE_URL, PROD_BASE_URL, KalshiAuthError, KalshiAuthSession


def main() -> None:
    load_dotenv()
    api_key_id = os.environ.get("KALSHI_API_KEY_ID", "")
    private_key_path = os.environ.get("KALSHI_PRIVATE_KEY_PATH", "")
    environment = os.environ.get("KALSHI_ENVIRONMENT", "demo").strip().lower()
    base_url = PROD_BASE_URL if environment == "production" else DEMO_BASE_URL

    if not api_key_id or not private_key_path:
        print("KALSHI_API_KEY_ID and KALSHI_PRIVATE_KEY_PATH must be set")
        sys.exit(1)

    try:
        session = KalshiAuthSession(api_key_id=api_key_id, private_key_path=private_key_path, base_url=base_url)
        response = session.get("/portfolio/balance")
        response.raise_for_status()
    except KalshiAuthError as exc:
        print(f"auth setup failed: {exc}")
        sys.exit(1)
    except Exception as exc:
        print(f"request failed: {exc}")
        sys.exit(1)

    print(f"environment={environment} base_url={base_url}")
    print(response.json())


if __name__ == "__main__":
    main()
