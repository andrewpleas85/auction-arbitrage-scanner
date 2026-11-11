"""Async orchestrator.

Two loops share one SQLite state:
  * discovery loop (POLL_INTERVAL_SECONDS): new listings from every source. Buy It Now
    steals alert as CRITICAL; auctions with a projected margin are parked as 'watchlist'.
  * ending loop (ENDING_POLL_SECONDS): auctions in their final minutes (and GSA 'daily_auction'
    lots whose end DATE is today) are re-valued at the current bid and alert as WATCHLIST.

Filtering: a low current bid never drops a listing (auctions open at $1). The only value floor
is on the ESTIMATED RESALE VALUE (MIN_MARKET_VALUE_USD), applied after valuation.
"""
import argparse
import asyncio
import dataclasses
import itertools
import sqlite3
import statistics
import logging
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import httpx

import db
import integrity
import catalog
import report
import webpage
from alerting.discord import CRITICAL, alert_tier, send_alert
from clients.bstock import load_lots
from clients.ebay import Comps, MockEbayClient, SoldDataUnavailable, build_comps_provider
from clients.etsy import EtsyClient
from clients.govdeals import GovDealsClient
from clients.gsa import GSAClient
from clients.propertyroom import PropertyRoomClient
from engine.title_cleaner import extract_imei
from clients.imei import ImeiClient, cached_report, check_imei_cli, set_manual, should_check as imei_should_check
from clients.retail import BestBuyClient
from clients.metals import SpotPriceClient
from clients.reverb import ReverbClient
from clients.shopgoodwill import ShopGoodwillClient
from config import Config
from engine import knowledge, liquidity
from engine.details import title_missing_parts
from engine.melt import estimate_melt
from engine.timeleft import time_left
from engine.title_cleaner import comp_matches, build_search_query, is_designer, is_locked, is_untested
from engine.valuation import ValuationEngine
from engine.watchlist import bid_escalation, parse_thresholds
from models import Listing

log = logging.getLogger("arbitrage")
COMP_CACHE_TTL = 3600
AUCTION_TYPES = ("auction", "daily_auction")


