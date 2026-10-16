import asyncio
import dataclasses
from datetime import datetime, timedelta, timezone

import httpx
import pytest

import db
from clients.metals import SpotPriceClient
from config import Config
from engine.melt import estimate_melt
from engine.title_cleaner import build_search_query, comp_matches, extract_models, is_designer
from engine.valuation import ValuationEngine
from models import Listing

run = asyncio.run
SPOT = {"gold": 4000.0, "silver": 50.0, "platinum": 1600.0}


def value(title):
    e = estimate_melt(title)
    return None if e is None else round(e.melt_value(SPOT[e.metal]), 2)


# ---------------------------------------------------------------- melt estimates
def test_solid_gold_scrap_math():
    # 10 g of 14K = 5.85 g pure = 0.18808 ozt x $4000
    e = estimate_melt("10 grams 14k Scrap Gold")
    assert e.metal == "gold" and e.purity == 0.585 and e.confidence == "high"
    assert e.melt_value(4000) == pytest.approx(10 * 0.95 * 0.585 / 31.1035 * 4000)


@pytest.mark.parametrize("title", [
    "14K Goldfilled Jewelry 4pc 22.6g", "6.4g 925 Sterling Rhodium Plate/14K Rose Gold Plate Floral",
    "18K Gold Overlay 45.00ctw Turquoise Bracelet", "Vintage 14K Gold Watch 25g", "Gold Tone Necklace 20g",
    "10K Gold Vermeil Ring 5g", "14k gold bracelet",            # no weight
    "Sterling Silver Necklace",                                  # no weight
    "Stainless Steel 925 Ring 6g", "Designer Inspired 14k Chain 10g", "Costume 925 earrings 4g",
])
def test_no_estimate_when_not_solid_metal_or_weight_unknown(title):
    assert estimate_melt(title) is None


def test_stones_and_beads_cut_the_metal_share():
    plain = estimate_melt("14K Yellow Gold Chain 8.2g")
    stones = estimate_melt("14K Yellow Gold Ring with Garnet 8.2g")
    beaded = estimate_melt("10k White Gold Glass Beaded Necklace TW 21.9g")
    assert plain.metal_fraction == 0.95 and stones.metal_fraction == 0.4 and beaded.metal_fraction == 0.25
    assert stones.confidence == beaded.confidence == "medium"
    assert stones.melt_value(4000) < plain.melt_value(4000) * 0.5


def test_units_dwt_ozt_and_bullion():
    assert estimate_melt("2.5 dwt 10K Gold Ring").grams == pytest.approx(2.5 * 1.55517)
    bar = estimate_melt("1 ozt 24k Gold Bar")
    assert bar.bullion and bar.grams == pytest.approx(31.1035) and bar.metal_fraction == 1.0
    rnd = estimate_melt("1 oz .999 Fine Silver Round")
    assert rnd.bullion and rnd.melt_value(50) == pytest.approx(49.95, abs=0.1)
    assert estimate_melt("Platinum Wedding Band 6.1 grams").metal == "platinum"
    assert estimate_melt("Sterling Candlestick Pair 400g").metal_fraction <= 0.5  # weighted bases


def test_gold_and_silver_both_named_counts_only_silver():
    e = estimate_melt("12.59g Sterling Silver Snake Chain with 10K Gold Accent")
    assert e.metal == "silver" and e.metal_fraction <= 0.5


# ---------------------------------------------------------------- spot prices
def _spot_client(handler, **kw):
    return SpotPriceClient(httpx.AsyncClient(transport=httpx.MockTransport(handler)), **kw)


def test_spot_prices_fetch_and_cache():
    calls = []

    def handler(req):
        calls.append(req.url.path)
        return httpx.Response(200, json={"price": {"XAU": 4134.5, "XAG": 59.3, "XPT": 1645.0}[req.url.path.split("/")[-1]]})

    async def go():
        c = _spot_client(handler)
        a = await c.refresh()
        b = await c.refresh()  # inside ttl
        return a, b

    a, b = run(go())
    assert a == {"gold": 4134.5, "silver": 59.3, "platinum": 1645.0} and b == a and len(calls) == 3


