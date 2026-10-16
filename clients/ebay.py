"""eBay sold-comps client.

eBay's Finding API (`findCompletedItems`) was decommissioned on 2025-02-05, and the Browse API
only returns ACTIVE listings. The only official source of sold prices is the Marketplace
Insights API (`item_sales/search`), which eBay grants case by case (contact Developer
Technical Support). Until that is approved this client raises SoldDataUnavailable instead of
silently valuing items off asking prices. Set ALLOW_ACTIVE_COMPS=1 to opt back into
discounted active asks (flagged source="active").
"""
import asyncio
import base64
import hashlib
import json
import logging
import sqlite3
import time
from dataclasses import dataclass
from datetime import datetime, timezone

import httpx

from models import Listing

TOKEN_URL = "https://api.ebay.com/identity/v1/oauth2/token"
BROWSE_URL = "https://api.ebay.com/buy/browse/v1/item_summary/search"
INSIGHTS_URL = "https://api.ebay.com/buy/marketplace_insights/v1_beta/item_sales/search"
SCOPE_BASE = "https://api.ebay.com/oauth/api_scope"
SCOPE_INSIGHTS = f"{SCOPE_BASE}/buy.marketplace.insights"
_EPOCH = datetime.min.replace(tzinfo=timezone.utc)
log = logging.getLogger("arbitrage.ebay")


class SoldDataUnavailable(RuntimeError):
    """Account has no Marketplace Insights access (or it is disabled)."""


@dataclass
class Comp:
    title: str
    price: float
    sold_date: datetime | None = None


@dataclass
class Comps:
    source: str  # "sold" | "active"
    items: list[Comp]


def _parse_date(raw: str | None) -> datetime | None:
    if not raw:
        return None
    try:
        return datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None


