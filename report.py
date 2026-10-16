"""Read-only views over arbitrage.db (no network): `python main.py --watchlist`."""
import sqlite3
import time
from datetime import datetime, timezone

from engine.timeleft import time_left


def _age(ts: int | None, now: float) -> str:
    if not ts:
        return "?"
    mins = int((now - ts) // 60)
    return "now" if mins < 1 else f"{mins}m ago" if mins < 120 else f"{mins // 60}h ago"


def watchlist_rows(conn: sqlite3.Connection, now: datetime | None = None, statuses=("watchlist", "alerted")) -> list[dict]:
    """Auctions still running that we are tracking or have alerted on, soonest-ending first."""
    now = now or datetime.now(timezone.utc)
    marks = ",".join("?" * len(statuses))
    rows = conn.execute(
        f"""SELECT platform, listing_id, title, price, shipping_cost, status, end_time, listing_type, url,
                   estimated_market_value, net_profit_usd, profit_margin, comp_source, updated_at
            FROM listings WHERE status IN ({marks}) AND listing_type IN ('auction','daily_auction')
            AND end_time IS NOT NULL""", statuses).fetchall()
    out = []
    for r in rows:
        end = datetime.fromisoformat(r["end_time"])
        if end.tzinfo is None:
            end = end.replace(tzinfo=timezone.utc)
        if end > now:
            out.append({**dict(r), "end": end, "left": time_left(end, now)})
    return sorted(out, key=lambda r: r["end"])


def format_watchlist(rows: list[dict], now_ts: float | None = None) -> str:
    if not rows:
        return "No tracked auctions are still running. Run a scan first (python main.py --once)."
    now_ts = now_ts or time.time()
    lines = [f"{'ENDS IN':>8}  {'BID':>9}  {'SEEN':>8}  {'EST VALUE':>9}  {'NET':>7}  {'':8} ITEM", "-" * 110]
    for r in rows:
        tag = "daily*" if r["listing_type"] == "daily_auction" else r["status"]
        src = "melt" if r["comp_source"] == "melt" else ""
        lines.append(
            f"{r['left']:>8}  ${r['price']:>8,.2f}  {_age(r['updated_at'], now_ts):>8}  "
            f"${(r['estimated_market_value'] or 0):>8,.0f}  ${(r['net_profit_usd'] or 0):>6,.0f}  {tag:<8} "
            f"[{r['platform'][:5]}{' ' + src if src else ''}] {r['title'][:52]}")
        if r.get("url"):
            lines.append(f"{'':>8}  {r['url']}")
    lines.append("")
    lines.append("Bids are as of the last scan (SEEN); they only go up. daily* = GSA gives a date, not a closing time.")
    return "\n".join(lines)


def format_lookup(conn: sqlite3.Connection, text: str) -> str:
    """Everything known about an item: current range, the evidence behind it, listings we've seen."""
    from engine import knowledge as kb
    keys = kb.find_items(conn, text)
    if not keys:
        return f"Nothing known about '{text}' yet. Add evidence: --add-price \"{text}\" --kind ask --source ebay --low 100 --high 200"
    now, out = int(time.time()), []
    for key in keys:
        rng = kb.compute_range(conn, key)
        out.append(f"\n== {key}")
        out.append("   VALUE: " + (rng.text() if rng else "no evidence inside the recency window"))
        for n in (rng.notes if rng else []):
            out.append(f"          note: {n}")
        out.append(f"   {'KIND':10} {'SOURCE':18} {'LOW':>8} {'TYPICAL':>8} {'HIGH':>8} {'N':>4}  AGE   NOTES")
        for o in kb.observations(conn, key)[:15]:
            days = (now - o["observed_at"]) / 86400
            out.append(f"   {o['kind']:10} {o['source'][:18]:18} {o['low'] or 0:>8,.0f} {o['typical'] or 0:>8,.0f} "
                       f"{o['high'] or 0:>8,.0f} {o['n'] or 1:>4}  {days:>4.0f}d  {(o['notes'] or '')[:60]}")
        toks = key.split()[:3]
        seen = conn.execute("SELECT title, price, status, listing_type, end_time FROM listings WHERE "
                            + " AND ".join("lower(title) LIKE ?" for _ in toks) + " ORDER BY first_seen_timestamp DESC LIMIT 5",
                            [f"%{t}%" for t in toks]).fetchall() if toks else []
        for r in seen:
            out.append(f"   seen: ${r['price']:,.2f} [{r['status']}] {r['title'][:70]}")
    return "\n".join(out)


def format_inventory(conn: sqlite3.Connection, now_ts: float | None = None) -> str:
    """Open trades (bought, not yet sold): how long each has been held/listed and what the markdown schedule says."""
    from engine import liquidity as lq
    now_ts = now_ts or time.time()
    rows = conn.execute("SELECT * FROM trades WHERE sold_at IS NULL ORDER BY COALESCE(listed_at, bought_at)").fetchall()
    if not rows:
        return "No open trades. Log a purchase: --log-trade \"item\" --buy 40 [--listed-at 2026-10-01 --list-price 90]"
    lines = [f"{'#':>3} {'HELD':>6} {'LISTED':>7} {'LIST $':>8} {'COST $':>8}  {'ACTION':30} ITEM", "-" * 100]
    for r in rows:
        held = (now_ts - r["bought_at"]) / 86400
        listed = (now_ts - r["listed_at"]) / 86400 if r["listed_at"] else None
        pct, label = lq.suggested_markdown(listed if listed is not None else 0)
        action = ("not listed yet: list it" if listed is None and held > 3 else
                  label + (f" -> ${r['list_price'] * (1 - pct):,.0f}" if pct and r["list_price"] else ""))
        lines.append(f"{r['id']:>3} {held:>5.0f}d {('%.0fd' % listed) if listed is not None else '-':>7} "
                     f"{(r['list_price'] or 0):>8,.0f} {(r['buy_price'] + (r['buy_costs'] or 0)):>8,.0f}  {action[:30]:30} {r['item_name'][:40]}")
    done = lq.calibration(conn)
    lines.append("")
    lines.append(f"Your realized median days-to-sell so far: {done[0]:.0f} days over {done[1]} sales." if done else
                 "No realized sales with listing dates yet: log --listed-at and --close-trade to start measuring days-to-sell.")
    lines.append("Markdown cadence is a vendor illustration (15% at 22d, 30% at 43d, 50% at 61d, 70% at 91d), not a measured optimum.")
    return "\n".join(lines)


def format_liquidity(conn: sqlite3.Connection, text: str) -> str:
    """Market stats we hold for an item, the expected days to sell, and any realized data of your own."""
    from engine import knowledge as kb
    from engine import liquidity as lq
    keys = kb.find_items(conn, text)
    keys += [r[0] for r in conn.execute("SELECT DISTINCT item_key FROM market_stats").fetchall() if set(kb.item_key(text).split()) <= set(r[0].split())]
    keys = sorted(set(keys))
    if not keys:
        return f"No market stats for '{text}' yet. They are collected when the scanner evaluates a profitable candidate (needs sold data)."
    out = []
    for key in keys:
        s = lq.latest_stats(conn, key)
        rng = kb.compute_range(conn, key)
        est = lq.expected_days(s, None, rng.typical if rng else None, lq.calibration(conn, key) or lq.calibration(conn))
        out.append(f"\n== {key}")
        if s:
            age = (time.time() - s.observed_at) / 86400
            out.append(f"   sold 30d: {s.sold_30d}  sold 90d: {s.sold_90d}  active: {s.active}  stale: {s.stale_active}  "
                       f"median sold: {s.median_sold}  median ask: {s.median_ask}  avg watchers: {s.avg_watchers}  ({s.source}, {age:.0f}d old)")
            if s.str_reseller() is not None:
                out.append(f"   sell-through (sold/active, 30d): {s.str_reseller():.0%}   retail-style: {s.str_retail():.0%}")
        out.append("   " + est.text())
        for n in est.notes:
            out.append(f"   note: {n}")
    return "\n".join(out)
