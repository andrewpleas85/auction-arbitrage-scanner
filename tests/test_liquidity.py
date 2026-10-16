import asyncio
import time
from datetime import datetime, timedelta, timezone

import httpx
import pytest

import db
import report
from clients.ebay import Comp, Comps, EbayClient, CompsRouter, SoldCompsClient
from config import Config
from engine import knowledge as kb
from engine import liquidity as lq
from engine.valuation import ValuationEngine
from models import Listing

NOW = datetime.now(timezone.utc)
run = asyncio.run


def S(**kw):
    return lq.MarketStats(item_key="x", source="t", **kw)


# ---------------------------------------------------------------- basics
@pytest.mark.parametrize("days,grade", [(3, "A"), (14, "A"), (15, "B"), (30, "B"), (45, "C"), (60, "C"), (61, "D"), (400, "D"), (None, "?")])
def test_grade(days, grade):
    assert lq.grade_for(days) == grade


@pytest.mark.parametrize("pos,f", [(None, 1.0), (0.7, 0.5), (0.95, 0.8), (1.05, 1.0), (1.2, 1.6), (1.6, 2.5)])
def test_price_position_factor(pos, f):
    assert lq.position_factor(pos) == f


def test_sell_through_definitions_differ():
    s = S(sold_30d=40, active=60)
    assert s.str_reseller() == pytest.approx(40 / 60) and s.str_retail() == pytest.approx(0.4)
    assert S(sold_30d=200, active=100).str_reseller() == 2.0       # reseller-style can exceed 100%
    assert S(sold_30d=5).str_reseller() is None and S(active=5).str_retail() is None
    assert S(median_sold=100, median_ask=135).ask_premium() == pytest.approx(1.35)


# ---------------------------------------------------------------- building stats
def test_build_stats_counts_windows_medians_and_stale():
    sold = [(100, NOW - timedelta(days=d)) for d in (1, 5, 20, 45, 80)] + [(300, None)]
    active = [{"price": 120, "watchers": 2, "created": NOW - timedelta(days=5)},
              {"price": 150, "watchers": 4, "created": NOW - timedelta(days=100)},
              {"price": 130, "watchers": None, "created": NOW - timedelta(days=70)}]
    s = lq.build_stats("k", "src", sold, active, NOW)
    assert (s.sold_30d, s.sold_90d, s.active, s.stale_active) == (3, 5, 3, 2)
    assert s.median_ask == 130 and s.avg_watchers == 3.0 and s.median_sold == 100


def test_build_stats_without_listing_age_or_active_data():
    s = lq.build_stats("k", "src", [(50, NOW)], [{"price": 60, "watchers": None, "created": None}], NOW)
    assert s.active == 1 and s.stale_active is None
    assert lq.build_stats("k", "src", [(50, NOW)], None, NOW).active is None


# ---------------------------------------------------------------- expected days
def test_no_market_data_falls_back_to_a_labelled_prior():
    e = lq.expected_days(None, "bag")
    assert e.basis == "prior" and not e.reliable and e.days == 54 and any("prior" in n for n in e.notes)
    assert lq.expected_days(S(), "nonsense-kind").days == lq.DEFAULT_PRIOR_DAYS


def test_busy_market_is_fast_and_reliable():
    e = lq.expected_days(S(sold_30d=60, sold_90d=170, active=20, median_sold=100), "phone", 95)
    assert e.grade == "A" and e.reliable and e.basis == "market"


def test_crowded_stale_market_is_slow():
    e = lq.expected_days(S(sold_30d=6, sold_90d=15, active=200, median_sold=100), "phone", 100)
    assert e.grade == "D" and e.reliable and e.days > 60


def test_stale_listings_are_discounted_from_the_competition():
    crowded = lq.expected_days(S(sold_30d=10, active=100, median_sold=100), "x", 100)
    mostly_stale = lq.expected_days(S(sold_30d=10, active=100, stale_active=80, median_sold=100), "x", 100)
    assert mostly_stale.days < crowded.days and any("stale" in n for n in mostly_stale.notes)


def test_pricing_above_the_median_slows_the_sale():
    s = S(sold_30d=20, active=40, median_sold=100)
    cheap, fair, dear = (lq.expected_days(s, "x", p).days for p in (80, 100, 140))
    assert cheap < fair < dear


def test_thin_sales_lean_on_the_prior_and_are_not_reliable():
    e = lq.expected_days(S(sold_30d=1, active=5, median_sold=100), "bag", 100)
    assert not e.reliable and e.basis == "blended"
    assert abs(e.days - 54) < abs(lq.expected_days(S(sold_30d=40, active=5, median_sold=100), "bag", 100).days - 54)


