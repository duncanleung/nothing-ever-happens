_ORDER_STATUS_ALIASES = {
    "canceled": "cancelled",
    "cancelled": "cancelled",
    "delayed": "delayed",
    "executed": "matched",  # Kalshi: a filled order.
    "filled": "filled",
    "live": "live",
    "matched": "matched",
    "open": "open",
    "partial": "partially_filled",
    "partial_fill": "partially_filled",
    "partial_filled": "partially_filled",
    "partially_filled": "partially_filled",
    "rejected": "rejected",
    "resting": "live",  # Kalshi: an unfilled order still on the book.
    "simulated": "simulated",
    "submitted": "submitted",
    "unmatched": "unmatched",
}


def normalize_order_status(status: str) -> str:
    normalized = str(status).strip().lower()
    return _ORDER_STATUS_ALIASES.get(normalized, normalized)


def normalize_optional_order_status(status: str | None) -> str | None:
    if status is None:
        return None
    normalized = normalize_order_status(status)
    return normalized or None
