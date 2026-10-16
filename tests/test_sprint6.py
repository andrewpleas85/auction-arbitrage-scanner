import asyncio
import dataclasses
import json
import time
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import httpx
import pytest

import db
import integrity
from alerting.discord import CRITICAL, build_embed
from clients import gsa
from clients.ebay import (Comp, CompCache, Comps, CompsRouter, SoldCompsClient, SoldDataUnavailable,
                          build_comps_provider)
from config import Config
from engine.title_cleaner import condition
from engine.valuation import Valuation
from engine.watchlist import bid_escalation
from models import Listing

EASTERN = ZoneInfo("America/New_York")


def run(coro):
    return asyncio.run(coro)


# ---------------------------------------------------------------- 1. file integrity guard
def test_verify_flags_empty_missing_and_broken_files(tmp_path):
    (tmp_path / "good.py").write_text("def f():\n    return 'a real module with content'\n")
    (tmp_path / "empty.py").write_text("")
    (tmp_path / "broken.py").write_text("def f(:\n    pass  # not valid python at all, long enough\n")
    problems = integrity.verify_critical_files(tmp_path, ("good.py", "empty.py", "broken.py", "gone.py"))
    text = " | ".join(problems)
    assert "empty.py: empty" in text and "broken.py: syntax error" in text and "gone.py: missing" in text
    assert not any(p.startswith("good.py") for p in problems)


def test_real_repo_is_healthy():
    assert integrity.verify_critical_files() == []


def test_safe_write_refuses_to_empty_a_file(tmp_path):
    target = tmp_path / "main.py"
    original = "def main():\n    return 'important working code that must survive'\n"
    target.write_text(original)
    for bad in ("", "   \n", "x=1"):
        with pytest.raises(integrity.IntegrityError):
            integrity.safe_write(target, bad)
    assert target.read_text() == original


def test_safe_write_refuses_invalid_python_and_keeps_original(tmp_path):
    target = tmp_path / "mod.py"
    target.write_text("def ok():\n    return 'this original module is valid and long enough'\n")
    with pytest.raises(integrity.IntegrityError):
        integrity.safe_write(target, "def broken(:\n    return 'something long enough to pass the size check'\n")
    assert "def ok" in target.read_text()


def test_safe_write_writes_atomically_and_leaves_no_temp_files(tmp_path):
    target = tmp_path / "new_module.py"
    integrity.safe_write(target, "def hello():\n    return 'hello world from a brand new module'\n")
    assert "hello" in target.read_text()
    assert [p.name for p in tmp_path.iterdir()] == ["new_module.py"]


def test_startup_refuses_to_run_on_emptied_critical_file(monkeypatch):
    monkeypatch.setattr(integrity, "verify_critical_files", lambda *a, **k: ["main.py: empty or near-empty (0 bytes)"])
    with pytest.raises(SystemExit, match="main.py"):
        integrity.require_healthy()


# ---------------------------------------------------------------- 3. condition-flag boundaries
@pytest.mark.parametrize("title,expected", [
    ("NOT locked iPhone 12", "working"),
    ("iPhone 12 not iCloud locked", "working"),
    ("Untested charger included with tested phone", "working"),
    ("Phone charger untested, phone tested working", "working"),
    ("Samsung S21 tested - not for parts", "working"),
    ("Phone no passcode, no lock", "working"),
    ("No iCloud lock, no MDM", "working"),
    ("Laptop no power adapter included", "working"),
    ("Pixel MDM-200 router", "working"),
    ("Locked storage case for camera", "working"),
    ("iPhone 11 iCloud removed", "working"),
    ("Factory Unlocked iPhone 13", "working"),
    ("Not untested, works great", "working"),
    ("Not refurbished Dell", "working"),
    ("iPad Mini MDM locked", "locked"),
    ("iPhone 12 Locked with charger", "locked"),
    ("iPhone 13 iCloud Locked", "locked"),
    ("Dell Latitude For Parts", "locked"),
    ("Galaxy Bad ESN", "locked"),
    ("Phone with passcode", "locked"),
    ("Refurbished iPhone iCloud Locked", "locked"),
    ("Camera body untested", "untested"),
    ("Camera Untested with charger", "untested"),
    ("Laptop sold as is", "untested"),
    ("Drone no power", "untested"),
    ("Phone not working", "untested"),
    ("MacBook Refurbished", "refurbished"),
    ("Mint condition lens", "refurbished"),
])
def test_condition_flag_boundaries(title, expected):
    assert condition(title) == expected