def test_spot_failure_keeps_last_price_but_never_goes_stale_forever():
    state = {"fail": False}

    def handler(req):
        return httpx.Response(500) if state["fail"] else httpx.Response(200, json={"price": 100.0})

    async def go():
        c = _spot_client(handler, ttl=0, max_age=3600)
        await c.refresh()
        state["fail"] = True
        kept = await c.refresh()
        c._ok_at -= 7200  # two hours old: too stale to value anything off
        return kept, c.prices

    kept, stale = run(go())
    assert kept["gold"] == 100.0 and stale == {}


# ---------------------------------------------------------------- valuation + scanner
def test_valuate_melt_has_no_marketplace_fees():
    cfg = Config()
    v = ValuationEngine(cfg).valuate_melt(realizable=800.0, price=300.0, shipping=0.0)
    assert v.comp_source == "melt" and v.cost == 300.0
    assert v.net_profit == pytest.approx(800 - 300 - cfg.melt_ship_usd)  # no 13.25% + $0.30
    assert v.meets(cfg)


class SpotStub:
    def __init__(self, prices):
        self.prices = prices

    async def refresh(self):
        return self.prices


def _scanner(tmp_path, monkeypatch, prices=SPOT, **cfg_kw):
    from main import Scanner
    from clients.ebay import MockEbayClient
    monkeypatch.setenv("DB_PATH", str(tmp_path / "t.db"))
    sc = Scanner.__new__(Scanner)
    sc.cfg, sc.conn = Config(min_market_value=0, **cfg_kw), db.connect(str(tmp_path / "t.db"))
    sc.engine, sc.ebay, sc._comp_cache, sc.dry_run = ValuationEngine(sc.cfg), MockEbayClient(), {}, True
    sc._ebay_gate, sc._warned_no_sold, sc.retail = asyncio.Semaphore(2), False, None
    sc.spot = SpotStub(prices)
    return sc


def _l(title, price, kind="buy_it_now", **kw):
    return Listing("shopgoodwill", "J1", title, price, 0.0, "u", None, "jewelry", listing_type=kind,
                   end_time=datetime.now(timezone.utc) + timedelta(hours=6), **kw)


def _row(sc):
    return sc.conn.execute("SELECT status, comp_source, net_profit_usd, detail_notes FROM listings").fetchone()


def test_scrap_gold_priced_well_under_melt_alerts_without_any_comps(tmp_path, monkeypatch):
    sc = _scanner(tmp_path, monkeypatch)
    sc.ebay = None  # no comp provider at all: melt must not need one
    run(sc._process(_l("14K Yellow Gold Scrap Jewelry 20 grams", 500.0)))   # melt ~ $1,500, realizable ~ $1,280
    r = _row(sc)
    assert r["status"] == "alerted" and r["comp_source"] == "melt" and "melt $" in r["detail_notes"]


def test_scrap_priced_at_melt_does_not_alert(tmp_path, monkeypatch):
    sc = _scanner(tmp_path, monkeypatch)
    run(sc._process(_l("14K Yellow Gold Scrap Jewelry 20 grams", 1500.0)))
    assert _row(sc)["status"] == "skipped"


def test_under_melt_auction_is_watchlisted_until_it_nears_its_end(tmp_path, monkeypatch):
    sc = _scanner(tmp_path, monkeypatch)
    run(sc._process(_l("10K Yellow Gold Scrap Jewelry 20.84g", 500.0, "auction")))
    assert _row(sc)["status"] == "watchlist"


def test_no_spot_price_means_no_melt_valuation(tmp_path, monkeypatch):
    sc = _scanner(tmp_path, monkeypatch, prices={})
    assert sc._melt(_l("14K Gold Scrap 20 grams", 500.0)) is None


