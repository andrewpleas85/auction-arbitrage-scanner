"""Tiered Discord webhook alerts: CRITICAL (Buy It Now) and WATCHLIST (auction ending soon)."""
import asyncio
from datetime import datetime, timezone

import httpx
from zoneinfo import ZoneInfo

from config import Config
from engine.timeleft import time_left
from engine.valuation import Valuation
from models import Listing

EASTERN = ZoneInfo("America/New_York")
CRITICAL, WATCHLIST = "critical", "watchlist"
COLORS = {CRITICAL: 0xFF1744, WATCHLIST: 0xFFA000}


def minutes_left(listing: Listing, now: datetime | None = None) -> float | None:
    if listing.end_time is None:
        return None
    return (listing.end_time - (now or datetime.now(timezone.utc))).total_seconds() / 60


def alert_tier(listing: Listing, val: Valuation, cfg: Config,
               now: datetime | None = None) -> str | None:
    """Which alert (if any) this valued listing earns.

    CRITICAL : Buy It Now and profit/ROI thresholds met.
    WATCHLIST: GSA daily_auction on its end date (date-only end time) with net profit >= MIN_PROFIT.
    WATCHLIST: auction inside the final window (current bid only) and thresholds met.
    None     : thresholds missed, or an auction that is still too far from ending.
    """
    if listing.listing_type == "daily_auction":
        today = (now or datetime.now(timezone.utc)).astimezone(EASTERN).date().isoformat()
        return WATCHLIST if (listing.extra.get("end_date") == today
                             and val.net_profit >= cfg.min_profit) else None
    if not val.meets(cfg):
        return None
    if listing.listing_type == "buy_it_now":
        return CRITICAL
    left = minutes_left(listing, now)
    if left is not None and 0 < left < cfg.ending_window_minutes:
        return WATCHLIST
    return None


def build_embed(listing: Listing, val: Valuation, tier: str) -> dict:
    basis = {"sold": "eBay sold, trimmed mean", "active": "eBay active asks (discounted)",
             "mock": "MOCK DATA (test only)", "melt": "Precious-metal melt value, not sold comps"}.get(
        val.comp_source, "eBay sold comps")
    fields = [
        {"name": "Buy Price", "value": f"${listing.price:,.2f} + ${listing.shipping_cost:,.2f} ship", "inline": True},
        {"name": "Est. Resale", "value": f"${val.estimated_value:,.2f}", "inline": True},
        {"name": "Net Profit", "value": f"${val.net_profit:,.2f} ({val.margin_pct:.0f}% ROI)", "inline": True},
        {"name": "Comps", "value": f"{val.comp_count} · {basis}" + (f"\n{val.detail}" if val.detail else ""), "inline": False},
    ]
    if tier == CRITICAL:
        title = f"🚨 CRITICAL · Buy It Now · {listing.title}"
        action = "Buy now. Price is fixed."
        if listing.extra.get("auction_bid") is not None:  # an auction whose Buy It Now price is the deal
            action += (f"\nThis is also an auction: current bid ${listing.extra['auction_bid']:,.2f}, "
                       f"ends in {time_left(listing.end_time)}. The bid may stay lower, but can be outbid.")
        fields.insert(0, {"name": "Action", "value": action, "inline": False})
    else:
        left = minutes_left(listing)
        if listing.listing_type == "daily_auction":
            title = f"⏳ WATCHLIST · Closes today · {listing.title}"
            when = "Closes TODAY (GSA gives no closing time, so check the listing)"
        else:
            title = f"⏳ WATCHLIST · Auction ending soon · {listing.title}"
            when = (f"Ends in {time_left(listing.end_time)} (<t:{int(listing.end_time.timestamp())}:R>)")
        fields.insert(0, {
            "name": "Auction",
            "value": when + f"\nCurrent bid ${listing.price:,.2f} only. It can still rise, so recheck before bidding.",
            "inline": False})
    if listing.extra.get("melt"):
        fields.append({"name": "Melt value (spot × weight × purity × payout)", "value": listing.extra["melt"][:900],
                       "inline": False})
    d = listing.extra.get("details")
    needs = listing.extra.get("needs")
    if d and d.get("summary"):
        fields.append({"name": "Listing page says", "value": d["summary"][:900], "inline": False})
    elif needs:
        fields.append({"name": "Needs", "inline": False, "value": ", ".join(
            f"{p} (~${c:,.0f})" for p, c in needs.items()) + " (included in cost)"})
    tel = listing.extra.get("telecom")
    if tel and tel.get("status"):
        clean = val.estimated_value / tel["factor"] if tel.get("factor") else None
        fields.append({"name": "Phone risk", "inline": False, "value": (
            f"{tel['status'].upper()}: valued at {tel['factor']:.0%} of a clean comp. " + "; ".join(tel.get("reasons", []))
            + (f" If the IMEI turns out clean it is worth about ${clean:,.0f}." if clean and tel["factor"] < 1 else ""))[:900]})
    liq = listing.extra.get("liquidity")
    if liq:
        fields.append({"name": "Will it sell?", "value": liq["text"][:900], "inline": False})
    if listing.extra.get("known_range"):
        fields.append({"name": "Value range on file", "value": listing.extra["known_range"][:500], "inline": False})
    if listing.extra.get("grade"):
        fields.append({"name": "Confidence", "value": listing.extra["grade"], "inline": True})
    if listing.extra.get("cross_check"):
        fields.insert(0, {"name": "⚠ Cross-check", "value": listing.extra["cross_check"][:500], "inline": False})
    retail = listing.extra.get("retail")
    if retail:
        fields.append({"name": "New at Best Buy", "inline": True,
                       "value": f"${retail['price']:,.2f} ([link]({retail['url']}))" if retail.get("url")
                       else f"${retail['price']:,.2f}"})
    if val.comp_source == "active":
        fields.insert(0, {"name": "⚠ WARNING: NOT SOLD DATA",
                          "value": "Valued from discounted eBay ACTIVE ASKING prices. Unsold asks overstate "
                                   "value: verify real sold prices before buying.", "inline": False})
        title = "⚠ ACTIVE-ASKS · " + title
    if listing.location:
        fields.append({"name": "Location", "value": listing.location, "inline": True})
    embed = {
        "title": title[:250], "url": listing.url, "color": COLORS[tier], "fields": fields,
        "footer": {"text": f"{listing.platform} · {listing.category}"[:100]},
    }
    if listing.image_url:
        embed["thumbnail"] = {"url": listing.image_url}
    return embed


async def send_alert(http: httpx.AsyncClient, webhook_url: str, listing: Listing,
                     val: Valuation, tier: str, ping_here: bool = False) -> None:
    payload: dict = {"embeds": [build_embed(listing, val, tier)],
                     "allowed_mentions": {"parse": []}}
    if ping_here and tier == CRITICAL:
        payload["content"] = "@here"
        payload["allowed_mentions"] = {"parse": ["everyone"]}
    for _ in range(3):
        resp = await http.post(webhook_url, json=payload)
        if resp.status_code == 429:
            await asyncio.sleep(float(resp.json().get("retry_after", 1)))
            continue
        resp.raise_for_status()
        return
    resp.raise_for_status()