# ---------------------------------------------------------------- 2. SoldComps + router + degradation
def _client(handler):
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


SOLD_PAYLOAD = {"keyword": "x", "items": [
    {"title": "Fluke 87V", "soldPrice": "300.00", "soldCurrency": "USD", "endedAt": "2026-10-01", "bestOfferAccepted": False},
    {"title": "Fluke 87V", "soldPrice": "999.00", "soldCurrency": "USD", "endedAt": "2026-10-02", "bestOfferAccepted": True},
    {"title": "Fluke 87V", "soldPrice": "250.00", "soldCurrency": "GBP", "endedAt": "2026-10-03"},
    {"title": "Fluke 87V", "soldPrice": "280.50", "soldCurrency": "USD", "endedAt": "2026-10-05"},
    {"title": "Fluke 87V", "soldPrice": "oops", "soldCurrency": "USD"},
]}


def test_soldcomps_parse_drops_best_offer_foreign_and_bad_rows():
    comps = SoldCompsClient.parse(SOLD_PAYLOAD)
    assert [c.price for c in comps] == [280.5, 300.0]  # newest first; BOA, GBP and junk removed


def test_soldcomps_request_shape_and_sold_source():
    seen = {}

    async def go():
        def handler(req):
            seen["auth"], seen["url"], seen["params"] = req.headers["authorization"], req.url.path, dict(req.url.params)
            return httpx.Response(200, json=SOLD_PAYLOAD)
        async with _client(handler) as h:
            return await SoldCompsClient(h, "sc_testkey").comps("fluke 87v")

    comps = run(go())
    assert comps.source == "sold" and len(comps.items) == 2
    assert seen["auth"] == "Bearer sc_testkey" and seen["url"] == "/v1/scrape"
    assert seen["params"]["keyword"] == "fluke 87v" and seen["params"]["sold"] == "true"


def test_soldcomps_persistent_cache_saves_quota(tmp_path):
    conn = db.connect(str(tmp_path / "t.db"))
    calls = []

    async def go():
        def handler(req):
            calls.append(1)
            return httpx.Response(200, json=SOLD_PAYLOAD)
        async with _client(handler) as h:
            a = SoldCompsClient(h, "sc_k", CompCache(conn, "soldcomps", 3600))
            await a.comps("fluke 87v")
            b = SoldCompsClient(h, "sc_k", CompCache(conn, "soldcomps", 3600))  # "restart"
            return await b.comps("fluke 87v")

    again = run(go())
    assert len(calls) == 1 and [c.price for c in again.items] == [280.5, 300.0]


def test_comp_cache_expires(tmp_path):
    conn = db.connect(str(tmp_path / "t.db"))
    cache = CompCache(conn, "p", ttl_seconds=60)
    cache.put("q", [Comp("t", 10.0, datetime.now(timezone.utc))])
    assert cache.get("q")[0].price == 10.0
    conn.execute("UPDATE comp_cache SET fetched_at=?", (int(time.time()) - 120,))
    assert cache.get("q") is None


@pytest.mark.parametrize("status,body,blocked", [
    (401, {}, "rejected"), (429, {"code": "quota_exceeded"}, "quota"),
])
def test_soldcomps_auth_and_quota_errors_block_provider(status, body, blocked):
    async def go():
        async with _client(lambda r: httpx.Response(status, json=body)) as h:
            c = SoldCompsClient(h, "sc_k")
            with pytest.raises(SoldDataUnavailable):
                await c.comps("x")
            with pytest.raises(SoldDataUnavailable):  # now short-circuits without another request
                await c.comps("y")
            return c.blocked_reason

    assert blocked in run(go())


class _Stub:
    def __init__(self, result=None, exc=None):
        self.result, self.exc, self.blocked_reason, self.calls = result, exc, None, 0

    async def comps(self, q, limit=50):
        self.calls += 1
        if self.exc:
            raise self.exc
        return self.result


