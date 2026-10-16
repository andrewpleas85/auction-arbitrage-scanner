"""SQLite state: seen listings, valuations, alert status."""
import sqlite3
import time
from datetime import datetime

SCHEMA = """
CREATE TABLE IF NOT EXISTS listings (
    listing_id TEXT NOT NULL,
    platform TEXT NOT NULL,
    title TEXT NOT NULL,
    price REAL NOT NULL,
    shipping_cost REAL NOT NULL DEFAULT 0,
    estimated_market_value REAL,
    profit_margin REAL,
    first_seen_timestamp INTEGER NOT NULL,
    status TEXT NOT NULL,
    PRIMARY KEY (platform, listing_id)
);
CREATE TABLE IF NOT EXISTS price_observations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    item_key TEXT NOT NULL,
    item_name TEXT,
    kind TEXT NOT NULL,          -- realized | sold | ask | new_retail | melt
    source TEXT NOT NULL,        -- soldcomps, ebay, poshmark, swappa, bestbuy, manual, my-sale-...
    low REAL, high REAL, typical REAL,
    n INTEGER DEFAULT 1,
    condition TEXT, url TEXT, notes TEXT,
    observed_at INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_obs_item ON price_observations(item_key, observed_at);
CREATE TABLE IF NOT EXISTS value_ranges (
    item_key TEXT PRIMARY KEY,
    item_name TEXT, low REAL, typical REAL, high REAL,
    confidence TEXT, basis TEXT, n_obs INTEGER, retail REAL, stale INTEGER,
    updated_at INTEGER, notes TEXT
);
CREATE TABLE IF NOT EXISTS market_stats (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    item_key TEXT NOT NULL, source TEXT NOT NULL,
    sold_30d INTEGER, sold_90d INTEGER, active INTEGER, stale_active INTEGER,
    median_sold REAL, median_ask REAL, avg_watchers REAL,
    observed_at INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_mstats_item ON market_stats(item_key, observed_at);
CREATE TABLE IF NOT EXISTS trades (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    item_key TEXT, item_name TEXT,
    bought_at INTEGER, buy_price REAL, buy_costs REAL DEFAULT 0,
    sold_at INTEGER, sell_price REAL, sell_fees REAL DEFAULT 0,
    channel TEXT, notes TEXT
);
CREATE TABLE IF NOT EXISTS catalog (
    platform TEXT NOT NULL, listing_id TEXT NOT NULL,
    title TEXT NOT NULL, brand TEXT, kind TEXT, style TEXT, category TEXT,
    listing_type TEXT, current_bid REAL, buy_now REAL, shipping REAL, bids INTEGER,
    end_time TEXT, url TEXT, first_seen INTEGER, last_seen INTEGER,
    PRIMARY KEY (platform, listing_id)
);
CREATE INDEX IF NOT EXISTS idx_catalog_brand ON catalog(brand, kind);
CREATE TABLE IF NOT EXISTS tracked (
    platform TEXT NOT NULL, listing_id TEXT NOT NULL, url TEXT, max_bid REAL, note TEXT, added_at INTEGER,
    PRIMARY KEY (platform, listing_id)
);
CREATE TABLE IF NOT EXISTS bidprowl_lots (
    id TEXT PRIMARY KEY, source TEXT, title TEXT, category TEXT, state TEXT, city TEXT, zip TEXT, bid REAL, buy_now REAL,
    start_bid REAL, bids INTEGER, end_date TEXT, quantity REAL, url TEXT, bidprowl_url TEXT, condition TEXT,
    first_seen INTEGER, last_seen INTEGER, band TEXT DEFAULT 'near'
);
CREATE TABLE IF NOT EXISTS closed_auctions (
    platform TEXT NOT NULL, listing_id TEXT NOT NULL, title TEXT NOT NULL, final_price REAL NOT NULL, bids INTEGER,
    start_price REAL, end_time TEXT, query TEXT, fetched_at INTEGER,
    PRIMARY KEY (platform, listing_id)
);
CREATE TABLE IF NOT EXISTS climb_stats (
    platform TEXT NOT NULL, kind TEXT NOT NULL, n INTEGER, median REAL, p25 REAL, p75 REAL, unsold_share REAL, updated_at INTEGER,
    PRIMARY KEY (platform, kind)
);
CREATE TABLE IF NOT EXISTS sold_sales (
    provider TEXT NOT NULL, query TEXT NOT NULL, title TEXT NOT NULL, price REAL NOT NULL, sold_date TEXT NOT NULL DEFAULT '',
    fetched_at INTEGER NOT NULL,
    PRIMARY KEY (provider, query, title, price, sold_date)
);
CREATE INDEX IF NOT EXISTS idx_sold_sales_query ON sold_sales(query);
CREATE TABLE IF NOT EXISTS comp_cache (
    provider TEXT NOT NULL,
    query TEXT NOT NULL,
    fetched_at INTEGER NOT NULL,
    payload TEXT NOT NULL,
    PRIMARY KEY (provider, query)
);
"""