class Scanner:
    melt_only = False  # class-level defaults keep partially-built scanners (tests) working
    spot = None

    def __init__(self, cfg: Config, http: httpx.AsyncClient, dry_run: bool, mock_comps: bool = False,
                 melt_only: bool = False):
        self.cfg = cfg
        self.http = http
        self.mock_comps = mock_comps
        self.melt_only = melt_only  # no comp source: only precious-metal (melt) listings can be valued
        self.spot = SpotPriceClient(http)
        # Mock comps are fabricated, so they can never post real alerts.
        self.dry_run = dry_run or mock_comps or not cfg.discord_webhook_url
        self.conn = db.connect(cfg.db_path)
        self.reverb = ReverbClient(http, cfg.reverb_token)
        self.ebay = MockEbayClient() if mock_comps else build_comps_provider(cfg, http, self.conn)  # comps provider
        # eBay Browse search (supply) reuses the router's EbayClient; None without eBay keys / in mock mode.
        self.ebay_search = getattr(self.ebay, "ebay", None)
        self.imei = ImeiClient(cfg, http, self.conn)
        self.retail = BestBuyClient(http, cfg.bestbuy_api_key) if cfg.bestbuy_api_key else None
        self.etsy = EtsyClient(http, cfg.etsy_api_key, cfg.etsy_shared_secret)
        self.engine = ValuationEngine(cfg)
        self.sgw = ShopGoodwillClient(http)
        self.govdeals = GovDealsClient(None, cfg.govdeals_feed_urls, cfg.govdeals_keywords,
                                       cfg.govdeals_home_location, cfg.govdeals_freight_usd,
                                       cfg.govdeals_premium_pct)
        self.gsa = GSAClient(http, cfg.gsa_api_key, cfg.gsa_min_interval, cfg.gsa_premium_pct,
                             cfg.gsa_freight_usd, cfg.gsa_closing_interval)
        self.propertyroom = PropertyRoomClient(None, cfg.propertyroom_categories, cfg.propertyroom_pages,
                                               cfg.propertyroom_premium_pct, cfg.propertyroom_shipping_usd)
        self._comp_cache: dict[str, tuple[float, Comps]] = {}
        self._ebay_gate = asyncio.Semaphore(2)  # polite concurrency toward eBay
        self._warned_no_sold = False

    async def aclose(self) -> None:
        await self.govdeals.aclose()
        await self.propertyroom.aclose()

    # ---------- valuation ----------
    async def _comps(self, query: str) -> Comps:
        hit = self._comp_cache.get(query)
        if hit and time.time() - hit[0] < COMP_CACHE_TTL:
            return hit[1]
        async with self._ebay_gate:
            comps = await self.ebay.comps(query)
            await asyncio.sleep(0.3)
        self._comp_cache[query] = (time.time(), comps)
        return comps

    def _observe(self, title: str, query: str, comps: Comps) -> None:
        """Feed the price knowledge base: what the comps said for this item (sold or asking), once a day per source."""
        if comps.source not in ("sold", "active"):
            return  # mock / unknown data never enters the knowledge base
        prices = sorted(c.price for c in comps.items if comp_matches(title, c.title))
        if not prices:
            return
        try:
            knowledge.add_observation(
                self.conn, query, "sold" if comps.source == "sold" else "ask", "comps-sold" if comps.source == "sold" else "ebay-active",
                low=prices[0], high=prices[-1], typical=statistics.median(prices), n=len(prices),
                notes="scanner comp lookup", replace_within_hours=24)
        except sqlite3.Error as e:
            log.warning("could not save observation for %r: %s", query, e)

    def _melt(self, l: Listing):
        """(MeltEstimate, spot $/oz) if this is a solid precious-metal piece we can price, else None."""
        spot = getattr(self, "spot", None)
        if not self.cfg.melt_enabled or l.lot_items or spot is None:
            return None
        est = estimate_melt(l.title)
        price = spot.prices.get(est.metal) if est else None
        return (est, price) if est and price else None

    def _lane(self, listings: list[Listing]) -> list[Listing]:
        """Keep only what each special lane can value: jewelry/coins need a stated metal + weight, fashion needs a designer name."""
        keep = []
        for l in listings:
            cat = (l.category or "").lower()
            if cat in ("jewelry", "coins", "sgw_jewelry"):
                ok = estimate_melt(l.title) is not None
            elif cat in ("fashion", "sgw_fashion"):
                ok = is_designer(l.title)
            else:
                ok = True
            if ok:
                keep.append(l)
        return keep

    async def _value(self, l: Listing, price: float | None = None):
        """Value `l` at its own price, or at `price` (e.g. its Buy It Now alternative)."""
        price = l.price if price is None else price
        melt = self._melt(l)
        if melt:
            est, spot = melt
            payout = self.cfg.melt_payout_bullion if est.bullion else self.cfg.melt_payout_scrap
            l.extra["melt"] = est.summary(spot, payout)
            return self.engine.valuate_melt(est.melt_value(spot) * payout, price, l.shipping_cost,
                                            l.extra_cost_pct, detail=l.extra["melt"])
        if l.lot_items:
            premium = price * l.extra_cost_pct / 100
            lines = []
            for item in l.lot_items[: self.cfg.max_lot_lines]:
                q = build_search_query(item["title"])
                unit = self.engine.unit_value(q, await self._comps(q), item["title"])
                lines.append((item["qty"], unit[0] if unit else None))
            return self.engine.valuate_lot(lines, l.price + premium + l.shipping_cost)
        query = build_search_query(l.title)
        needs = title_missing_parts(l.title, self.cfg.fix_costs)  # "No Power Cord" in the title => cost of a cord
        if needs:
            l.extra["needs"] = needs
        comps = await self._comps(query)
        if comps.source == "sold":  # dated sales: the raw material for sell-through (kept for the liquidity check)
            l.extra["_sold"] = [(c.price, c.sold_date) for c in comps.items if comp_matches(l.title, c.title)]
        l.extra["_query"] = query
        if "prior_range" not in l.extra:  # what we ALREADY knew, taken before this lookup adds its own evidence
            l.extra["prior_range"] = knowledge.compute_range(self.conn, knowledge.item_key(query))
            self._observe(l.title, query, comps)
        return self.engine.valuate(query, comps, price, l.shipping_cost,
                                   l.title, buyer_premium_pct=l.extra_cost_pct,
                                   fix_cost=sum(needs.values()))

    async def _process(self, listing: Listing) -> None:
        base = dict(platform=listing.platform, listing_id=listing.listing_id,
                    title=listing.title, price=listing.price,
                    shipping_cost=listing.shipping_cost, listing_type=listing.listing_type,
                    end_time=listing.end_time, location=listing.location, url=listing.url,
                    is_locked=is_locked(listing.title), is_untested=is_untested(listing.title))
        try:
            val = await self._value(listing)
        except SoldDataUnavailable as e:
            if not self._warned_no_sold:
                log.error("no sold-comps source available (%s); valuations are paused. "
                          "Get Marketplace Insights access, set ALLOW_ACTIVE_COMPS=1, or use --use-mock-comps "
                          "to test the pipeline.", e)
                self._warned_no_sold = True
            return  # not recorded: retried once comps are available
        except httpx.HTTPError as e:
            log.warning("comp lookup failed for %r: %s", listing.title, e)
            return
        if val is None:
            db.record(self.conn, **base, status="no_comps")
            return
        if (self.cfg.liquidity_enabled and val.comp_source != "melt" and not listing.lot_items
                and val.meets(self.cfg)):
            await self._apply_liquidity(listing, val)
            if listing.extra.get("illiquid"):
                db.record(self.conn, **base, status="illiquid", **dict(
                    estimated_market_value=val.estimated_value, profit_margin=val.margin_pct, net_profit_usd=val.net_profit,
                    comp_source=val.comp_source, detail_notes=listing.extra["liquidity"]["text"],
                    liquidity_days=listing.extra["liquidity"]["days"], liquidity_grade=listing.extra["liquidity"]["grade"]))
                return
        pr = getattr(self, "propertyroom", None)
        if (pr and listing.platform == "propertyroom" and not listing.lot_items and val.meets(self.cfg)
                and val.comp_source != "melt"):
            val = await self._apply_details(listing, val)
        retail_price = None
        if self.retail and not listing.lot_items and val.meets(self.cfg) and val.comp_source != "melt":
            val, retail_price = await self._cap_at_retail(listing, val)
        prior = listing.extra.pop("prior_range", None)
        if val.comp_source != "melt" and not listing.lot_items:
            rng = knowledge.compute_range(self.conn, knowledge.item_key(build_search_query(listing.title)))
            listing.extra["grade"] = knowledge.grade(val.comp_source, rng)
            if rng is not None and rng.typical is not None:
                listing.extra["known_range"] = rng.text()
            warn = knowledge.cross_check(prior, val.estimated_value)
            if warn:
                listing.extra["cross_check"] = warn
                log.warning("cross-check [%s] %s: %s", listing.platform, listing.title[:60], warn)
        figures = dict(estimated_market_value=val.estimated_value, profit_margin=val.margin_pct,
                       gross_margin_pct=val.gross_margin_pct, net_profit_usd=val.net_profit,
                       comp_source=val.comp_source, retail_price=retail_price,
                       unverified_telecom=bool((listing.extra.get("telecom") or {}).get("unverified_telecom_status")),
                       liquidity_days=(listing.extra.get("liquidity") or {}).get("days"),
                       liquidity_grade=(listing.extra.get("liquidity") or {}).get("grade"),
                       fix_cost=sum(listing.extra.get("needs", {}).values()) or None,
                       detail_notes="; ".join(x for x in ((listing.extra.get("details") or {}).get("summary") or
                                                          listing.extra.get("melt"), listing.extra.get("cross_check")) if x) or None)
        if val.estimated_value < self.cfg.min_market_value:
            # The floor is on what it resells for, never on the current bid: a $1 auction for a
            # $300 item stays in play, a $5 bid on a $25 item does not.
            db.record(self.conn, **base, status="low_value", **figures)
            return
        tier = alert_tier(listing, val, self.cfg)
        alert_listing, alert_val = listing, val
        buy_now = listing.extra.get("buy_now_price")
        if not tier and buy_now:
            # An auction with a Buy It Now above the bid: if the guaranteed Buy It Now price is itself a deal, say so now.
            bin_val = await self._value(listing, price=buy_now)
            if bin_val is not None and bin_val.estimated_value >= self.cfg.min_market_value:
                twin = dataclasses.replace(listing, price=buy_now, listing_type="buy_it_now",
                                           extra={**listing.extra, "auction_bid": listing.price})
                bin_tier = alert_tier(twin, bin_val, self.cfg)
                if bin_tier:
                    tier, alert_listing, alert_val = bin_tier, twin, bin_val
        status = "skipped"
        if tier:
            status = "alerted"
            left = time_left(alert_listing.end_time) if alert_listing.listing_type in AUCTION_TYPES or \
                alert_listing.extra.get("auction_bid") is not None else None
            log.info("%s%s [%s] %s | cost $%.2f est $%.2f net $%.2f (%.0f%%)%s %s", tier.upper(),
                     " [ACTIVE-ASKS, NOT SOLD DATA]" if alert_val.comp_source == "active" else "",
                     alert_listing.platform, alert_listing.title, alert_val.cost, alert_val.estimated_value,
                     alert_val.net_profit, alert_val.margin_pct, f" ENDS IN {left}" if left else "",
                     alert_listing.url)
            if not self.dry_run:
                try:
                    await send_alert(self.http, self.cfg.discord_webhook_url, alert_listing, alert_val,
                                     tier, self.cfg.discord_ping_here)
                except httpx.HTTPError as e:
                    log.error("discord alert failed: %s", e)
                    return  # retry next cycle
        elif val.meets(self.cfg) and listing.listing_type in AUCTION_TYPES:
            status = "watchlist"  # margin holds at the current bid; re-check as it nears its end
        db.record(self.conn, **base, status=status, **figures)

    async def _apply_liquidity(self, listing: Listing, val) -> None:
        """Estimate how long this would take to SELL, from dated sales + current competition (+ your own realized days).

        Runs only for items that already clear the profit bar. Marks the listing 'illiquid' only when the estimate is
        RELIABLE (real sales behind it) and slower than MAX_DAYS_TO_SELL; with no data it just reports a low-confidence prior.
        """
        query = listing.extra.pop("_query", None) or build_search_query(listing.title)
        sold = listing.extra.pop("_sold", [])
        key = knowledge.item_key(query)
        stats = liquidity.latest_stats(self.conn, key)
        if stats is None or time.time() - stats.observed_at > 86400:
            rows = None
            getter = getattr(self.ebay, "active", None)
            if getter is not None:
                try:
                    rows = await getter(query)
                except Exception as e:  # never let a liquidity lookup break a scan
                    log.warning("liquidity lookup failed for %r: %s", query, e)
            if rows is not None:
                rows = [r for r in rows if not r.get("title") or comp_matches(listing.title, r["title"])]
            stats = liquidity.build_stats(key, "scanner", sold, rows)
            if sold or rows:
                liquidity.save_stats(self.conn, stats)
        kind = catalog.style_of(listing.title, "fashion" if is_designer(listing.title) else "electronics")[1]
        est = liquidity.expected_days(stats, kind, val.estimated_value,
                                      liquidity.calibration(self.conn, key) or liquidity.calibration(self.conn))
        carry = liquidity.carry_cost(val.cost, est.days, self.cfg.carry_rate)
        bits = []
        if stats.sold_30d is not None and stats.active:
            bits.append(f"{stats.sold_30d} sold/30d vs {stats.active} active (STR {stats.str_reseller():.0%})")
        elif stats.sold_30d is not None:
            bits.append(f"{stats.sold_30d} sold in 30d")
        if stats.ask_premium():
            bits.append(f"asks run {stats.ask_premium():.0%} of sold prices")
        listing.extra["liquidity"] = {"grade": est.grade, "days": round(est.days, 1), "low": round(est.low, 1),
                                      "high": round(est.high, 1), "reliable": est.reliable, "carry": carry,
                                      "text": est.text() + (" | " + "; ".join(bits) if bits else "") + f" | carrying cost ~${carry:,.2f}"}
        if est.reliable and est.days > self.cfg.max_days_to_sell:
            listing.extra["illiquid"] = True
            log.info("illiquid [%s] %s: %s", listing.platform, listing.title[:60], est.text())

    async def _apply_details(self, listing: Listing, val):
        """Re-value a profitable-looking PropertyRoom listing using its own page.

        The page's test results beat title guesses (powers on => full value, activation-locked or dead =>
        the 20% tier, untested => 60%), and missing parts (power adapter, drive, remote...) are added to
        the cost. If the listing page can't be read the original valuation stands.
        """
        a = await self.propertyroom.assess(listing, self.cfg.fix_costs)
        if a is None or not a.found:
            return val
        a.missing = {**listing.extra.get("needs", {}), **a.missing}
        text = f"{listing.title} {a.text}"
        report = cached_report(self.conn, extract_imei(text)) if extract_imei(text) else None  # free: manual or earlier paid result
        if report is None and (imei := imei_should_check(self.cfg, text, val.estimated_value)):  # paid: IMEI printed + value >= $100
            report = await self.imei.check_imei_status(imei)
        query = build_search_query(listing.title)
        revalued = self.engine.valuate(
            query, await self._comps(query), listing.price, listing.shipping_cost, listing.title,
            buyer_premium_pct=listing.extra_cost_pct,
            condition_factor=self.engine.detail_condition_factor(listing.title, a, report), fix_cost=a.fix_cost)
        if (lux := self.engine.luxury_assessment(listing.title, a.text)) is not None:
            listing.extra["luxury"] = {"brand": lux.brand, "tier": lux.tier, "factor": lux.factor, "note": lux.note()}
        factor, ts = self.engine.telecom_factor(listing.title, a.text, report)
        if ts.mobile:
            listing.extra["telecom"] = {"status": ts.status, "unverified_telecom_status": ts.unverified_telecom_status,
                                        "factor": factor, "reasons": ts.reasons}
            if report is not None and report.verdict != "unknown":
                listing.extra["telecom"]["imei"] = {"imei": report.imei, "verdict": report.verdict, "fmi": report.fmi_status,
                                                    "blacklist": report.blacklist_status, "sim_lock": report.sim_lock}
                a.notes.append(f"IMEI {report.imei} checked: {report.verdict.upper()} (Find My {report.fmi_status}, "
                               f"blacklist {report.blacklist_status}), valued at {factor:.0%} of a clean comp")
            elif ts.unverified_telecom_status:
                a.notes.append(f"GOVERNMENT TELECOM DISCLAIMER: IMEI/lock/blacklist unverified, valued at {factor:.0%} of a clean comp")
        listing.extra["needs"] = dict(a.missing)
        listing.extra["details"] = {"summary": a.summary()}
        return revalued or val

    async def _cap_at_retail(self, listing: Listing, val):
        """Cap a profitable-looking estimate at a fraction of the item's NEW retail price (Best Buy).

        Only called for listings that already clear the profit thresholds, to keep Best Buy calls low.
        If the capped estimate no longer clears them, the listing is not alerted.
        """
        try:
            retail = await self.retail.lookup(listing.title)
        except httpx.HTTPError as e:
            log.warning("retail lookup failed for %r: %s", listing.title, e)
            return val, None
        if retail is None:
            return val, None
        listing.extra["retail"] = {"price": retail.sale_price, "url": retail.url, "name": retail.name}
        try:
            knowledge.add_observation(self.conn, build_search_query(listing.title), "new_retail", "bestbuy",
                                      typical=retail.sale_price, url=retail.url, notes=retail.name, replace_within_hours=24)
        except sqlite3.Error as e:
            log.warning("could not save retail observation: %s", e)
        ceiling = retail.sale_price * self.cfg.retail_cap_fraction
        if val.estimated_value <= ceiling * self.engine.condition_factor(listing.title):
            return val, retail.sale_price
        query = build_search_query(listing.title)
        capped = self.engine.valuate(query, await self._comps(query), listing.price, listing.shipping_cost,
                                     listing.title, buyer_premium_pct=listing.extra_cost_pct,
                                     value_ceiling=ceiling)
        if capped is None:
            return val, retail.sale_price
        capped.detail = (capped.detail + "; " if capped.detail else "") + (
            f"capped at {self.cfg.retail_cap_fraction:.0%} of new retail ${retail.sale_price:,.2f}")
        return capped, retail.sale_price

    # ---------- supply ----------
    async def _run_sources(self, fetchers: dict) -> list[list[Listing]]:
        """Run source fetchers in parallel; one failing source never blocks the others."""
        names = list(fetchers)
        results = await asyncio.gather(*(f() for f in fetchers.values()), return_exceptions=True)
        batches = []
        for name, res in zip(names, results):
            if isinstance(res, Exception):
                log.warning("source=%s event=fetch_failed reason=%r", name, res)
            else:
                batches.append(res)
        return batches

    def _discovery_fetchers(self) -> dict:
        cfg = self.cfg

        async def reverb() -> list[Listing]:
            out: list[Listing] = []
            for cat in cfg.reverb_categories:
                out += await self.reverb.new_listings(cat)
                await asyncio.sleep(1)
            return out

        async def etsy() -> list[Listing]:
            out: list[Listing] = []
            for q in cfg.etsy_queries:
                out += await self.etsy.new_listings(q)
                await asyncio.sleep(1)
            return out

        async def ebay() -> list[Listing]:
            if not self.ebay_search:
                return []  # no eBay keys yet: fail soft
            out: list[Listing] = []
            for q in cfg.ebay_queries:
                out += await self.ebay_search.new_listings(q, False, max_price=cfg.max_price)
                out += await self.ebay_search.new_listings(q, True, max_price=cfg.max_price)
                await asyncio.sleep(0.5)
            return out

        async def pr_discovery() -> list[Listing]:
            return self._lane(await self.propertyroom.fetch())

        async def sgw_jewelry() -> list[Listing]:
            for_cat = await self.sgw.fetch_recent_listings(cfg.sgw_jewelry_category_ids, cfg.sgw_recent_pages)
            return [l for l in for_cat if estimate_melt(l.title)]

        async def sgw_fashion() -> list[Listing]:
            for_cat = await self.sgw.fetch_recent_listings(cfg.sgw_fashion_category_ids, cfg.sgw_recent_pages)
            return [l for l in for_cat if is_designer(l.title)]

        all_ = {
            "reverb": reverb,
            "ebay": ebay,
            "etsy": etsy,
            "shopgoodwill": lambda: self.sgw.fetch_recent_listings(
                cfg.sgw_category_ids, cfg.sgw_recent_pages),
            "govdeals": self.govdeals.fetch,
            "gsa": self.gsa.fetch,
            "propertyroom": pr_discovery,
            "sgw_jewelry": sgw_jewelry,
            "sgw_fashion": sgw_fashion,
            "bstock": lambda: _async(load_lots(cfg.bstock_manifest_dir, cfg.bstock_premium_pct)),
        }
        return {k: v for k, v in all_.items() if k in cfg.sources}

    def _ending_fetchers(self) -> dict:
        cfg = self.cfg
        cutoff = timedelta(minutes=cfg.ending_window_minutes)

        def soon(listings: list[Listing]) -> list[Listing]:
            now = datetime.now(timezone.utc)
            return [l for l in listings if l.end_time and now < l.end_time <= now + cutoff]

        async def govdeals() -> list[Listing]:
            return soon(await self.govdeals.fetch())

        async def gsa() -> list[Listing]:  # date-only end times: lots closing today, from a fresher feed
            return await self.gsa.fetch_closing_day()

        async def ebay() -> list[Listing]:  # auctions ending soonest; 3-minute cache keeps Browse calls low
            if not self.ebay_search:
                return []
            out: list[Listing] = []
            for q in cfg.ebay_queries:
                out += await self.ebay_search.new_listings(q, True, max_price=cfg.max_price, ttl=180)
            return soon(out)

        async def propertyroom() -> list[Listing]:  # page 1 is ending-soonest; 2-minute cache
            return soon(self._lane(await self.propertyroom.fetch(pages=1, ttl=120)))

        async def sgw_jewelry() -> list[Listing]:
            ls = await self.sgw.fetch_ending_auctions(cfg.sgw_jewelry_category_ids, cfg.ending_window_minutes,
                                                      cfg.sgw_ending_max_pages)
            return [l for l in ls if estimate_melt(l.title)]

        async def sgw_fashion() -> list[Listing]:
            ls = await self.sgw.fetch_ending_auctions(cfg.sgw_fashion_category_ids, cfg.ending_window_minutes,
                                                      cfg.sgw_ending_max_pages)
            return [l for l in ls if is_designer(l.title)]

        all_ = {
            "shopgoodwill": lambda: self.sgw.fetch_ending_auctions(
                cfg.sgw_category_ids, cfg.ending_window_minutes, cfg.sgw_ending_max_pages),
            "govdeals": govdeals,
            "gsa": gsa,
            "propertyroom": propertyroom,
            "sgw_jewelry": sgw_jewelry,
            "sgw_fashion": sgw_fashion,
            "ebay": ebay,
            "bstock": lambda: _async(soon(load_lots(cfg.bstock_manifest_dir, cfg.bstock_premium_pct))),
        }
        return {k: v for k, v in all_.items() if k in cfg.sources}

    def _admit(self, batches: list[list[Listing]], revisit_watchlist: bool) -> list[Listing]:
        """Drop seen listings, then interleave sources and cap the comp budget.

        Nothing is dropped for a LOW price (auctions open at $1). Only a price above MAX_PRICE_USD
        is recorded as out_of_range: a bid can only rise, and a fixed price can't fall.
        """
        per_source = []
        for batch in batches:
            fresh = []
            for l in batch:
                status = db.get_status(self.conn, l.platform, l.listing_id)
                if status is not None and not (revisit_watchlist and status == "watchlist"):
                    if not (status == "watchlist" and self._bid_escalated(l)):
                        continue
                if self.melt_only and not self._melt(l):
                    continue  # no comp source: only precious-metal listings can be valued
                if not l.lot_items and l.price > self.cfg.max_price:
                    if status is None:
                        db.record(self.conn, platform=l.platform, listing_id=l.listing_id,
                                  title=l.title, price=l.price, shipping_cost=l.shipping_cost,
                                  status="out_of_range", listing_type=l.listing_type,
                                  end_time=l.end_time, location=l.location)
                    continue
                fresh.append(l)
            per_source.append(fresh)
        mixed = [l for grp in itertools.zip_longest(*per_source) for l in grp if l]
        return mixed[: self.cfg.max_comps_per_cycle]

    def _bid_escalated(self, l: Listing) -> bool:
        """A watchlisted auction whose bid jumped since we valued it is re-valued now, not at the end."""
        if l.listing_type not in AUCTION_TYPES:
            return False
        row = db.get_row(self.conn, l.platform, l.listing_id)
        if row is None:
            return False
        why = bid_escalation(row["price"], l.price, row["estimated_market_value"],
                             parse_thresholds(self.cfg.watchlist_bid_thresholds),
                             self.cfg.watchlist_value_fraction, self.cfg.watchlist_approach)
        if why:
            log.info("watchlist re-eval [%s] %s: %s", l.platform, l.title[:60], why)
        return bool(why)

    async def discovery_cycle(self) -> None:
        if self.cfg.melt_enabled:
            await self.spot.refresh()
        batch = self._admit(await self._run_sources(self._discovery_fetchers()), False)
        log.info("discovery: comping %d new listings", len(batch))
        await asyncio.gather(*(self._process(l) for l in batch))

    async def ending_cycle(self) -> None:
        if self.cfg.melt_enabled:
            await self.spot.refresh()
        batch = self._admit(await self._run_sources(self._ending_fetchers()), True)
        if batch:
            log.info("ending soon: re-valuing %d auctions at current bid", len(batch))
        await asyncio.gather(*(self._process(l) for l in batch))

    async def _loop(self, cycle, interval: float, name: str) -> None:
        while True:
            start = time.time()
            try:
                await cycle()
            except Exception:
                log.exception("%s cycle failed", name)
            await asyncio.sleep(max(1, interval - (time.time() - start)))

    async def run(self, once: bool) -> None:
        if once:
            await self.discovery_cycle()
            await self.ending_cycle()
            return
        await asyncio.gather(
            self._loop(self.discovery_cycle, self.cfg.poll_interval, "discovery"),
            self._loop(self.ending_cycle, self.cfg.ending_poll_seconds, "ending"),
        )