def test_router_prefers_soldcomps_then_falls_back_to_ebay():
    sold, active = Comps("sold", [Comp("a", 1.0)]), Comps("active", [Comp("b", 2.0)])
    assert run(CompsRouter(_Stub(sold), _Stub(active)).comps("q")).source == "sold"
    ebay = _Stub(active)
    assert run(CompsRouter(_Stub(exc=SoldDataUnavailable("quota")), ebay).comps("q")).source == "active"
    assert ebay.calls == 1
    with pytest.raises(SoldDataUnavailable):
        run(CompsRouter(None, None).comps("q"))


def test_build_comps_provider_follows_configured_keys(tmp_path):
    conn = db.connect(str(tmp_path / "t.db"))
    base = Config()
    with_both = dataclasses.replace(base, soldcomps_api_key="sc_k", ebay_client_id="a", ebay_client_secret="b")
    only_sold = dataclasses.replace(base, soldcomps_api_key="sc_k", ebay_client_id="", ebay_client_secret="")
    neither = dataclasses.replace(base, soldcomps_api_key="", ebay_client_id="", ebay_client_secret="")
    r1, r2, r3 = (build_comps_provider(c, None, conn) for c in (with_both, only_sold, neither))
    assert r1.soldcomps and r1.ebay and r2.soldcomps and not r2.ebay and not r3.soldcomps and not r3.ebay


def _val(source):
    return Valuation(400, 6, source, 100, 150, 150.0)


def test_alert_embed_tags_active_ask_valuations():
    l = Listing("reverb", "1", "Fluke 87V", 100, 0, "https://u", None, "c")
    active = build_embed(l, _val("active"), CRITICAL)
    sold = build_embed(l, _val("sold"), CRITICAL)
    assert active["title"].startswith("⚠ ACTIVE-ASKS") and any("NOT SOLD DATA" in f["name"] for f in active["fields"])
    assert not sold["title"].startswith("⚠") and not any("NOT SOLD" in f["name"] for f in sold["fields"])


# ---------------------------------------------------------------- 4. GSA closing-day polling
def _gsa_raw(sale, end_date, bid):
    return {"saleNo": sale, "lotNo": "001", "auctionStatus": "Active", "itemName": "Cisco Switch Lot",
            "highBidAmount": str(bid), "aucEndDt": end_date, "propertyCity": "Erie", "propertyState": "PA",
            "propertyZip": "16504", "itemDescURL": "https://x"}


def test_gsa_tags_lots_closing_today_and_refreshes_them_faster():
    today = datetime.now(EASTERN).date()
    state = {"bid": 100, "requests": 0}

    async def go():
        def handler(req):
            state["requests"] += 1
            return httpx.Response(200, json={"Results": [
                _gsa_raw("A", today.isoformat(), state["bid"]),
                _gsa_raw("B", (today + timedelta(days=3)).isoformat(), 50)]})
        async with _client(handler) as h:
            c = gsa.GSAClient(h, "k", min_interval=1800, closing_interval=0)
            first = await c.fetch()
            state["bid"] = 175  # someone outbids
            cached = await c.fetch()                 # discovery cache: still the old bid
            closing = await c.fetch_closing_day()    # closing-day poll bypasses the cache
            return first, cached, closing

    first, cached, closing = run(go())
    assert {l.listing_id: l.extra["closes_today"] for l in first} == {"A-001": True, "B-001": False}
    assert [l.price for l in cached if l.listing_id == "A-001"] == [100.0]
    assert [(l.listing_id, l.price) for l in closing] == [("A-001", 175.0)]
    assert state["requests"] == 2


def test_gsa_closing_day_respects_interval():
    today = datetime.now(EASTERN).date().isoformat()
    calls = []

    async def go():
        def handler(req):
            calls.append(1)
            return httpx.Response(200, json={"Results": [_gsa_raw("A", today, 100)]})
        async with _client(handler) as h:
            c = gsa.GSAClient(h, "k", min_interval=1800, closing_interval=600)
            await c.fetch_closing_day()
            await c.fetch_closing_day()
    run(go())
    assert len(calls) == 1  # the second poll is inside the closing-day interval: no extra request


def test_gsa_closing_interval_never_exceeds_discovery_interval():
    c = gsa.GSAClient(None, "k", min_interval=300, closing_interval=900)
    assert c._closing_interval == 300