# Sprint 2 columns, added in place so existing arbitrage.db files upgrade without data loss.
MIGRATIONS = {
    "listing_type": "TEXT",  # 'buy_it_now' | 'auction'
    "end_time": "TIMESTAMP",  # ISO-8601 UTC
    "location": "TEXT",
    "gross_margin_pct": "REAL",  # (est resale - cost) / est resale * 100
    "net_profit_usd": "REAL",
    "is_locked": "INTEGER",  # 1 if the title mentions a lock / parts-only condition
    "is_untested": "INTEGER",  # 1 if untested / as-is / no power / spares
    "unverified_telecom": "INTEGER",  # 1 = government telecom boilerplate present: IMEI/lock/blacklist status unverified
    "liquidity_days": "REAL",  # expected days to sell at the expected price
    "liquidity_grade": "TEXT",  # A (<=14d) B (<=30d) C (<=60d) D (slower)
    "url": "TEXT",  # listing link, for the --watchlist view
    "updated_at": "INTEGER",  # last time we valued it (so a stored bid shows its age)
    "fix_cost": "REAL",  # est. cost of missing parts, included in total cost
    "detail_notes": "TEXT",  # what the listing detail page said (tests, lock status, missing parts)
    "retail_price": "REAL",  # new retail price (Best Buy) used to cap the estimate, if found
    "comp_source": "TEXT",  # sold | active | mock: what the valuation was based on
}


def connect(path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA)
    have = {r["name"] for r in conn.execute("PRAGMA table_info(listings)")}
    for col, typ in MIGRATIONS.items():
        if col not in have:
            conn.execute(f"ALTER TABLE listings ADD COLUMN {col} {typ}")
    have_trades = {r["name"] for r in conn.execute("PRAGMA table_info(trades)")}
    for col, typ in (("listed_at", "INTEGER"), ("list_price", "REAL")):  # when it went up for sale, and at what price
        if col not in have_trades:
            conn.execute(f"ALTER TABLE trades ADD COLUMN {col} {typ}")
    have_cat = {r["name"] for r in conn.execute("PRAGMA table_info(catalog)")}
    if "domain" not in have_cat:
        conn.execute("ALTER TABLE catalog ADD COLUMN domain TEXT")  # added after the first fashion-only catalog
    have_bp = {r["name"] for r in conn.execute("PRAGMA table_info(bidprowl_lots)")}
    if have_bp and "band" not in have_bp:
        conn.execute("ALTER TABLE bidprowl_lots ADD COLUMN band TEXT DEFAULT 'near'")  # 'near' (<= radius) or 'far' (radius..far radius)
    if "location" not in have_cat:
        conn.execute("ALTER TABLE catalog ADD COLUMN location TEXT")  # where the lot is (pickup-only sites)
    if "description" not in have_cat:
        conn.execute("ALTER TABLE catalog ADD COLUMN description TEXT")  # lot details (units, models) for surplus lots
    if not conn.execute("SELECT 1 FROM sold_sales LIMIT 1").fetchone():  # keep what the 24h cache already holds
        import json
        for prov, q, ts, payload in conn.execute("SELECT provider, query, fetched_at, payload FROM comp_cache").fetchall():
            try:
                rows = json.loads(payload)
            except ValueError:
                continue
            conn.executemany("INSERT OR IGNORE INTO sold_sales VALUES (?,?,?,?,?,?)",
                             [(prov, q, t, float(p), dt or "", ts) for t, p, dt in rows])
    conn.execute("CREATE INDEX IF NOT EXISTS idx_listings_status ON listings(status)")
    conn.commit()
    return conn


def is_seen(conn: sqlite3.Connection, platform: str, listing_id: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM listings WHERE platform=? AND listing_id=?", (platform, listing_id)
    ).fetchone()
    return row is not None


def record(
    conn: sqlite3.Connection,
    *,
    platform: str,
    listing_id: str,
    title: str,
    price: float,
    shipping_cost: float,
    status: str,
    estimated_market_value: float | None = None,
    profit_margin: float | None = None,
    listing_type: str | None = None,
    end_time: datetime | None = None,
    location: str | None = None,
    gross_margin_pct: float | None = None,
    net_profit_usd: float | None = None,
    is_locked: bool = False,
    is_untested: bool = False,
    comp_source: str | None = None,
    retail_price: float | None = None,
    fix_cost: float | None = None,
    detail_notes: str | None = None,
    url: str | None = None,
    liquidity_days: float | None = None,
    liquidity_grade: str | None = None,
    unverified_telecom: bool = False,
) -> None:
    conn.execute(
        """INSERT OR REPLACE INTO listings
           (listing_id, platform, title, price, shipping_cost, estimated_market_value,
            profit_margin, first_seen_timestamp, status, listing_type, end_time, location,
            gross_margin_pct, net_profit_usd, is_locked, is_untested, comp_source, retail_price, fix_cost, detail_notes, url, updated_at, liquidity_days, liquidity_grade, unverified_telecom)
           VALUES (?,?,?,?,?,?,?,
                   COALESCE((SELECT first_seen_timestamp FROM listings
                             WHERE platform=? AND listing_id=?), ?),
                   ?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (listing_id, platform, title, price, shipping_cost, estimated_market_value,
         profit_margin, platform, listing_id, int(time.time()), status, listing_type,
         end_time.isoformat() if end_time else None, location, gross_margin_pct,
         net_profit_usd, int(is_locked), int(is_untested), comp_source, retail_price, fix_cost, detail_notes, url, int(time.time()), liquidity_days, liquidity_grade, int(unverified_telecom)),
    )
    conn.commit()


def get_row(conn: sqlite3.Connection, platform: str, listing_id: str) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT status, price, estimated_market_value FROM listings WHERE platform=? AND listing_id=?",
        (platform, listing_id)).fetchone()


def get_status(conn: sqlite3.Connection, platform: str, listing_id: str) -> str | None:
    row = conn.execute(
        "SELECT status FROM listings WHERE platform=? AND listing_id=?", (platform, listing_id)
    ).fetchone()
    return row["status"] if row else None