class EbayClient:
    def __init__(self, http: httpx.AsyncClient, client_id: str, client_secret: str,
                 use_insights: bool = True, allow_active: bool = False):
        self._http = http
        self._auth = base64.b64encode(f"{client_id}:{client_secret}".encode()).decode()
        self._use_insights = use_insights
        self._allow_active = allow_active
        self._token = ""
        self._token_exp = 0.0
        self._search_cache: dict[tuple[str, bool], tuple[float, list[Listing]]] = {}

    async def _get_token(self, insights: bool) -> str:
        if self._token and time.time() < self._token_exp - 60 and self._token_insights == insights:
            return self._token
        scope = f"{SCOPE_BASE} {SCOPE_INSIGHTS}" if insights else SCOPE_BASE
        resp = await self._http.post(
            TOKEN_URL,
            headers={"Authorization": f"Basic {self._auth}"},
            data={"grant_type": "client_credentials", "scope": scope},
        )
        if resp.status_code in (400, 401) and insights:
            raise SoldDataUnavailable("keyset not authorised for the Marketplace Insights scope")
        resp.raise_for_status()
        body = resp.json()
        self._token, self._token_insights = body["access_token"], insights
        self._token_exp = time.time() + int(body["expires_in"])
        return self._token

    _token_insights = False

    async def comps(self, query: str, limit: int = 50) -> Comps:
        """Recent sold comps for `query`, newest first."""
        if self._use_insights:
            try:
                return await self._sold(query, limit)
            except SoldDataUnavailable:
                if not self._allow_active:
                    raise
        elif not self._allow_active:
            raise SoldDataUnavailable("EBAY_USE_MARKETPLACE_INSIGHTS=0 and ALLOW_ACTIVE_COMPS=0")
        return await self._active(query, limit)

    # ---------- supply: Browse API search for items to BUY ----------
    async def new_listings(self, query: str, auction: bool = False, limit: int = 50,
                           max_price: float = 1500, ttl: float = 300) -> list[Listing]:
        """Active US listings matching `query`: Buy It Now newest-first, or auctions ending-soonest.

        Auctions are priced at the CURRENT bid (itemEndDate gives the real end time). Browse allows
        ~5,000 calls/day, so results are cached for `ttl` seconds per (query, kind).
        """
        key = (query, auction)
        hit = self._search_cache.get(key)
        if hit and time.time() - hit[0] < ttl:
            return hit[1]
        flt = (f"buyingOptions:{{{'AUCTION' if auction else 'FIXED_PRICE'}}},deliveryCountry:US,"
               f"itemLocationCountry:US,price:[1..{max_price:g}],priceCurrency:USD")
        headers = {"Authorization": f"Bearer {await self._get_token(False)}",
                   "X-EBAY-C-MARKETPLACE-ID": "EBAY_US"}
        resp = await self._http.get(BROWSE_URL, headers=headers, params={
            "q": query, "limit": limit, "filter": flt,
            "sort": "endingSoonest" if auction else "newlyListed"})
        self._check_rate_limit(resp)
        resp.raise_for_status()
        listings = [l for raw in resp.json().get("itemSummaries", []) if (l := self.parse_summary(raw, query))]
        self._search_cache[key] = (time.time(), listings)
        return listings

    async def item_live(self, item_id: str) -> dict:
        """Current price, bid count and end time of one listing (Browse getItem); {} when it can't be read, ended=True when it is over."""
        headers = {"Authorization": f"Bearer {await self._get_token(False)}", "X-EBAY-C-MARKETPLACE-ID": "EBAY_US"}
        resp = await self._http.get(f"{BROWSE_URL.rsplit('/item_summary', 1)[0]}/item/{item_id}", headers=headers)
        if resp.status_code == 404:
            return {"ended": True}
        self._check_rate_limit(resp)
        resp.raise_for_status()
        raw = resp.json()
        money = raw.get("currentBidPrice") or raw.get("price") or {}
        if money.get("currency") != "USD" or not money.get("value"):
            return {}
        end = _parse_date(raw.get("itemEndDate"))
        return {"price": float(money["value"]), "bids": raw.get("bidCount"), "end": end,
                "ended": raw.get("estimatedAvailabilities", [{}])[0].get("estimatedAvailabilityStatus") == "OUT_OF_STOCK"
                         or bool(end and end <= datetime.now(timezone.utc))}

    @staticmethod
    def parse_active(payload: dict) -> list[dict]:
        """Browse itemSummaries -> competition rows: price, watchers (watchCount), age (itemCreationDate), offers."""
        out = []
        for raw in payload.get("itemSummaries", []):
            price = raw.get("price") or {}
            if price.get("currency") != "USD" or not price.get("value"):
                continue
            out.append({"title": raw.get("title", ""), "price": float(price["value"]), "watchers": raw.get("watchCount"),
                        "created": _parse_date(raw.get("itemCreationDate")),
                        "accepts_offers": "BEST_OFFER" in (raw.get("buyingOptions") or [])})
        return out

    async def active_listings(self, query: str, limit: int = 200, ttl: float = 3600) -> list[dict] | None:
        """Active US fixed-price listings for `query` (competition: how many, how old, how asked). Cached; None on failure."""
        key = (query, "active")
        hit = self._search_cache.get(key)
        if hit and time.time() - hit[0] < ttl:
            return hit[1]
        try:
            headers = {"Authorization": f"Bearer {await self._get_token(False)}", "X-EBAY-C-MARKETPLACE-ID": "EBAY_US"}
            resp = await self._http.get(BROWSE_URL, headers=headers, params={
                "q": query, "limit": limit, "filter": "deliveryCountry:US,buyingOptions:{FIXED_PRICE},priceCurrency:USD"})
            self._check_rate_limit(resp)
            resp.raise_for_status()
        except httpx.HTTPError as e:
            log.warning("eBay active-listings lookup failed for %r: %s", query, e)
            return None
        rows = self.parse_active(resp.json())
        self._search_cache[key] = (time.time(), rows)
        return rows

    @staticmethod
    def parse_summary(raw: dict, query: str = "") -> Listing | None:
        """Browse `itemSummaries` entry -> Listing. None if not USD or unpriced."""
        options = raw.get("buyingOptions") or []
        is_auction = "AUCTION" in options
        money = (raw.get("currentBidPrice") if is_auction and raw.get("currentBidPrice") else raw.get("price")) or {}
        if money.get("currency") != "USD" or not money.get("value"):
            return None
        ship = ((raw.get("shippingOptions") or [{}])[0].get("shippingCost") or {})
        ship_type = ((raw.get("shippingOptions") or [{}])[0]).get("shippingCostType")
        loc = raw.get("itemLocation") or {}
        return Listing(
            platform="ebay",
            listing_id=raw["itemId"],
            title=raw["title"],
            price=float(money["value"]),
            shipping_cost=float(ship.get("value") or 0) if ship.get("currency", "USD") == "USD" else 0.0,
            url=raw["itemWebUrl"],
            image_url=(raw.get("image") or {}).get("imageUrl"),
            category=query,
            listing_type="auction" if is_auction else "buy_it_now",
            end_time=_parse_date(raw.get("itemEndDate")) if is_auction else None,
            location=", ".join(p for p in (loc.get("city"), loc.get("stateOrProvince")) if p) or None,
            extra={"bid_count": raw.get("bidCount"), "condition": raw.get("condition"),
                   "shipping_cost_type": ship_type,  # CALCULATED => cost unknown, treated as $0
                   "seller_feedback": (raw.get("seller") or {}).get("feedbackPercentage")},
        )

    @staticmethod
    def _check_rate_limit(resp: httpx.Response) -> None:
        if resp.status_code == 429:
            log.warning("eBay rate limit hit (429); this lookup is skipped and retried next cycle. "
                        "Lower MAX_COMPS_PER_CYCLE or raise POLL_INTERVAL_SECONDS.")

    async def _sold(self, query: str, limit: int) -> Comps:
        headers = {"Authorization": f"Bearer {await self._get_token(True)}",
                   "X-EBAY-C-MARKETPLACE-ID": "EBAY_US"}
        resp = await self._http.get(INSIGHTS_URL, headers=headers,
                                    params={"q": query, "limit": limit})
        if resp.status_code in (401, 403):
            raise SoldDataUnavailable(f"Marketplace Insights returned {resp.status_code}")
        self._check_rate_limit(resp)
        resp.raise_for_status()
        items = [
            Comp(s["title"], float(s["lastSoldPrice"]["value"]), _parse_date(s.get("lastSoldDate")))
            for s in resp.json().get("itemSales", [])
            if s.get("lastSoldPrice")
        ]
        items.sort(key=lambda c: c.sold_date or _EPOCH, reverse=True)
        return Comps("sold", items)

    async def _active(self, query: str, limit: int) -> Comps:
        headers = {"Authorization": f"Bearer {await self._get_token(False)}",
                   "X-EBAY-C-MARKETPLACE-ID": "EBAY_US"}
        resp = await self._http.get(
            BROWSE_URL, headers=headers,
            params={"q": query, "limit": limit,
                    "filter": "deliveryCountry:US,buyingOptions:{FIXED_PRICE}"},
        )
        self._check_rate_limit(resp)
        resp.raise_for_status()
        return Comps("active", [
            Comp(s["title"], float(s["price"]["value"]))
            for s in resp.json().get("itemSummaries", [])
            if s.get("price", {}).get("currency") == "USD"
        ])


