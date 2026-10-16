import asyncio
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest

import db
from alerting.discord import WATCHLIST, alert_tier
from clients import gsa
from clients.ebay import Comp, Comps, MockEbayClient
from config import Config
from engine.title_cleaner import condition
from engine.valuation import ValuationEngine, Valuation
from models import Listing

EASTERN = ZoneInfo("America/New_York")


def _scanner(tmp_path, monkeypatch, **cfg_kw):
    from main import Scanner
    monkeypatch.setenv("DB_PATH", str(tmp_path / "t.db"))
    sc = Scanner.__new__(Scanner)
    sc.cfg = Config(**cfg_kw)
    sc.conn = db.connect(str(tmp_path / "t.db"))
    sc.engine = ValuationEngine(sc.cfg)
    sc.retail = None
    sc.ebay, sc._comp_cache, sc.dry_run = MockEbayClient(), {}, True
    sc._ebay_gate, sc._warned_no_sold = asyncio.Semaphore(2), False
    return sc


def _l(i, price, kind="auction", title="Fluke 87V multimeter", **kw):
    return Listing("propertyroom", i, title, price, 0, "u", None, "c", listing_type=kind, **kw)


def test_condition_tiers_and_factors():
    eng = ValuationEngine(Config())
    assert condition("Fluke 87V") == "working" and eng.condition_factor("Fluke 87V") == 1.0
    assert eng.condition_factor("Apple MacBook Refurbished") == pytest.approx(1.10)
    assert eng.condition_factor("Sony camera Untested") == pytest.approx(0.6)
    assert eng.condition_factor("iPad Spares or repair") == pytest.approx(0.6)
    for locked in ("iPhone iCloud Locked", "Galaxy Bad ESN", "Laptop For Parts", "Pixel MDM"):
        assert eng.condition_factor(locked) == pytest.approx(0.2)  # 80% haircut
    assert condition("Refurbished iPhone iCloud Locked") == "locked"  # worst signal wins


def test_locked_item_valued_at_20_percent():
    cfg = Config()
    comps = Comps("sold", [Comp("Apple iPhone 12 64GB", 300.0, datetime.now(timezone.utc)) for _ in range(6)])
    v = ValuationEngine(cfg).valuate("iphone 12", comps, 50, 0, "Apple iPhone 12 64GB iCloud Locked",
                                     buyer_premium_pct=15)
    assert v.estimated_value == pytest.approx(60.0)
    assert v.cost == pytest.approx(57.5)  # 50 + 15% premium


def test_net_profit_formula_matches_spec():
    cfg = Config()
    comps = Comps("sold", [Comp("Fluke 87V multimeter", 400.0, datetime.now(timezone.utc)) for _ in range(6)])
    v = ValuationEngine(cfg).valuate("fluke 87v", comps, 100, 10, "Fluke 87V multimeter", buyer_premium_pct=15)
    cost = 100 + 15 + 10
    net = 400 - cost - cfg.outbound_shipping - (400 * 0.1325 + 0.30)
    assert v.net_profit == pytest.approx(net, abs=0.01)
    assert v.roi_pct == pytest.approx(net / cost * 100, abs=0.1)


def test_low_bid_auctions_are_never_dropped_for_price(tmp_path, monkeypatch):
    sc = _scanner(tmp_path, monkeypatch)
    batch = [_l("a", 1.0), _l("b", 1.0, "buy_it_now"), _l("c", sc.cfg.max_price + 1)]
    assert [l.listing_id for l in sc._admit([batch], False)] == ["a", "b"]
    assert dict(sc.conn.execute("SELECT listing_id, status FROM listings").fetchall()) == {"c": "out_of_range"}


def test_value_floor_applies_to_resale_value_not_bid(tmp_path, monkeypatch):
    sc = _scanner(tmp_path, monkeypatch, min_market_value=10_000)  # everything resells for less
    asyncio.run(sc._process(_l("a", 1.0)))
    row = sc.conn.execute("SELECT status, estimated_market_value FROM listings").fetchone()
    assert row["status"] == "low_value" and row["estimated_market_value"] > 0

    sc2 = _scanner(tmp_path / "x" if False else tmp_path, monkeypatch, min_market_value=0)
    sc2.conn.execute("DELETE FROM listings"); sc2.conn.commit()
    asyncio.run(sc2._process(_l("b", 1.0)))
    assert sc2.conn.execute("SELECT status FROM listings").fetchone()["status"] in ("watchlist", "skipped")


def test_one_dollar_auction_is_revisited_by_ending_loop(tmp_path, monkeypatch):
    sc = _scanner(tmp_path, monkeypatch, min_market_value=0)
    soon = datetime.now(timezone.utc) + timedelta(hours=5)
    asyncio.run(sc._process(_l("a", 1.0, end_time=soon)))
    assert sc.conn.execute("SELECT status FROM listings").fetchone()["status"] == "watchlist"
    assert sc._admit([[_l("a", 1.0)]], False) == []          # discovery: already seen
    assert len(sc._admit([[_l("a", 3.0)]], True)) == 1       # ending loop: revisited


def _val(cfg, profit=100.0):
    return Valuation(500, 6, "sold", 100, profit, 100.0)


def _gsa_listing(end_date):
    return Listing("gsa", "1", "t", 10, 0, "u", None, "gsa", listing_type="daily_auction",
                   end_time=datetime.now(timezone.utc), extra={"end_date": end_date})


def test_daily_auction_alerts_only_on_end_date_with_min_profit():
    cfg = Config(min_profit=50, min_margin_pct=35)
    today = datetime.now(EASTERN).date().isoformat()
    tomorrow = (datetime.now(EASTERN).date() + timedelta(days=1)).isoformat()
    assert alert_tier(_gsa_listing(today), _val(cfg), cfg) == WATCHLIST
    assert alert_tier(_gsa_listing(tomorrow), _val(cfg), cfg) is None
    assert alert_tier(_gsa_listing(today), _val(cfg, profit=10), cfg) is None


def test_gsa_end_date_recorded():
    end = (datetime.now(timezone.utc) + timedelta(days=2)).date().isoformat()
    raw = {"saleNo": "s", "lotNo": "1", "auctionStatus": "Active", "itemName": "Cisco Switch", "highBidAmount": "50",
           "aucEndDt": end}
    l = gsa.parse_item(raw)
    assert l.listing_type == "daily_auction" and l.extra["end_date"] == end


def test_mock_comps_are_deterministic_and_flagged():
    c1 = asyncio.run(MockEbayClient().comps("fluke 87v"))
    c2 = asyncio.run(MockEbayClient().comps("fluke 87v"))
    assert c1.source == "mock" and [c.price for c in c1.items] == [c.price for c in c2.items]
    assert len(c1.items) >= 5


def test_ebay_app_id_alias(monkeypatch):
    import importlib, config
    monkeypatch.setattr("dotenv.load_dotenv", lambda *a, **k: None)
    monkeypatch.delenv("EBAY_CLIENT_ID", raising=False)
    monkeypatch.setenv("EBAY_APP_ID", "abc")
    try:
        assert importlib.reload(config).Config().ebay_client_id == "abc"
    finally:
        monkeypatch.undo()
        importlib.reload(config)