def test_melt_only_mode_ignores_everything_that_is_not_precious_metal(tmp_path, monkeypatch):
    sc = _scanner(tmp_path, monkeypatch)
    sc.melt_only = True
    batch = [_l("14K Gold Scrap 20 grams", 500.0), Listing("shopgoodwill", "X", "Sony Walkman WM-F10", 20, 0, "u", None, "c")]
    assert [l.title for l in sc._admit([batch], False)] == ["14K Gold Scrap 20 grams"]


def test_lanes_keep_only_what_they_can_value(tmp_path, monkeypatch):
    sc = _scanner(tmp_path, monkeypatch)

    def L(title, cat):
        return Listing("propertyroom", title, title, 10, 0, "u", None, cat)

    kept = sc._lane([L("14K Gold Chain 8g", "jewelry"), L("Designer Inspired 55ctw Necklace", "jewelry"),
                     L("1 oz .999 silver round", "coins"), L("Coach Willis Purse", "fashion"),
                     L("Plain tee shirt", "fashion"), L("Dell Latitude 7420", "computers")])
    assert [l.title for l in kept] == ["14K Gold Chain 8g", "1 oz .999 silver round", "Coach Willis Purse", "Dell Latitude 7420"]


# ---------------------------------------------------------------- buy-it-now alternative on auctions
def test_buy_it_now_alternative_alerts_when_it_is_itself_a_deal(tmp_path, monkeypatch):
    sc = _scanner(tmp_path, monkeypatch)
    # auction far from its end (so no WATCHLIST alert), but the guaranteed BIN is still way under melt
    listing = _l("14K Yellow Gold Scrap Jewelry 20 grams", 400.0, "auction")
    listing.end_time = datetime.now(timezone.utc) + timedelta(days=3)
    listing.extra["buy_now_price"] = 700.0
    run(sc._process(listing))
    assert _row(sc)["status"] == "alerted"


def test_buy_it_now_alternative_ignored_when_it_is_not_a_deal(tmp_path, monkeypatch):
    sc = _scanner(tmp_path, monkeypatch)
    listing = _l("14K Yellow Gold Scrap Jewelry 20 grams", 400.0, "auction")
    listing.end_time = datetime.now(timezone.utc) + timedelta(days=3)
    listing.extra["buy_now_price"] = 1700.0  # above melt: not a deal
    run(sc._process(listing))
    assert _row(sc)["status"] == "watchlist"


# ---------------------------------------------------------------- designer clothing / bags
@pytest.mark.parametrize("title,expected", [
    ("Louis Vuitton Neverfull MM Tote Bag Monogram", True), ("Coach Willis Leather Crossbody Purse", True),
    ("Michael Kors Jet Set Handbag", True), ("Allen Edmonds Park Avenue Shoes 10.5", True),
    ("Brooks Brothers Wool Suit 42R", True), ("Hugo Boss suit 40R", True), ("Gucci GG Marmont belt 90", True),
    ("Plain leather purse", False), ("Sony Walkman WM-F10", False), ("Vera Wang dress", False),
])
def test_is_designer(title, expected):
    assert is_designer(title) is expected


def test_suit_sizes_are_not_mistaken_for_model_numbers():
    assert extract_models("Brooks Brothers Wool Suit 42R") == []
    assert extract_models("Hugo Boss suit 40R") == []
    assert comp_matches("Brooks Brothers Wool Suit 42R", "Brooks Brothers Mens Wool Suit Navy 40R")


def test_fashion_comps_must_name_the_same_brand():
    t = "Louis Vuitton Neverfull MM Tote Bag Monogram"
    assert comp_matches(t, "Louis Vuitton Neverfull MM Monogram Tote")
    assert not comp_matches(t, "Coach Monogram Tote Bag")
    assert build_search_query(t).startswith("louis vuitton")