# ---------------------------------------------------------------- 5. watchlist bid escalation
@pytest.mark.parametrize("prev,cur,est,expect", [
    (1.0, 12.0, 400, True),      # crossed $10
    (11.0, 24.0, 400, False),    # between thresholds, far from 50% of value
    (24.0, 26.0, 400, True),     # crossed $25
    (30.0, 55.0, 400, True),     # crossed $50
    (60.0, 150.0, 400, False),   # past every threshold, still under 90% of the 50% line (180)
    (150.0, 185.0, 400, True),   # nearing 50% of est. value
    (185.0, 300.0, 400, False),  # line already crossed last time: don't re-trigger
    (10.0, 10.0, 400, False),    # no change
    (20.0, 12.0, 400, False),    # bids never fall
    (None, 50.0, 400, False),    # nothing stored to compare with
])
def test_bid_escalation(prev, cur, est, expect):
    assert bool(bid_escalation(prev, cur, est)) is expect


def test_bid_escalation_reports_reason_and_custom_thresholds():
    assert "crossed $100" in bid_escalation(90, 120, 10_000, thresholds=(100,))
    assert "nearing 50%" in bid_escalation(100, 190, 400)


def _scanner(tmp_path, monkeypatch, **kw):
    from main import Scanner
    from clients.ebay import MockEbayClient
    from engine.valuation import ValuationEngine
    monkeypatch.setenv("DB_PATH", str(tmp_path / "t.db"))
    sc = Scanner.__new__(Scanner)
    sc.cfg, sc.conn = Config(**kw), db.connect(str(tmp_path / "t.db"))
    sc.engine, sc.ebay, sc._comp_cache, sc.dry_run = ValuationEngine(sc.cfg), MockEbayClient(), {}, True
    sc.retail = None
    sc._ebay_gate, sc._warned_no_sold = asyncio.Semaphore(2), False
    return sc


def _auction(price, i="a", kind="auction"):
    return Listing("propertyroom", i, "Fluke 87V multimeter", price, 0, "u", None, "c", listing_type=kind,
                   end_time=datetime.now(timezone.utc) + timedelta(hours=6))


def test_watchlisted_auction_is_revalued_when_bid_crosses_threshold(tmp_path, monkeypatch):
    sc = _scanner(tmp_path, monkeypatch, min_market_value=0)
    run(sc._process(_auction(1.0)))
    assert db.get_status(sc.conn, "propertyroom", "a") == "watchlist"
    assert sc._admit([[_auction(1.0)]], False) == []                 # unchanged bid: left alone
    assert sc._admit([[_auction(6.0)]], False) == []                 # under $10: left alone
    assert len(sc._admit([[_auction(12.0)]], False)) == 1            # crossed $10: re-valued in discovery
    run(sc._process(_auction(12.0)))
    assert db.get_row(sc.conn, "propertyroom", "a")["price"] == 12.0  # new baseline stored
    assert sc._admit([[_auction(14.0)]], False) == []                # next trigger is $25


def test_escalation_ends_watchlisting_once_the_margin_is_gone(tmp_path, monkeypatch):
    sc = _scanner(tmp_path, monkeypatch, min_market_value=0)
    run(sc._process(_auction(1.0)))
    est = db.get_row(sc.conn, "propertyroom", "a")["estimated_market_value"]
    run(sc._process(_auction(est * 2)))  # bid now far above resale value: no profit
    assert db.get_status(sc.conn, "propertyroom", "a") == "skipped"
    assert sc._admit([[_auction(est * 3)]], False) == []


def test_escalation_ignores_buy_it_now_listings(tmp_path, monkeypatch):
    sc = _scanner(tmp_path, monkeypatch, min_market_value=0)
    db.record(sc.conn, platform="propertyroom", listing_id="b", title="x", price=1, shipping_cost=0,
              status="watchlist", estimated_market_value=400)
    assert sc._admit([[_auction(60.0, "b", "buy_it_now")]], False) == []


def test_comp_source_is_recorded(tmp_path, monkeypatch):
    sc = _scanner(tmp_path, monkeypatch, min_market_value=0)
    run(sc._process(_auction(1.0)))
    assert sc.conn.execute("SELECT comp_source FROM listings").fetchone()[0] == "mock"
