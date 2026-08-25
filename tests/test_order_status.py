from bot.order_status import normalize_order_status


def test_normalize_order_status_maps_kalshi_executed_to_matched():
    # MF8: the strategy only recognizes "matched"/"filled"/"simulated" as a
    # success status. Without this alias every Kalshi fill falls through to
    # the "unknown status" quarantine branch.
    assert normalize_order_status("executed") == "matched"


def test_normalize_order_status_maps_kalshi_resting_to_live():
    assert normalize_order_status("resting") == "live"


def test_normalize_order_status_maps_kalshi_canceled_to_cancelled():
    assert normalize_order_status("canceled") == "cancelled"


def test_normalize_order_status_leaves_polymarket_statuses_unchanged():
    assert normalize_order_status("matched") == "matched"
    assert normalize_order_status("filled") == "filled"