def test_no_sales_at_all_is_flagged():
    e = lq.expected_days(S(sold_30d=0, sold_90d=0, active=50, median_sold=100), "x", 100)
    assert e.grade == "D" and any("no sales" in n for n in e.notes)


def test_your_own_realized_days_pull_the_estimate():
    s = S(sold_30d=30, active=30, median_sold=100)
    base = lq.expected_days(s, "x", 100)
    cal = lq.expected_days(s, "x", 100, calibrated=(70.0, 10))
    assert cal.basis == "calibrated" and cal.days > base.days and any("realized" in n for n in cal.notes)


def test_estimates_are_clamped_and_text_is_readable():
    e = lq.expected_days(S(sold_30d=500, active=1, median_sold=1), "x", 1)
    assert e.days >= 1.0 and e.low >= 1.0
    assert "grade" in lq.expected_days(None, "bag").text() and "LOW CONFIDENCE" in lq.expected_days(None, "bag").text()


def test_carry_cost_and_markdown_schedule():
    assert lq.carry_cost(365, 30, 0.25) == pytest.approx(7.5)
    assert lq.suggested_markdown(10) == (0.0, "hold price")
    assert lq.suggested_markdown(30)[0] == 0.15 and lq.suggested_markdown(50)[0] == 0.30
    assert lq.suggested_markdown(70)[0] == 0.50 and "liquidate" in lq.suggested_markdown(120)[1]


# ---------------------------------------------------------------- persistence + calibration
def test_stats_are_saved_once_a_day_and_read_back(tmp_path):
    conn = db.connect(str(tmp_path / "l.db"))
    lq.save_stats(conn, lq.MarketStats("k", "s", sold_30d=5, active=20))
    lq.save_stats(conn, lq.MarketStats("k", "s", sold_30d=9, active=18))   # same day: updates
    assert conn.execute("SELECT COUNT(*) FROM market_stats").fetchone()[0] == 1
    got = lq.latest_stats(conn, "k")
    assert (got.sold_30d, got.active) == (9, 18) and lq.latest_stats(conn, "other") is None


def test_trades_measure_days_to_sell(tmp_path):
    conn = db.connect(str(tmp_path / "l.db"))
    day = 86400
    t0 = int(time.time()) - 100 * day
    kb.add_trade(conn, "Widget A", 20, 60, listed_at=t0, sold_at=t0 + 10 * day, channel="ebay")
    kb.add_trade(conn, "Widget A", 20, 60, listed_at=t0, sold_at=t0 + 30 * day, channel="ebay")
    kb.add_trade(conn, "Widget B", 20, 60, listed_at=t0, sold_at=t0 + 20 * day)
    kb.add_trade(conn, "Widget C", 20, 60)    # no listing date: ignored
    assert lq.calibration(conn, kb.item_key("Widget A")) == (20.0, 2)
    assert lq.calibration(conn, "nothing") is None
    assert lq.calibration(conn) == (20.0, 3)  # overall median needs >=3


def test_closing_a_trade_records_the_sale_as_realized_evidence(tmp_path):
    conn = db.connect(str(tmp_path / "l.db"))
    tid = kb.add_trade(conn, "Widget", 30, listed_at=int(time.time()) - 5 * 86400, list_price=90)
    assert conn.execute("SELECT sold_at FROM trades").fetchone()[0] is None
    kb.close_trade(conn, tid, 80, sell_fees=12, channel="ebay")
    assert conn.execute("SELECT sell_price FROM trades").fetchone()[0] == 80
    assert kb.compute_range(conn, "widget").basis == "realized"
    with pytest.raises(ValueError):
        kb.close_trade(conn, 999, 1)


# ---------------------------------------------------------------- provider parsers
def test_soldcomps_active_parser_uses_watchers_and_units_sold():
    rows = SoldCompsClient.parse_active({"items": [
        {"title": "A", "currentPrice": "120.50", "currentCurrency": "USD", "watcherCount": 7, "unitsSold": 3, "acceptsOffers": True},
        {"title": "B", "currentPrice": "99", "currentCurrency": "GBP"}, {"title": "C", "currentPrice": "oops"}]})
    assert rows == [{"title": "A", "price": 120.5, "watchers": 7, "units_sold": 3, "created": None, "accepts_offers": True}]


def test_browse_active_parser_reads_listing_age():
    rows = EbayClient.parse_active({"itemSummaries": [
        {"title": "X", "price": {"value": "50.00", "currency": "USD"}, "watchCount": 4,
         "itemCreationDate": "2026-05-01T12:00:00.000Z", "buyingOptions": ["FIXED_PRICE", "BEST_OFFER"]},
        {"title": "Y", "price": {"value": "5", "currency": "EUR"}}]})
    assert len(rows) == 1 and rows[0]["watchers"] == 4 and rows[0]["accepts_offers"] and rows[0]["created"].year == 2026