class MockEbayClient:
    """Fabricated comps for pipeline testing (`main.py --use-mock-comps`). NOT real market data.

    Price is a deterministic function of the query ($30-$600), so the same item always gets the
    same value. Returns source="mock"; the scanner forces dry-run so nothing is ever alerted.
    """

    async def comps(self, query: str, limit: int = 50) -> Comps:
        base = 30 + int(hashlib.md5(query.encode()).hexdigest()[:6], 16) % 570
        now = datetime.now(timezone.utc)
        spread = (-0.1, -0.05, 0.0, 0.0, 0.05, 0.1, 0.15, -0.15)
        return Comps("mock", [Comp(query, round(base * (1 + d), 2), now) for d in spread])


class CompCache:
    """Persistent comp cache in SQLite, so restarts don't re-spend a metered provider quota."""

    def __init__(self, conn: sqlite3.Connection, provider: str, ttl_seconds: float):
        self._conn, self._provider, self._ttl = conn, provider, ttl_seconds

    def get(self, query: str) -> list[Comp] | None:
        row = self._conn.execute(
            "SELECT fetched_at, payload FROM comp_cache WHERE provider=? AND query=?",
            (self._provider, query)).fetchone()
        if not row or time.time() - row[0] > self._ttl:
            return None
        return [Comp(t, p, _parse_date(d)) for t, p, d in json.loads(row[1])]

    def stored(self, query: str) -> list[Comp]:
        """Every sale ever saved for this query, whatever its age (no API call)."""
        rows = self._conn.execute("SELECT title, price, sold_date FROM sold_sales WHERE provider=? AND query=?",
                                  (self._provider, query)).fetchall()
        return [Comp(t, p, _parse_date(d) if d else None) for t, p, d in rows]

    def put(self, query: str, items: list[Comp]) -> None:
        now = int(time.time())
        self._conn.executemany(
            "INSERT OR IGNORE INTO sold_sales (provider, query, title, price, sold_date, fetched_at) VALUES (?,?,?,?,?,?)",
            [(self._provider, query, c.title, c.price, c.sold_date.isoformat() if c.sold_date else "", now) for c in items])
        payload = json.dumps([[c.title, c.price, c.sold_date.isoformat() if c.sold_date else None]
                              for c in items])
        self._conn.execute(
            "INSERT OR REPLACE INTO comp_cache (provider, query, fetched_at, payload) VALUES (?,?,?,?)",
            (self._provider, query, int(time.time()), payload))
        self._conn.commit()


