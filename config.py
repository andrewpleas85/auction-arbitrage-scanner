"""Central configuration, loaded from environment / .env."""
import os
from dataclasses import dataclass, field

from dotenv import load_dotenv

from engine.details import parse_fix_costs

load_dotenv()


def _f(name: str, default: float) -> float:
    return float(os.getenv(name, default))


def _list(name: str, default: str) -> list[str]:
    return [s.strip() for s in os.getenv(name, default).split(",") if s.strip()]


@dataclass(frozen=True)
class Config:
    # Credentials
    reverb_token: str = os.getenv("REVERB_API_TOKEN", "")
    ebay_client_id: str = os.getenv("EBAY_CLIENT_ID") or os.getenv("EBAY_APP_ID", "")
    ebay_client_secret: str = os.getenv("EBAY_CLIENT_SECRET", "")
    ebay_use_insights: bool = os.getenv("EBAY_USE_MARKETPLACE_INSIGHTS", "1") == "1"
    # Sold-price data (https://sold-comps.com, free tier = 100 requests/month; paid from $9).
    # Best Buy Products API (developer.bestbuy.com): NEW retail price as a ceiling on resale value.
    bestbuy_api_key: str = os.getenv("BESTBUY_API_KEY", "")
    retail_cap_fraction: float = _f("RETAIL_CAP_FRACTION", 0.85)  # resale value <= this x new price
    soldcomps_api_key: str = (os.getenv("SOLDCOMPS_API_KEY") or os.getenv("SOLD_COMPS_API_KEY") or "").strip().strip("\"'")
    # Paid IMEI checks (sickw.com): only run for items worth >= $100 whose listing prints a valid IMEI.
    sickw_api_key: str = os.getenv("SICKW_API_KEY", "")
    sickw_service_id: str = os.getenv("SICKW_SERVICE_ID", "")  # which SickW service (must return Find My + blacklist)
    enable_paid_imei_checks: bool = os.getenv("ENABLE_PAID_IMEI_CHECKS", "1") == "1"
    soldcomps_cache_ttl_hours: float = _f("SOLDCOMPS_CACHE_TTL_HOURS", 168)
    etsy_api_key: str = os.getenv("ETSY_API_KEY", "")
    etsy_shared_secret: str = os.getenv("ETSY_SHARED_SECRET", "")
    discord_webhook_url: str = os.getenv("DISCORD_WEBHOOK_URL", "")

    # Supply
    sources: list[str] = field(
        default_factory=lambda: _list("SOURCES", "reverb,shopgoodwill,sgw_jewelry,sgw_fashion,gsa,propertyroom,ebay,govdeals,bstock")
    )
    reverb_categories: list[str] = field(
        default_factory=lambda: _list("REVERB_CATEGORIES", "pro-audio,amps,synths")
    )
    # eBay Browse API search terms for items to BUY (free keys; ~5,000 calls/day). Each term runs one
    # Buy It Now (newest) and one auction (ending soonest) search.
    ebay_queries: list[str] = field(
        default_factory=lambda: _list("EBAY_QUERIES", "oscilloscope,fluke multimeter,vintage camera lens"))
    etsy_queries: list[str] = field(
        default_factory=lambda: _list("ETSY_QUERIES", "vintage synthesizer,vintage camera,vintage amplifier")
    )
    poll_interval: int = int(_f("POLL_INTERVAL_SECONDS", 600))
    # Buy price ABOVE this is dropped. There is no minimum buy price: auctions open at $1.
    max_price: float = _f("MAX_PRICE_USD", 1500)
    # Floor on the ESTIMATED RESALE VALUE (after condition haircut), not on the current bid.
    min_market_value: float = _f("MIN_MARKET_VALUE_USD", 40)
    max_comps_per_cycle: int = int(_f("MAX_COMPS_PER_CYCLE", 40))

    # Valuation
    min_comps: int = int(_f("MIN_COMPS", 5))
    resale_fee_pct: float = _f("RESALE_FEE_PCT", 13.25)
    resale_fixed_fee: float = _f("RESALE_FIXED_FEE_USD", 0.30)
    outbound_shipping: float = _f("OUTBOUND_SHIPPING_USD", 15)
    # Sold comps: use the most recent N matching sales, drop high+low, average the rest.
    sold_comps_max: int = int(_f("SOLD_COMPS_MAX", 10))
    # Active asking prices are NOT sold data. Off by default; if enabled they are haircut.
    allow_active_comps: bool = os.getenv("ALLOW_ACTIVE_COMPS", "0") == "1"
    active_ask_discount: float = _f("ACTIVE_ASK_DISCOUNT", 0.85)

    # Alert thresholds
    min_profit: float = _f("MIN_PROFIT_USD", 50)
    min_margin_pct: float = _f("MIN_MARGIN_PCT", 35)

    # ShopGoodwill: top-level category ids (13 instruments, 170 cameras, 7 electronics, 4 collectibles)
    sgw_category_ids: list[str] = field(
        default_factory=lambda: _list("SHOPGOODWILL_CATEGORY_IDS", "13,170,7,4")
    )
    sgw_pages: int = int(_f("SHOPGOODWILL_PAGES", 3))
    auction_window_hours: float = _f("AUCTION_WINDOW_HOURS", 2)
    # Auctions are only priced/alerted at the CURRENT bid, and only inside this window.
    ending_window_minutes: float = _f("ENDING_WINDOW_MINUTES", 15)
    ending_poll_seconds: int = int(_f("ENDING_POLL_SECONDS", 60))
    sgw_recent_pages: int = int(_f("SHOPGOODWILL_RECENT_PAGES", 3))
    sgw_ending_max_pages: int = int(_f("SHOPGOODWILL_ENDING_MAX_PAGES", 5))
    discord_ping_here: bool = os.getenv("DISCORD_PING_HERE", "0") == "1"
    # Watchlisted auctions are re-valued early when the bid crosses these $ levels or nears 50% of value.
    watchlist_bid_thresholds: list[str] = field(
        default_factory=lambda: _list("WATCHLIST_BID_THRESHOLDS", "10,25,50"))
    watchlist_value_fraction: float = _f("WATCHLIST_VALUE_FRACTION", 0.5)
    watchlist_approach: float = _f("WATCHLIST_APPROACH", 0.9)
    # Liquidity: will it SELL? (engine/liquidity.py: a heuristic, not yet validated on our own sales)
    liquidity_enabled: bool = os.getenv("LIQUIDITY_ENABLED", "1") == "1"
    max_days_to_sell: float = _f("MAX_DAYS_TO_SELL", 60)  # skip reliably slow items (needs real sold data behind the estimate)
    carry_rate: float = _f("CARRY_RATE_PER_YEAR", 0.25)  # cost of money/space/risk while it sits (vendors quote 20-30%)
    # Precious-metal melt value (needs no sold comps): spot x weight x purity x payout.
    melt_enabled: bool = os.getenv("MELT_ENABLED", "1") == "1"
    melt_payout_scrap: float = _f("MELT_PAYOUT_SCRAP", 0.85)  # what a refiner/dealer typically pays of melt
    melt_payout_bullion: float = _f("MELT_PAYOUT_BULLION", 0.95)
    melt_ship_usd: float = _f("MELT_SHIP_USD", 12)  # insured shipping to the refiner
    # ShopGoodwill: 353 = Precious Metal Scrap, 6 = all Jewelry & Gemstones. Only listings that state metal + weight are kept.
    sgw_jewelry_category_ids: list[str] = field(default_factory=lambda: _list("SHOPGOODWILL_JEWELRY_CATEGORY_IDS", "353,6"))
    # Designer clothing / bags / shoes (authenticity is ASSUMED, not checked). 103 purses, 164 accessories, 161/162 shoes,
    # 150 suits, 357 blazers, 154 outerwear, 167 vintage menswear.
    sgw_fashion_category_ids: list[str] = field(
        default_factory=lambda: _list("SHOPGOODWILL_FASHION_CATEGORY_IDS", "103,164,161,162,150,357,154,167"))
    # Rough replacement cost of commonly missing parts, subtracted from profit ("power adapter=25,remote=12,...").
    fix_costs: dict = field(default_factory=lambda: parse_fix_costs(os.getenv("FIX_COSTS", "")))
    # Untested / as-is / for-parts items resell for less.
    risky_condition_factor: float = _f("RISKY_CONDITION_FACTOR", 0.6)

    # GovDeals / PublicSurplus: RSS feed URLs you supply (see README note) + title keyword filter
    govdeals_feed_urls: list[str] = field(default_factory=lambda: _list("GOVDEALS_FEED_URLS", ""))
    govdeals_keywords: list[str] = field(default_factory=lambda: _list(
        "GOVDEALS_KEYWORDS",
        "oscilloscope,analyzer,centrifuge,microscope,espresso,coffee,switch,cisco,fluke,tektronix,"
        "projector,amplifier,mixer,receiver,camera,lens,server,rack"))
    govdeals_home_location: str = os.getenv("GOVDEALS_HOME_LOCATION", "")
    # Most surplus is pickup-only; assumed freight cost when the item isn't near home.
    govdeals_freight_usd: float = _f("GOVDEALS_FREIGHT_USD", 75)
    govdeals_premium_pct: float = _f("GOVDEALS_BUYER_PREMIUM_PCT", 10)

    # Condition weighting on true market value: working 1.0, refurbished/mint 1.10, untested 0.6, locked/parts 0.2.
    refurbished_condition_factor: float = _f("REFURBISHED_CONDITION_FACTOR", 1.10)
    locked_condition_factor: float = _f("LOCKED_CONDITION_FACTOR", 0.2)
    # Cellular devices (phones, cellular tablets/watches), as a share of a CLEAN-device market comp:
    #   clean (listing states Clean IMEI / Factory Unlocked) 100%; moderate (nothing verified, or reset/setup screen) 50%;
    #   unverified (government telecom disclaimer, or untested/unable to test) 20% = assume Bad ESN / blacklisted / locked, i.e. parts value.
    telecom_factor_clean: float = _f("TELECOM_FACTOR_CLEAN", 1.0)
    telecom_factor_moderate: float = _f("TELECOM_FACTOR_MODERATE", 0.5)
    telecom_factor_unverified: float = _f("TELECOM_FACTOR_UNVERIFIED", 0.2)

    # GSA Auctions (official feed). DEMO_KEY allows only 10 requests/hour, so the feed is cached.
    gsa_api_key: str = os.getenv("GSA_API_KEY", "DEMO_KEY")
    gsa_min_interval: int = int(_f("GSA_MIN_INTERVAL_SECONDS", 1800))
    # Lots closing today are re-read this often (bypasses the 30-minute cache). DEMO_KEY = 10 requests/hour,
    # so keep >= 600 unless you have your own api.data.gov key (1,000/hour): then 120 is fine.
    gsa_closing_interval: int = int(_f("GSA_CLOSING_DAY_INTERVAL_SECONDS", 600))
    gsa_premium_pct: float = _f("GSA_BUYER_PREMIUM_PCT", 0)
    # Most GSA lots are inspection/pickup only; assumed freight added to the buy cost.
    gsa_freight_usd: float = _f("GSA_FREIGHT_USD", 10)

    # PropertyRoom (police seizures): category slugs from propertyroom.com/c/<slug>
    propertyroom_categories: list[str] = field(
        default_factory=lambda: _list("PROPERTYROOM_CATEGORIES", "electronics,computers,tools-and-equipment,jewelry,coins,fashion")
    )
    propertyroom_pages: int = int(_f("PROPERTYROOM_PAGES", 2))
    propertyroom_premium_pct: float = _f("PROPERTYROOM_BUYER_PREMIUM_PCT", 15)
    propertyroom_shipping_usd: float = _f("PROPERTYROOM_SHIPPING_USD", 15)
    sgw_closed_categories: tuple = ("7", "170", "13", "4", "103", "164", "161")  # electronics, cameras, instruments, collectibles, purses, accessories, shoes
    bidprowl_api_key: str = os.getenv("BIDPROWL_API_KEY", "")
    bidprowl_radius_miles: int = int(_f("BIDPROWL_RADIUS_MILES", 25))  # lots always shown within this many miles
    bidprowl_far_radius_miles: int = int(_f("BIDPROWL_FAR_RADIUS_MILES", 100))  # "worth a drive" lots shown out to here
    ship_to_zip: str = os.getenv("SHIP_TO_ZIP", "")  # your ZIP and state: lets the site fetch real ShopGoodwill shipping quotes
    ship_to_state: str = os.getenv("SHIP_TO_STATE", "")

    # B-Stock: local manifest dir (JSON/CSV you export yourself)
    bstock_manifest_dir: str = os.getenv("BSTOCK_MANIFEST_DIR", "manifests")
    bstock_premium_pct: float = _f("BSTOCK_BUYER_PREMIUM_PCT", 10)
    # Returns lots: share of manifest value you realistically recover (non-working, unsold, etc.)
    lot_haircut: float = _f("LOT_HAIRCUT", 0.6)
    max_lot_lines: int = int(_f("MAX_LOT_LINES", 60))

    web_dir: str = os.getenv("WEB_DIR", "site")  # where --web writes index.html
    db_path: str = os.getenv("DB_PATH", "arbitrage.db")