async def _async(value):
    return value


async def amain() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--once", action="store_true", help="run one discovery + one ending cycle and exit")
    ap.add_argument("--dry-run", action="store_true", help="log deals, don't post to Discord")
    ap.add_argument("--watchlist", action="store_true",
                    help="show tracked auctions soonest-ending first, with time left (reads the database, no scan)")
    ap.add_argument("--web", action="store_true", help="build a local web page (site/index.html) of the top auctions to bid on")
    ap.add_argument("--web-refresh", action="store_true",
                    help="with --web: re-read each listing first for live bids / end times / test results (~2 minutes)")
    ap.add_argument("--web-limit", type=int, default=100, help="how many items on the page (default 100)")
    ap.add_argument("--track", action="append", metavar="URL", help="add an auction to your tracked list (repeatable)")
    ap.add_argument("--track-max", type=float, help="your own max bid for the --track URL(s)")
    ap.add_argument("--track-kind", metavar="KIND", help="track every running priced auction of an item type, e.g. tablet")
    ap.add_argument("--untrack", metavar="URL_OR_ID", help="remove an auction from your tracked list")
    ap.add_argument("--tracked", action="store_true", help="print your tracked list")
    ap.add_argument("--web-all", action="store_true", help="put EVERY profitable item on the page, sortable as a whole (refresh takes longer)")
    ap.add_argument("--sync-txauction", action="store_true", help="re-read the TXAuction auction pages listed in txauction.py (about 4 requests)")
    ap.add_argument("--import-facebook", metavar="FILE", help="load a Facebook Marketplace export (JSON or CSV) and print the confidence-gate result for every listing; no network, no paid quota")
    ap.add_argument("--sync-ibid", action="store_true", help="read Illinois iBid (state surplus + unclaimed-property lots): about 10 listing pages plus detail pages for jewelry/tech lots, 2 s apart; no account, no spend")
    ap.add_argument("--import-lots", metavar="FILE", help="load an auction-sources export (CWS/Treasury, HiBid, Illinois iBid, CTBids, discovery leads) with per-sale buyer's premium; no network")
    ap.add_argument("--serve", action="store_true", help="serve the page at http://127.0.0.1:PORT (this machine only)")
    ap.add_argument("--port", type=int, default=8765)
    ap.add_argument("--lan", action="store_true", help="with --serve: let other computers on your local network open the page (no password)")
    ap.add_argument("--catalog", action="store_true",
                    help="crawl all designer clothing/bag/shoe listings into the catalog, then print the report")
    ap.add_argument("--catalog-report", action="store_true", help="print the catalog report from the database (no crawl)")
    ap.add_argument("--catalog-opps", action="store_true",
                    help="cross-reference every running auction against its known price range (profit at current bid)")
    ap.add_argument("--domain", help="limit the catalog report / opportunities to one domain (fashion, electronics, computers, "
                                      "cameras, instruments, collectibles, tools, gsa)")
    ap.add_argument("--catalog-domains", default=None,
                    help="comma-separated domains to crawl (default: all of them)")
    ap.add_argument("--catalog-pages", type=int, default=None, help="limit pages per category when crawling (default: all)")
    ap.add_argument("--focus", metavar="KINDS", help="comma-separated item types to concentrate on, e.g. tablet,laptop: limits the page and --fill-ranges to them")
    ap.add_argument("--measure-climb", action="store_true", help="download ShopGoodwill CLOSED auctions for the priced models and measure how high they really close vs eBay sold value")
    ap.add_argument("--closed-categories", action="store_true", help="with --measure-climb: also read whole categories (electronics, cameras...) to measure how many auctions get no bids")
    ap.add_argument("--closed-days", type=int, default=30, help="with --measure-climb: how many days of closed auctions to read")
    ap.add_argument("--fill-ranges", type=int, metavar="N", help="look up real eBay SOLD prices (SoldComps) for the top N unpriced groups closing soon; each costs one request")
    ap.add_argument("--rebuild-ranges", action="store_true", help="recompute every sold price range from the saved sales (no API calls)")
    ap.add_argument("--sync-bidprowl", action="store_true", help="pull lots near your ZIP from BidProwl into the local watch list (about 12 of your 1,000 monthly calls)")
    ap.add_argument("--bidprowl-radius", type=int, default=None, help="miles from SHIP_TO_ZIP (default BIDPROWL_RADIUS_MILES = 25)")
    ap.add_argument("--watch-luxury", action="store_true", help="list every running tier-1 luxury / tier-2 streetwear piece in the catalog with its authenticity evidence (Chicago pickup rule applied)")
    ap.add_argument("--scoreboard", action="store_true", help="where the money is: auctions closing soon by item type and brand, how many are priced / sold-backed / pass the page rules")
    ap.add_argument("--fill-model", action="append", metavar="IDENT=QUERY", help="look up one named model: store the sold range under IDENT using the eBay search text QUERY (repeatable)")
    ap.add_argument("--fill-baseline", action="append", metavar="BRAND=QUERY", help="brand-level SOLD baseline for bags from real sales, e.g. \"michael kors=michael kors handbag\" (repeatable)")
    ap.add_argument("--dry-run-ranges", action="store_true", help="with --fill-ranges: list what would be looked up, spend nothing")
    ap.add_argument("--check-imei", metavar="IMEI", help="test one IMEI (Luhn, cache, SickW, multiplier) without scanning")
    ap.add_argument("--set-imei", metavar="IMEI", help="record a hand-checked IMEI result (use with --imei-verdict)")
    ap.add_argument("--imei-verdict", choices=["clean", "bad"], help="clean = blacklist AND iCloud lock checked OK; bad = either failed")
    ap.add_argument("--lookup", metavar="TEXT", help="show the known price range and evidence for an item")
    ap.add_argument("--add-price", metavar="ITEM", help="record a price observation (use with --kind --source --low/--high)")
    ap.add_argument("--kind", choices=knowledge.KINDS, default="ask")
    ap.add_argument("--source", default="manual")
    ap.add_argument("--low", type=float)
    ap.add_argument("--high", type=float)
    ap.add_argument("--typical", type=float)
    ap.add_argument("--n", type=int, default=1, help="how many sales/listings the observation is based on")
    ap.add_argument("--condition")
    ap.add_argument("--url")
    ap.add_argument("--notes")
    ap.add_argument("--log-trade", metavar="ITEM", help="log a real purchase/sale (use with --buy and optionally --sell)")
    ap.add_argument("--listed-at", metavar="YYYY-MM-DD", help="when the item went up for sale (makes days-to-sell measurable)")
    ap.add_argument("--sold-at", metavar="YYYY-MM-DD")
    ap.add_argument("--list-price", type=float, help="asking price it was listed at")
    ap.add_argument("--close-trade", type=int, metavar="ID", help="record the sale of open trade ID (use with --sell [--fees --sold-at])")
    ap.add_argument("--inventory", action="store_true", help="open trades with days held/listed and the markdown to take")
    ap.add_argument("--liquidity", metavar="ITEM", help="market stats and expected days-to-sell for an item")
    ap.add_argument("--buy", type=float)
    ap.add_argument("--sell", type=float)
    ap.add_argument("--fees", type=float, default=0.0, help="selling fees + shipping for a logged sale")
    ap.add_argument("--channel")
    ap.add_argument("--use-mock-comps", action="store_true",
                    help="fabricated eBay comps to test the pipeline without eBay keys (forces --dry-run)")
    args = ap.parse_args()
    integrity.require_healthy()  # refuse to run on an emptied/broken source file
    cfg = Config()
    if args.watchlist:
        print(report.format_watchlist(report.watchlist_rows(db.connect(cfg.db_path))))
        return
    if args.track or args.untrack or args.tracked or args.track_kind:
        import tracking
        conn = db.connect(cfg.db_path)
        for url in args.track or []:
            print("tracking", tracking.track(conn, url, args.track_max))
        if args.track_kind:
            print(f"tracking {tracking.track_kind(conn, args.track_kind)} {args.track_kind} auctions")
        if args.untrack:
            print("removed", tracking.untrack(conn, args.untrack))
        rows, missing = tracking.tracked_rows(conn)
        for r in rows:
            print(f"{r['platform_label']:<13} bid ${r['bid']:>7,.2f}  max ${r['max_bid']:>7,.2f}  mine {r['my_max']}  {r['title'][:60]}")
        for e in missing:
            print(f"not shown (ended, unpriced or not in the catalog): {e['platform']} {e['listing_id']}")
        return
    if args.focus:
        focus = {k.strip() for k in args.focus.split(",") if k.strip()}
        webpage.FOCUS_KINDS = focus
    else:
        focus = set(webpage.FOCUS_KINDS) or None  # the standing focus from FOCUS_KINDS in .env
    if args.measure_climb:
        import closed_auctions, fill_ranges
        conn = db.connect(cfg.db_path)
        idents = sorted({r[0] for r in conn.execute("SELECT DISTINCT item_key FROM price_observations WHERE kind='sold' AND source='soldcomps-fill'")})
        base = [i for i in idents if not any(t in i for t in ("gb", "cellular", "wifi"))]  # one search per model, not per variant
        print(f"reading closed ShopGoodwill auctions for {len(base)} models, {args.closed_days} days back...")
        async with httpx.AsyncClient(timeout=40, headers={"User-Agent": "arbitrage-scanner/1.0 (personal use)"}) as cl_http:
            n = await closed_auctions.fetch(cl_http, conn, base, args.closed_days)
        print(f"{n} closed auctions saved")
        if args.closed_categories:
            async with httpx.AsyncClient(timeout=40, headers={"User-Agent": "arbitrage-scanner/1.0 (personal use)"}) as cl_http:
                nc = await closed_auctions.fetch_categories(cl_http, conn, list(cfg.sgw_closed_categories), args.closed_days)
            print(f"{nc} closed auctions saved from whole categories")
            print(f"{'CATEGORY':<10}{'AUCTIONS':>9}{'NO BIDS':>9}{'MED BIDS':>9}{'MED FINAL':>10}{'FINAL/START':>12}")
            for r in closed_auctions.category_stats(conn):
                print(f"{r['category']:<10}{r['n']:>9}{r['unsold']*100:>8.0f}%{r['median_bids']:>9.0f}{r['median_final']:>9.0f}${r['median_vs_start']:>10.1f}x")
        print(f"{'KIND':<12}{'SALES':>6}{'MEDIAN':>8}{'25-75%':>14}{'UNSOLD':>8}")
        for r in closed_auctions.measure(conn):
            print(f"{r['kind']:<12}{r['n']:>6}{r['median']*100:>7.0f}%{r['p25']*100:>8.0f}-{r['p75']*100:.0f}%{r['unsold_share']*100:>7.0f}%")
        return
    if args.rebuild_ranges:
        import fill_ranges
        conn = db.connect(cfg.db_path)
        for r in fill_ranges.rebuild(conn):
            extra = f"  ${r['low']:,.0f}-${r['high']:,.0f} typ ${r['typical']:,.0f} from {r['n']} sales" if r["status"] == "stored" else f"  ({r['n']} sales saved)"
            print(f"{r['status']:<26} {r['name'][:40]:<40}{extra}")
        return
    if args.sync_bidprowl:
        import bidprowl_sync
        from clients.bidprowl import BidProwlClient
        if not cfg.bidprowl_api_key or not cfg.ship_to_zip:
            raise SystemExit("Set BIDPROWL_API_KEY and SHIP_TO_ZIP in .env first.")
        conn = db.connect(cfg.db_path)
        async with httpx.AsyncClient(timeout=40) as bp_http:
            bp = BidProwlClient(bp_http, cfg.bidprowl_api_key)
            res = await bidprowl_sync.sync(conn, bp, cfg.ship_to_zip, args.bidprowl_radius or cfg.bidprowl_radius_miles,
                                           far_radius=cfg.bidprowl_far_radius_miles)
            use = await bp.usage()
        print(f"synced {res['lots']} near lots + {res['far']} far lots (to {cfg.bidprowl_far_radius_miles} mi) with {res['calls']} calls; {use.get('used')} of {use.get('limit')} calls used this month")
        for t, n in __import__("collections").Counter(w["topic"] for w in bidprowl_sync.watch_rows(conn)).most_common():
            print(f"  watch list: {t:<14}{n:>4}")
        return
    if args.watch_luxury:
        import luxury_watch
        conn = db.connect(cfg.db_path)
        print(luxury_watch.format_watch(luxury_watch.watch(conn)))
        return
    if args.scoreboard:
        import scoreboard
        conn = db.connect(cfg.db_path)
        rows = scoreboard.build(conn)
        print(scoreboard.format_table(scoreboard.by_kind(rows), "BY ITEM TYPE", lambda r: r["kind"]))
        print()
        print(scoreboard.format_table(rows[:25], "BY TYPE + BRAND (top 25 by profit, then size)", lambda r: f"{r['kind']}/{r['brand']}"[:21]))
        return
    if args.fill_baseline:
        import fill_ranges
        from clients.ebay import SoldCompsClient, CompCache
        conn = db.connect(cfg.db_path)
        async with httpx.AsyncClient(timeout=60) as fb_http:
            client = (SoldCompsClient(fb_http, cfg.soldcomps_api_key, CompCache(conn, "soldcomps", cfg.soldcomps_cache_ttl_hours * 3600))
                      if cfg.soldcomps_api_key else None)
            for item in args.fill_baseline:
                brand, _, query = item.partition("=")
                r = await fill_ranges.fill_baseline(conn, client, brand.strip(), (query or brand).strip(), dry_run=args.dry_run_ranges)
                extra = f"  ${r['low']:,.0f}-${r['high']:,.0f} typ ${r['typical']:,.0f} from {r['n']} sales" if r["status"] == "stored" else ""
                print(f"{r['status']:<30} {r['name'][:34]:<34} <- {r['query'][:30]}{extra}")
        return
    if args.fill_model:
        import fill_ranges
        from clients.ebay import SoldCompsClient, CompCache
        pairs = []
        for item in args.fill_model:
            ident, _, query = item.partition("=")
            pairs.append((ident.strip().lower(), (query or ident).strip()))
        conn = db.connect(cfg.db_path)
        async with httpx.AsyncClient(timeout=60) as fm_http:
            client = (SoldCompsClient(fm_http, cfg.soldcomps_api_key, CompCache(conn, "soldcomps", cfg.soldcomps_cache_ttl_hours * 3600))
                      if cfg.soldcomps_api_key else None)
            for r in await fill_ranges.fill_named(conn, client, pairs, dry_run=args.dry_run_ranges):
                extra = f"  ${r['low']:,.0f}-${r['high']:,.0f} typ ${r['typical']:,.0f} from {r['n']} sales" if r["status"] == "stored" else ""
                print(f"{r['status']:<34} {r['name'][:30]:<30} <- {r['query'][:34]}{extra}")
        return
    if args.fill_ranges:
        import fill_ranges
        from clients.ebay import SoldCompsClient, CompCache, SoldDataUnavailable
        conn = db.connect(cfg.db_path)
        async with httpx.AsyncClient(timeout=60) as fr_http:
            client = (SoldCompsClient(fr_http, cfg.soldcomps_api_key, CompCache(conn, "soldcomps", cfg.soldcomps_cache_ttl_hours * 3600))
                      if cfg.soldcomps_api_key else None)
            if client is None and not args.dry_run_ranges:
                raise SystemExit("Set SOLDCOMPS_API_KEY in .env first (free key at sold-comps.com), or add --dry-run-ranges to preview.")
            try:
                res = await fill_ranges.fill(conn, client, args.fill_ranges, cfg.page_max_days if hasattr(cfg, "page_max_days") else 2.0,
                                             dry_run=args.dry_run_ranges, kinds=focus)
            except SoldDataUnavailable as e:
                raise SystemExit(f"stopped: {e}")
        for r in res:
            extra = f"  ${r['low']:,.0f}-${r['high']:,.0f} typ ${r['typical']:,.0f} from {r['n']} sales" if r["status"] == "stored" else ""
            print(f"{r['status']:<34} {r['listings']:>3} listings  {r['name'][:34]:<34}{extra}")
        print(f"{len(res)} groups" + (" (dry run, nothing spent)" if args.dry_run_ranges else ""))
        return
    if args.sync_ibid:
        import lot_sources
        from clients import illinois_ibid
        conn = db.connect(cfg.db_path)
        async def _ibid():
            async with httpx.AsyncClient(timeout=40) as http:
                return await illinois_ibid.sync(conn, http)
        print(await _ibid())
        pay = lot_sources.page_payload(conn)
        print(f"{len(pay['verified'])} verified, {len(pay['unverified'])} unverified lot(s) across the intake sources")
        for r in pay["unverified"][:12]:
            if r["source"] == "illinois_ibid":
                print(f"  [{r['failed']}] {r['title'][:60]}")
        return
    if args.import_lots:
        import lot_sources
        conn = db.connect(cfg.db_path)
        print(lot_sources.import_file(conn, args.import_lots))
        pay = lot_sources.page_payload(conn)
        for r in pay["verified"] + pay["unverified"]:
            print(f"[{r['status'].upper():10}] {r['platform_label']} {r['listing_id']}  {r['title'][:50]}  " + (f"net ${r['evidence']['net']:,.0f}" if r["status"] == "verified" else f"FAILED: {r['failed']}"))
        print(f"{len(pay['leads'])} discovery lead(s)")
        return
    if args.import_facebook:
        import fb_market
        conn = db.connect(cfg.db_path)
        print(fb_market.import_file(conn, args.import_facebook))
        for r in fb_market.assess_all(conn):
            ev = r["evidence"]
            print(f"[{r['status'].upper():10}] ${r['buy']:,.0f}  {r['title'][:60]}  " + (f"net ${ev['net']:,.0f} via {ev['best_channel']}, max buy ${ev['max_buy']:,.0f}" if r["status"] == "verified" else f"FAILED: {r['failed']} ({r['failures'][0]['why']})"))
        return
    if args.sync_txauction:
        import txauction
        async with httpx.AsyncClient(timeout=40) as tx_http:
            snap = await txauction.sync(tx_http)
        for a in snap["auctions"]:
            print(f"{a['auction']['id']} {a['auction']['title']}: {len(a['lots'])} of {a['auction']['visible_lots']} lots, ends {a['auction']['end']}")
        print(f"{len(txauction.snapshot_watch_rows(snap))} lots on the watch list")
        return
    if args.web or args.serve:
        site = Path(cfg.web_dir)
        if args.web:
            conn = db.connect(cfg.db_path)
            limit, per_style = (5000, 5000) if args.web_all else (args.web_limit, 5)
            watch_rows = []
            if cfg.bidprowl_api_key and cfg.ship_to_zip:
                import bidprowl_sync, time as _time
                from clients.bidprowl import BidProwlClient
                if _time.time() - bidprowl_sync.last_sync(conn) > 6 * 3600:  # at most every 6 hours: each sync costs ~25 calls
                    try:
                        async with httpx.AsyncClient(timeout=40) as bp_http:
                            await bidprowl_sync.sync(conn, BidProwlClient(bp_http, cfg.bidprowl_api_key), cfg.ship_to_zip,
                                                     cfg.bidprowl_radius_miles, far_radius=cfg.bidprowl_far_radius_miles)
                    except httpx.HTTPError as e:
                        print(f"BidProwl sync skipped: {e}")
                watch_rows = bidprowl_sync.watch_rows(conn)
            import scrap, txauction
            tx_snap = txauction.load_snapshot()
            if txauction.is_stale(tx_snap):
                async with httpx.AsyncClient(timeout=40) as tx_http:
                    tx_snap = await txauction.sync(tx_http)
            scrap_spot = None
            try:
                from clients.metals import SpotPriceClient
                async with httpx.AsyncClient(timeout=20) as sp_http:
                    sp = await SpotPriceClient(sp_http).refresh()
                if sp.get("gold") and sp.get("silver"):
                    scrap_spot = {"gold": sp["gold"], "silver": sp["silver"]}
            except Exception as e:  # no spot feed: the page falls back to the stored prices
                print(f"spot prices unavailable: {e!r}")
            watch_rows = watch_rows + txauction.snapshot_watch_rows(tx_snap, gold_spot=(scrap_spot or {}).get("gold") or 4165.0)
            scrap_auctions = scrap.payload(scrap.apply_snapshot(scrap.load(), tx_snap), scrap_spot)
            rows = webpage.collect(conn, limit if args.web_all else (int(limit * 1.6) if args.web_refresh else limit),
                                   per_style=per_style, mobile_candidates=args.web_refresh)
            if args.web_refresh:
                print(f"re-reading {len(rows)} listings (about {len(rows) * 1.3 / 60:.0f} minutes)...")
                async with httpx.AsyncClient(timeout=30, headers={"User-Agent": "arbitrage-scanner/0.3"}) as http:
                    rows = webpage.rerank(await webpage.refresh(rows, cfg, http), limit, per_style=per_style)
            import tracking
            mine, _ = tracking.tracked_rows(conn)
            if args.web_refresh and mine:
                async with httpx.AsyncClient(timeout=30, headers={"User-Agent": "arbitrage-scanner/0.3"}) as http:
                    live = {(m["platform"], m["listing_id"]): m for m in mine}
                    for m in await webpage.refresh(mine, cfg, http, keep_ending=True):
                        live[(m["platform"], m["listing_id"])].update(m)
                    mine = list(live.values())
            rows = tracking.merge(rows, mine + [d for d in webpage.designer_rows(conn)
                                                if (d['platform'], d['listing_id']) not in {(m['platform'], m['listing_id']) for m in mine}])
            tx_main = webpage.tx_rows(tx_snap, scrap_auctions, gold_spot=(scrap_spot or {}).get("gold") or 4165.0)
            for i, t_row in enumerate(tx_main, max((r.get("rank", 0) for r in rows), default=0) + 1):
                t_row["rank"] = i
            rows = rows + tx_main
            tx_ids = {t_row["listing_id"] for t_row in tx_main}
            watch_rows = [w for w in watch_rows if w.get("source") != "TXAuction"]  # Texas lots live in the main table; the lower list is everything else
            import batch_evaluate  # spec-matched pricer over every open lot (stored sold sales only: no network, no quota)
            picks, _n, _c = batch_evaluate.run()
            import page_gate
            gate_payload = page_gate.page_payload(conn, rows)  # the verified gate: only verified rows stay in the main table
            rows = gate_payload["main"]
            import fb_market
            fb_payload = fb_market.page_payload(conn)  # Marketplace rows the owner imported; empty until a file is loaded
            import lot_sources
            ext_payload = lot_sources.page_payload(conn)
            import luxury_goods
            lux_payload = luxury_goods.page_payload(conn, snap=tx_snap)
            path = webpage.write_site(site / "index.html", rows, args.web_refresh, watch=watch_rows, scrap=scrap_auctions, picks=picks,
                                      fb=fb_payload if fb_payload["verified"] or fb_payload["unverified"] else None,
                                      lux=lux_payload if lux_payload["verified"] or lux_payload["unverified"] else None, gate=gate_payload,
                                      ext=ext_payload)
            print(f"wrote {path} with {len(rows)} items" + ("" if args.web_refresh else " (bids as of the last crawl)"))
        if args.serve:
            webpage.serve(site, args.port, lan=args.lan)
        return
    if (args.lookup or args.add_price or args.log_trade or args.catalog_report or args.catalog
            or args.catalog_opps
            or args.inventory or args.close_trade or args.liquidity or args.check_imei or args.set_imei):
        conn = db.connect(cfg.db_path)
        _day = lambda s: int(datetime.strptime(s, "%Y-%m-%d").replace(tzinfo=timezone.utc).timestamp()) if s else None
        if args.close_trade:
            if args.sell is None:
                raise SystemExit("--close-trade needs --sell")
            knowledge.close_trade(conn, args.close_trade, args.sell, sold_at=_day(args.sold_at), sell_fees=args.fees,
                                  channel=args.channel)
            args.inventory = True
        if args.set_imei:
            if not args.imei_verdict:
                raise SystemExit("--set-imei needs --imei-verdict clean|bad")
            try:
                r = set_manual(conn, args.set_imei, args.imei_verdict)
            except ValueError as e:
                raise SystemExit(str(e))
            print(f"saved: IMEI {r.imei} marked {args.imei_verdict.upper()} (manual). Re-run --web --web-refresh to apply it.")
        if args.check_imei:
            async with httpx.AsyncClient(timeout=45) as imei_http:
                print(await check_imei_cli(cfg, imei_http, conn, args.check_imei))
        if args.liquidity:
            print(report.format_liquidity(conn, args.liquidity))
        if args.add_price:
            knowledge.add_observation(conn, args.add_price, args.kind, args.source, low=args.low, high=args.high,
                                      typical=args.typical, n=args.n, condition=args.condition, url=args.url, notes=args.notes)
            args.lookup = args.add_price
        if args.log_trade:
            if args.buy is None:
                raise SystemExit("--log-trade needs --buy")
            knowledge.add_trade(conn, args.log_trade, args.buy, args.sell, channel=args.channel, sell_fees=args.fees,
                                notes=args.notes, listed_at=_day(args.listed_at), list_price=args.list_price,
                                sold_at=_day(args.sold_at))
            args.lookup = args.log_trade
        if args.catalog:
            async with httpx.AsyncClient(timeout=30, headers={"User-Agent": "arbitrage-scanner/0.3"}) as http:
                print("crawled:", await catalog.crawl(conn, http, cfg, args.catalog_pages,
                                                 args.catalog_domains.split(",") if args.catalog_domains else None))
        if args.lookup:
            print(report.format_lookup(conn, args.lookup))
        if args.inventory:
            print(report.format_inventory(conn))
        if args.catalog_opps:
            print(catalog.format_opportunities(catalog.opportunities(conn, args.domain)))
        elif args.catalog or args.catalog_report:
            print(catalog.format_report(conn, domain=args.domain))
        return
    melt_only = False
    if args.use_mock_comps:
        log.warning("MOCK COMPS: eBay data is fabricated, valuations are NOT real, alerts are disabled")
    elif not cfg.soldcomps_api_key:
        if not (cfg.ebay_client_id and cfg.ebay_client_secret):
            if not cfg.melt_enabled:
                log.error("No comp source: set SOLDCOMPS_API_KEY (sold data) and/or EBAY_CLIENT_ID + "
                          "EBAY_CLIENT_SECRET in .env, or pass --use-mock-comps to test the pipeline.")
                raise SystemExit(2)
            log.warning("No comp source configured (SOLDCOMPS_API_KEY / EBAY keys): running in MELT-ONLY mode. "
                        "Only solid gold/silver/platinum listings with a stated weight can be valued; "
                        "everything else is skipped until you add a comp source.")
            melt_only = True
        else:
            log.warning("SOLDCOMPS_API_KEY is not set. Using eBay Marketplace Insights if your account has it, "
                        "otherwise falling back to DISCOUNTED ACTIVE ASKING PRICES (ALLOW_ACTIVE_COMPS forced on). "
                        "Active asks are NOT sold data and skew valuations high; alerts are tagged ACTIVE-ASKS.")
            cfg = dataclasses.replace(cfg, allow_active_comps=True)
    async with httpx.AsyncClient(timeout=20, headers={"User-Agent": "arbitrage-scanner/0.3"}) as http:
        scanner = Scanner(cfg, http, args.dry_run, args.use_mock_comps, melt_only)
        try:
            await scanner.run(args.once)
        finally:
            await scanner.aclose()


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)  # GSA redirects to a pre-signed S3 URL
    asyncio.run(amain())