class SoldCompsClient:
    """Real eBay SOLD listings via the SoldComps API (https://api.sold-comps.com/v1/scrape).

    One request = one keyword search = up to `count` sold listings; billed per request, so results
    are cached (persistently when a CompCache is given). Best-offer sales are dropped because the
    listed `soldPrice` is not what the buyer paid. 401 / monthly-quota errors raise
    SoldDataUnavailable so the router can fall back.
    """

    BASE = "https://api.sold-comps.com"

    def __init__(self, http: httpx.AsyncClient, api_key: str, cache: CompCache | None = None,
                 count: int = 60):
        self._http = http
        self._headers = {"Authorization": f"Bearer {api_key}"}
        self._cache = cache
        self._count = count
        self.blocked_reason: str | None = None  # set once the key is rejected / quota is spent
        self._active_cache: dict[str, tuple[float, list[dict]]] = {}

    @staticmethod
    def parse(payload: dict) -> list[Comp]:
        items = []
        for it in payload.get("items", []):
            if it.get("bestOfferAccepted") or (it.get("soldCurrency") or "USD") != "USD":
                continue
            try:
                price = float(it["soldPrice"])
            except (KeyError, TypeError, ValueError):
                continue
            if price > 0 and it.get("title"):
                items.append(Comp(it["title"], price, _parse_date(it.get("endedAt"))))
        items.sort(key=lambda c: c.sold_date or _EPOCH, reverse=True)
        return items

    @staticmethod
    def parse_active(payload: dict) -> list[dict]:
        """sold=false items -> competition rows. watcherCount / unitsSold / acceptsOffers come straight from SoldComps."""
        out = []
        for it in payload.get("items", []):
            if (it.get("currentCurrency") or "USD") != "USD":
                continue
            try:
                price = float(it.get("currentPrice") or it.get("totalPrice"))
            except (TypeError, ValueError):
                continue
            out.append({"title": it.get("title", ""), "price": price, "watchers": it.get("watcherCount"),
                        "units_sold": it.get("unitsSold"), "created": None, "accepts_offers": it.get("acceptsOffers")})
        return out

    async def active(self, query: str, ttl_hours: float = 24) -> list[dict] | None:
        """What is for sale NOW (one extra request, cached). Used for finalists only: the free tier is 100 requests/month."""
        if self.blocked_reason:
            return None
        cached = self._cache_active(query)
        if cached is not None:
            return cached
        try:
            resp = await self._http.get(f"{self.BASE}/v1/scrape", headers=self._headers, timeout=60,
                                        params={"keyword": query, "count": self._count, "sold": "false", "ebaySite": "ebay.com"})
            if resp.status_code in (401, 429):
                self.blocked_reason = f"SoldComps returned {resp.status_code}"
                return None
            resp.raise_for_status()
        except httpx.HTTPError as e:
            log.warning("SoldComps active lookup failed for %r: %s", query, e)
            return None
        rows = self.parse_active(resp.json())
        self._store_active(query, rows)
        return rows

    def _cache_active(self, query: str) -> list[dict] | None:
        got = self._active_cache.get(query)
        return got[1] if got and time.time() - got[0] < 86400 else None

    def _store_active(self, query: str, rows: list[dict]) -> None:
        self._active_cache[query] = (time.time(), rows)

    async def comps(self, query: str, limit: int = 60) -> Comps:
        if self.blocked_reason:
            raise SoldDataUnavailable(self.blocked_reason)
        cached = self._cache.get(query) if self._cache else None
        if cached is not None:
            return Comps("sold", cached)
        params = {"keyword": query, "count": self._count, "sold": "true", "ebaySite": "ebay.com"}
        for attempt in (1, 2):
            resp = await self._http.get(f"{self.BASE}/v1/scrape", headers=self._headers, params=params,
                                        timeout=60)
            if resp.status_code == 401:
                self.blocked_reason = "SoldComps rejected the API key (401)"
                raise SoldDataUnavailable(self.blocked_reason)
            if resp.status_code == 429:
                code = (resp.json() if resp.content else {}).get("code")
                if code == "quota_exceeded":
                    self.blocked_reason = "SoldComps monthly quota exhausted"
                    raise SoldDataUnavailable(self.blocked_reason)
                wait = min(float(resp.headers.get("Retry-After", 5)), 30)  # per-minute limit
                log.warning("SoldComps rate limited; waiting %.0fs", wait)
                if attempt == 2:
                    resp.raise_for_status()
                await asyncio.sleep(wait)
                continue
            break
        resp.raise_for_status()
        items = self.parse(resp.json())
        if self._cache:
            self._cache.put(query, items)
        return Comps("sold", items)