def test_router_prefers_soldcomps_active_and_falls_back_to_browse():
    class SC:
        blocked_reason = None

        def __init__(self, rows): self.rows = rows

        async def active(self, q): return self.rows

    class EB:
        async def active_listings(self, q): return [{"title": "eb", "price": 1}]

    assert run(CompsRouter(SC([{"title": "sc"}]), EB()).active("q")) == [{"title": "sc"}]
    assert run(CompsRouter(SC(None), EB()).active("q")) == [{"title": "eb", "price": 1}]
    assert run(CompsRouter(None, None).active("q")) is None


def test_browse_active_listings_request_and_cache():
    seen = []

    def handler(req):
        if req.url.path.endswith("/token"):
            return httpx.Response(200, json={"access_token": "t", "expires_in": 7200})
        seen.append(dict(req.url.params))
        return httpx.Response(200, json={"itemSummaries": [{"title": "X", "price": {"value": "10", "currency": "USD"}}]})

    async def go():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as h:
            c = EbayClient(h, "i", "s", False, False)
            return await c.active_listings("fluke 87v"), await c.active_listings("fluke 87v")

    a, b = run(go())
    assert len(a) == 1 and a == b and len(seen) == 1 and "FIXED_PRICE" in seen[0]["filter"] and seen[0]["limit"] == "200"


# ---------------------------------------------------------------- scanner gate
class Router:
    """A comps provider with dated sold items and a competition feed."""

    def __init__(self, n_sold, n_active, sold_days=range(0, 25), price=500.0):
        self.n_sold, self.n_active, self.sold_days, self.price = n_sold, n_active, list(sold_days), price

    async def comps(self, q, limit=50):
        return Comps("sold", [Comp("Dell Latitude 7420 Laptop", self.price + i, NOW - timedelta(days=self.sold_days[i % len(self.sold_days)]))
                              for i in range(self.n_sold)])

    async def active(self, q):
        return [{"title": "Dell Latitude 7420 Laptop", "price": 540.0, "watchers": 2, "created": None} for _ in range(self.n_active)]


def _scanner(tmp_path, monkeypatch, comps, **cfg_kw):
    from main import Scanner
    monkeypatch.setenv("DB_PATH", str(tmp_path / "t.db"))
    sc = Scanner.__new__(Scanner)
    sc.cfg, sc.conn = Config(min_market_value=0, **cfg_kw), db.connect(str(tmp_path / "t.db"))
    sc.engine, sc.ebay, sc._comp_cache, sc.dry_run = ValuationEngine(sc.cfg), comps, {}, True
    sc._ebay_gate, sc._warned_no_sold, sc.retail = asyncio.Semaphore(2), False, None
    return sc


def _l(price=65.0):
    return Listing("shopgoodwill", "L1", "Dell Latitude 7420 Laptop", price, 0.0, "u", None, "c")


def _row(sc):
    return sc.conn.execute("SELECT status, liquidity_days, liquidity_grade, detail_notes FROM listings").fetchone()


def test_a_fast_market_alerts_and_records_the_grade(tmp_path, monkeypatch):
    sc = _scanner(tmp_path, monkeypatch, Router(n_sold=30, n_active=12))
    l = _l()
    run(sc._process(l))
    r = _row(sc)
    assert r["status"] == "alerted" and r["liquidity_grade"] in ("A", "B") and r["liquidity_days"] < 30
    assert "Will it sell" not in str(l.extra) and l.extra["liquidity"]["reliable"] and "carrying cost" in l.extra["liquidity"]["text"]
    assert sc.conn.execute("SELECT COUNT(*) FROM market_stats").fetchone()[0] == 1


def test_a_reliably_slow_market_blocks_the_alert(tmp_path, monkeypatch):
    sc = _scanner(tmp_path, monkeypatch, Router(n_sold=6, n_active=250))
    run(sc._process(_l()))
    r = _row(sc)
    assert r["status"] == "illiquid" and r["liquidity_grade"] == "D" and "grade D" in r["detail_notes"]


def test_slow_estimates_resting_on_few_sales_do_not_block(tmp_path, monkeypatch):
    # six sales, all ~80 days ago, 250 active: the estimate says "very slow" but only 2 sales/month-equivalents back it
    sc = _scanner(tmp_path, monkeypatch, Router(n_sold=6, n_active=250, sold_days=[80]))
    l = _l()
    run(sc._process(l))
    assert _row(sc)["status"] == "alerted" and l.extra["liquidity"]["grade"] == "D" and l.extra["liquidity"]["reliable"] is False


