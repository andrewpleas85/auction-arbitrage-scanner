"""Measure how high ShopGoodwill auctions really close relative to what the item resells for.

The static `catalog.CLIMB` table was a guess. ShopGoodwill's closed-auction search returns every finished auction with its final
price and bid count, free and without a key. For each closed item whose MODEL has a real eBay sold range (variant-aware, see
`catalog.model_range`) we compute  final_price / sold_typical  and keep the distribution per item kind. `catalog.economics` then
uses the measured median when a kind has enough samples, and the guess otherwise.

Only auctions that drew at least one bid count as sales (an unsold auction closes at its starting price), and only titles that read
as working units (the sold range describes working units). Closing times are Pacific.
"""
import asyncio
import logging
import sqlite3
import statistics
import time
from datetime import datetime, timezone

import httpx

import catalog
from clients.shopgoodwill import PACIFIC, PAGE_SIZE, URL, _body
from engine import knowledge as kb

log = logging.getLogger("arbitrage.closed")
MIN_SAMPLES = 15


def _closed_body(query: str, page: int, days_back: int, category: str = "") -> dict:
    b = _body(category, page, "1", query)
    b.update({"searchClosedAuctions": "true", "closedAuctionDaysBack": str(days_back), "sortDescending": "true"})
    return b


def parse_closed(raw: dict) -> dict | None:
    try:
        end = datetime.fromisoformat(raw["endTime"]).replace(tzinfo=PACIFIC).astimezone(timezone.utc)
        return {"listing_id": str(raw["itemId"]), "title": raw["title"], "final_price": float(raw["currentPrice"]),
                "bids": int(raw.get("numBids") or 0), "start_price": float(raw.get("startingPrice") or 0),
                "end_time": end.isoformat()}
    except (KeyError, TypeError, ValueError):
        return None


def save(conn: sqlite3.Connection, rows: list[dict], query: str) -> int:
    now = int(time.time())
    conn.executemany(
        "INSERT OR REPLACE INTO closed_auctions (platform, listing_id, title, final_price, bids, start_price, end_time, query, fetched_at) "
        "VALUES ('shopgoodwill',?,?,?,?,?,?,?,?)",
        [(r["listing_id"], r["title"], r["final_price"], r["bids"], r["start_price"], r["end_time"], query, now) for r in rows])
    conn.commit()
    return len(rows)


async def fetch(http: httpx.AsyncClient, conn: sqlite3.Connection, queries: list[str], days_back: int = 30, pages: int = 3,
                sleep: float = 0.6) -> int:
    """Download closed auctions for each query (about 40 per page) into `closed_auctions`."""
    total = 0
    for q in queries:
        for page in range(1, pages + 1):
            try:
                resp = await http.post(URL, json=_closed_body(q, page, days_back))
                resp.raise_for_status()
                items = resp.json()["searchResults"]["items"]
            except (httpx.HTTPError, KeyError, ValueError) as e:
                log.warning("closed-auction search %r page %d failed: %s", q, page, e)
                break
            total += save(conn, [r for raw in items if (r := parse_closed(raw))], q)
            if len(items) < PAGE_SIZE:
                break
            await asyncio.sleep(sleep)
        await asyncio.sleep(sleep)
    return total


async def fetch_categories(http: httpx.AsyncClient, conn: sqlite3.Connection, category_ids: list[str], days_back: int = 30,
                           pages: int = 12, sleep: float = 0.6) -> int:
    """Closed auctions for whole ShopGoodwill categories (about 40 per page): a big sample for how often auctions get no bids."""
    total = 0
    for cid in category_ids:
        for page in range(1, pages + 1):
            try:
                resp = await http.post(URL, json=_closed_body("", page, days_back, cid))
                resp.raise_for_status()
                items = resp.json()["searchResults"]["items"]
            except (httpx.HTTPError, KeyError, ValueError) as e:
                log.warning("closed-auction category %s page %d failed: %s", cid, page, e)
                break
            total += save(conn, [r for raw in items if (r := parse_closed(raw))], f"cat:{cid}")
            if len(items) < PAGE_SIZE:
                break
            await asyncio.sleep(sleep)
    return total


def category_stats(conn: sqlite3.Connection) -> list[dict]:
    """Per downloaded category: auctions, share that got no bids, median bids and median final/start price among those that sold."""
    out = []
    for (q,) in conn.execute("SELECT DISTINCT query FROM closed_auctions WHERE query LIKE 'cat:%'").fetchall():
        rows = conn.execute("SELECT final_price, bids, start_price FROM closed_auctions WHERE query=?", (q,)).fetchall()
        sold = [r for r in rows if r[1] >= 1]
        ratios = [r[0] / r[2] for r in sold if r[2] and r[2] > 0]
        out.append({"category": q[4:], "n": len(rows), "unsold": 1 - len(sold) / len(rows) if rows else 0.0,
                    "median_bids": statistics.median([r[1] for r in sold]) if sold else 0,
                    "median_final": statistics.median([r[0] for r in sold]) if sold else 0.0,
                    "median_vs_start": statistics.median(ratios) if ratios else 0.0})
    return sorted(out, key=lambda r: -r["n"])


def measure(conn: sqlite3.Connection) -> list[dict]:
    """Ratio of final price to the model's sold value, per item kind. Writes climb_stats and returns the table."""
    ratios: dict[str, list[float]] = {}
    unsold: dict[str, list[int]] = {}
    rows = conn.execute("SELECT title, final_price, bids FROM closed_auctions WHERE platform='shopgoodwill'").fetchall()
    for title, final, bids in rows:
        rng = catalog.model_range(conn, title)
        brand, kind, style = catalog.style_of(title, "electronics")
        if not brand:
            brand, kind, style = catalog.style_of(title, "fashion")
        if rng is None and brand:
            rng = catalog.known_range(conn, brand, kind, style)  # the style-group range, when it rests on sold prices
        if rng is None or rng.typical is None or rng.basis not in ("sold", "realized") or not brand:
            continue
        factor, cond = catalog.condition_factor(title)
        if cond not in ("working", "refurbished"):
            continue
        unsold.setdefault(kind, []).append(1 if bids == 0 else 0)
        if bids >= 1 and final > 0:
            ratios.setdefault(kind, []).append(final / rng.typical)
    out = []
    now = int(time.time())
    conn.execute("DELETE FROM climb_stats WHERE platform='shopgoodwill'")
    for kind, vals in sorted(ratios.items(), key=lambda kv: -len(kv[1])):
        vals.sort()
        q = lambda p: vals[min(len(vals) - 1, int(p * len(vals)))]
        row = {"kind": kind, "n": len(vals), "median": statistics.median(vals), "p25": q(0.25), "p75": q(0.75),
               "unsold_share": sum(unsold.get(kind, [])) / max(1, len(unsold.get(kind, [])))}
        conn.execute("INSERT INTO climb_stats (platform, kind, n, median, p25, p75, unsold_share, updated_at) VALUES ('shopgoodwill',?,?,?,?,?,?,?)",
                     (kind, row["n"], row["median"], row["p25"], row["p75"], row["unsold_share"], now))
        out.append(row)
    conn.commit()
    return out


def measured_climb(conn: sqlite3.Connection, platform: str, kind: str) -> float | None:
    """The measured median closing ratio for this platform and item kind, or None without MIN_SAMPLES."""
    try:
        r = conn.execute("SELECT n, median FROM climb_stats WHERE platform=? AND kind=?", (platform, kind)).fetchone()
    except sqlite3.Error:
        return None
    return float(r[1]) if r and r[0] >= MIN_SAMPLES else None