class CompsRouter:
    """Sold-data provider order: SoldComps, then eBay (Marketplace Insights, and discounted active
    asks only if ALLOW_ACTIVE_COMPS is on). Falls through when a provider is unavailable."""

    def __init__(self, soldcomps: SoldCompsClient | None, ebay: EbayClient | None):
        self.soldcomps, self.ebay = soldcomps, ebay
        self._warned = False

    async def active(self, query: str) -> list[dict] | None:
        """Competition data (active listings) from whichever provider has it; None when nobody does."""
        if self.soldcomps and not self.soldcomps.blocked_reason:
            rows = await self.soldcomps.active(query)
            if rows is not None:
                return rows
        if self.ebay:
            return await self.ebay.active_listings(query)
        return None

    async def comps(self, query: str, limit: int = 50) -> Comps:
        if self.soldcomps and not self.soldcomps.blocked_reason:
            try:
                return await self.soldcomps.comps(query)
            except SoldDataUnavailable as e:
                if not self._warned:
                    log.warning("SoldComps unavailable (%s); falling back to eBay data", e)
                    self._warned = True
        if self.ebay:
            return await self.ebay.comps(query, limit)
        raise SoldDataUnavailable("no sold-comps provider available")


def build_comps_provider(cfg, http: httpx.AsyncClient, conn: sqlite3.Connection) -> CompsRouter:
    """SoldComps (if SOLDCOMPS_API_KEY is set) first, then eBay (Insights / discounted active asks)."""
    soldcomps = None
    if cfg.soldcomps_api_key:
        cache = CompCache(conn, "soldcomps", cfg.soldcomps_cache_ttl_hours * 3600)
        soldcomps = SoldCompsClient(http, cfg.soldcomps_api_key, cache)
    ebay = None
    if cfg.ebay_client_id and cfg.ebay_client_secret:
        ebay = EbayClient(http, cfg.ebay_client_id, cfg.ebay_client_secret, cfg.ebay_use_insights,
                          cfg.allow_active_comps)
    return CompsRouter(soldcomps, ebay)