def test_no_provider_data_reports_a_prior_but_never_blocks(tmp_path, monkeypatch):
    class Bare:
        async def comps(self, q, limit=50):
            return Comps("sold", [Comp("Dell Latitude 7420 Laptop", 500, NOW - timedelta(days=100)) for _ in range(6)])   # nothing recent
    sc = _scanner(tmp_path, monkeypatch, Bare())
    l = _l()
    run(sc._process(l))
    assert _row(sc)["status"] == "alerted" and l.extra["liquidity"]["reliable"] is False


def test_the_gate_threshold_is_configurable_and_can_be_off(tmp_path, monkeypatch):
    sc = _scanner(tmp_path, monkeypatch, Router(n_sold=6, n_active=250), max_days_to_sell=100000)
    run(sc._process(_l()))
    assert _row(sc)["status"] == "alerted"
    sc2 = _scanner(tmp_path, monkeypatch, Router(n_sold=6, n_active=250), liquidity_enabled=False)
    sc2.conn.execute("DELETE FROM listings"); sc2.conn.commit()
    l = _l()
    run(sc2._process(l))
    assert _row(sc2)["status"] == "alerted" and "liquidity" not in l.extra


def test_unprofitable_items_never_trigger_a_competition_lookup(tmp_path, monkeypatch):
    router = Router(n_sold=30, n_active=12)
    calls = []
    orig = router.active
    router.active = lambda q: (calls.append(q), orig(q))[1]
    sc = _scanner(tmp_path, monkeypatch, router)
    run(sc._process(_l(price=480.0)))            # buy price near resale value: no profit
    assert calls == []


def test_alert_embed_shows_the_liquidity_line():
    from alerting.discord import CRITICAL, build_embed
    from engine.valuation import Valuation
    l = _l()
    l.extra["liquidity"] = {"text": "grade B: ~22 days | 31 sold/30d vs 52 active"}
    e = build_embed(l, Valuation(300, 6, "sold", 100, 150, 150.0), CRITICAL)
    assert any(f["name"] == "Will it sell?" and "grade B" in f["value"] for f in e["fields"])


# ---------------------------------------------------------------- inventory + liquidity reports
def test_inventory_report_recommends_markdowns(tmp_path):
    conn = db.connect(str(tmp_path / "l.db"))
    day = 86400
    now = int(time.time())
    kb.add_trade(conn, "Fresh item", 20, listed_at=now - 5 * day, list_price=100)
    kb.add_trade(conn, "Stale item", 20, listed_at=now - 50 * day, list_price=100)
    kb.add_trade(conn, "Never listed", 20, bought_at=now - 10 * day)
    out = report.format_inventory(conn)
    assert "hold price" in out and "take 30% off -> $70" in out and "not listed yet: list it" in out
    assert "No realized sales" in out
    assert "No open trades" in report.format_inventory(db.connect(str(tmp_path / "empty.db")))


def test_liquidity_report_shows_stats_and_expectation(tmp_path):
    conn = db.connect(str(tmp_path / "l.db"))
    kb.add_observation(conn, "Fluke 87V", "sold", "x", low=250, high=330, typical=290, n=20)
    lq.save_stats(conn, lq.MarketStats("fluke 87v", "scanner", sold_30d=18, sold_90d=50, active=30, stale_active=10, median_sold=290))
    out = report.format_liquidity(conn, "fluke")
    assert "sold 30d: 18" in out and "sell-through" in out and "grade" in out
    assert "No market stats" in report.format_liquidity(conn, "nothing at all")


def test_cli_inventory_and_trade_commands(tmp_path, monkeypatch, capsys):
    import main
    path = str(tmp_path / "w.db")
    monkeypatch.setattr(main, "Config", lambda: Config(db_path=path))

    def cli(*args):
        monkeypatch.setattr("sys.argv", ["main.py", *args])
        asyncio.run(main.amain())
        return capsys.readouterr().out

    out = cli("--log-trade", "Widget", "--buy", "30", "--listed-at", "2026-09-01", "--list-price", "90")
    assert "widget" in out.lower()
    inv = cli("--inventory")
    assert "Widget" in inv and "take" in inv       # listed ~38 days ago
    out = cli("--close-trade", "1", "--sell", "75", "--fees", "10", "--sold-at", "2026-09-20")
    assert "No open trades" in out
    conn = db.connect(path)
    assert lq.calibration(conn, kb.item_key("Widget"))[0] == pytest.approx(19.0, abs=1)
