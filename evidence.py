"""Shared evidence rules for every verified list (Facebook, designer goods, the existing auction page).

THE BAR (ruling): net >= MIN_NET dollars AND return >= MIN_RETURN of all-in cost. Repair plays need REPAIR_CUSHION more net.
VALUATION TIERS (ruling, replaces a flat minimum of 5):
    n >= 10 comps                value at the MEDIAN
    5 <= n <= 9                  value at the 25th PERCENTILE
    3 <= n <= 4                  value at the 25th percentile AND the net bar rises to THIN_NET
    n < 3                        no value
    wide spread (IQR / median > 0.5)  value at the 25th percentile whatever n is, and the row is flagged "wide spread"
OPEN CONFLICT (flagged to Muse): the page-gate ruling says a verified row needs "5+ comps", while the tier ruling gives n = 3-4 a value
with a $150 bar. VERIFY_MIN_COMPS = 5 is the stricter reading and is what the gates use; set it to 3 to allow the thin tier through.

DEMAND: sales velocity (sold per 30 days) comes only from the dates of stored sold comps. Time to sell is "unknown" unless an active-listing
count exists, in which case a model-based chance of selling within 90 days is shown; it is never invented from a category prior.
"""
from __future__ import annotations

import math
import sqlite3
from datetime import datetime, timezone

MIN_NET = 100.0
MIN_RETURN = 0.40
REPAIR_CUSHION = 25.0
THIN_NET = 150.0
VERIFY_MIN_COMPS = 5
WIDE_IQR = 0.5


def percentile(prices: list[float], q: float) -> float:
    xs = sorted(prices)
    if not xs:
        raise ValueError("no prices")
    pos = (len(xs) - 1) * q
    lo, hi = math.floor(pos), math.ceil(pos)
    return xs[lo] + (xs[hi] - xs[lo]) * (pos - lo)


def value_tier(prices: list[float]) -> dict:
    """Value from comp prices by the tier rules above. 'value' is None below 3 comps. 'verifiable' is True only at VERIFY_MIN_COMPS or more."""
    n = len(prices)
    out = {"n": n, "tier": "none", "value": None, "median": None, "p25": None, "iqr_ratio": None, "wide": False, "net_bar": MIN_NET,
           "verifiable": n >= VERIFY_MIN_COMPS, "low": None, "high": None}
    if n == 0:
        return out
    med = percentile(prices, 0.5)
    out.update({"median": round(med, 2), "low": round(min(prices), 2), "high": round(max(prices), 2)})
    if n < 3:
        return out
    p25 = percentile(prices, 0.25)
    iqr = (percentile(prices, 0.75) - p25) / med if med else 0.0
    out.update({"p25": round(p25, 2), "iqr_ratio": round(iqr, 3), "wide": iqr > WIDE_IQR})
    if n >= 10 and not out["wide"]:
        out.update({"tier": "median (n>=10)", "value": round(med, 2)})
    elif n >= 5:
        out.update({"tier": "25th percentile (n=5-9)" if not out["wide"] or n < 10 else "25th percentile (wide spread)", "value": round(p25, 2)})
    else:
        out.update({"tier": "25th percentile, bar raised (n=3-4)", "value": round(p25, 2), "net_bar": THIN_NET})
    if out["wide"]:
        out["tier"] += "; wide spread"
    return out


def _dt(raw: str):
    try:
        d = datetime.fromisoformat(raw)
    except (TypeError, ValueError):
        return None
    return d if d.tzinfo else d.replace(tzinfo=timezone.utc)


def active_count(conn: sqlite3.Connection, item_key: str | None) -> int | None:
    """Stored active-listing count for this item (market_stats), or None when none exists."""
    if not item_key:
        return None
    try:
        row = conn.execute("SELECT active FROM market_stats WHERE item_key=? AND active IS NOT NULL ORDER BY observed_at DESC LIMIT 1", (item_key,)).fetchone()
    except sqlite3.Error:
        return None
    return int(row[0]) if row and row[0] is not None else None


def demand(sales: list[tuple], now: datetime, active: int | None = None) -> dict:
    """Velocity from the stored comp dates. sales = [(title, price, sold_date_iso), ...]."""
    ages = [(now - d).days for d in (_dt(s[2]) for s in sales) if d is not None]
    sold_30, sold_90 = sum(1 for a in ages if a <= 30), sum(1 for a in ages if a <= 90)
    per_30d = round(sold_90 / 3, 2)  # the 90-day window is steadier than 30 days for thin markets
    out = {"sold_30d": sold_30, "sold_90d": sold_90, "sold_per_30d": per_30d, "dated_sales": len(ages), "active_listings": active,
           "p_sell_90d": None, "time_to_sell": "unknown (no active-listing count stored)"}
    if active is not None and per_30d > 0:
        rate_per_30d_share = per_30d / (active + 1)  # one more listing shares the sales with those already up
        out["p_sell_90d"] = round(1 - math.exp(-3 * rate_per_30d_share), 3)
        out["time_to_sell"] = f"model estimate: {out['p_sell_90d']:.0%} chance of selling within 90 days among {active} active listing(s)"
    return out
