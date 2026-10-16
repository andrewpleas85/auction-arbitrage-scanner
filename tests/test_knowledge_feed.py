import asyncio
from datetime import datetime, timezone

import db
from alerting.discord import CRITICAL, build_embed
from clients.ebay import Comp, Comps, MockEbayClient
from config import Config
from engine import knowledge as kb
from engine.valuation import ValuationEngine, Valuation
from models import Listing

run = asyncio.run


class Comps500:
    def __init__(self, source="sold", price=500.0, n=6):
        self.source, self.price, self.n = source, price, n

    async def comps(self, q, limit=50):
        now = datetime.now(timezone.utc)
        return Comps(self.source, [Comp("Dell Latitude 7420 Laptop", self.price + i, now) for i in range(self.n)])


def _scanner(tmp_path, monkeypatch, comps):
    from main import Scanner
    monkeypatch.setenv("DB_PATH", str(tmp_path / "t.db"))
    sc = Scanner.__new__(Scanner)
    sc.cfg, sc.conn = Config(min_market_value=0, allow_active_comps=True), db.connect(str(tmp_path / "t.db"))
    sc.engine, sc.ebay, sc._comp_cache, sc.dry_run = ValuationEngine(sc.cfg), comps, {}, True
    sc._ebay_gate, sc._warned_no_sold, sc.retail = asyncio.Semaphore(2), False, None
    return sc


def _l(price=65.0):
    return Listing("shopgoodwill", "L1", "Dell Latitude 7420 Laptop", price, 0.0, "u", None, "c")


def test_sold_comps_are_saved_as_a_range(tmp_path, monkeypatch):
    sc = _scanner(tmp_path, monkeypatch, Comps500("sold", 500, 6))
    run(sc._process(_l()))
    o = kb.observations(sc.conn, kb.item_key("dell 7420"))[0]
    assert (o["kind"], o["low"], o["high"], o["n"]) == ("sold", 500.0, 505.0, 6) and o["typical"] == 502.5
    assert sc.conn.execute("SELECT low, high FROM value_ranges").fetchone()["low"] == 500.0


def test_asking_price_comps_are_saved_as_asks(tmp_path, monkeypatch):
    sc = _scanner(tmp_path, monkeypatch, Comps500("active", 400, 6))
    run(sc._process(_l()))
    assert kb.observations(sc.conn, "dell 7420")[0]["kind"] == "ask"


def test_repeat_lookups_the_same_day_update_instead_of_piling_up(tmp_path, monkeypatch):
    sc = _scanner(tmp_path, monkeypatch, Comps500("sold", 500, 6))
    run(sc._process(_l()))
    sc._comp_cache.clear()
    sc.conn.execute("DELETE FROM listings")
    sc.ebay = Comps500("sold", 520, 6)
    run(sc._process(_l()))
    obs = kb.observations(sc.conn, "dell 7420")
    assert len(obs) == 1 and obs[0]["low"] == 520.0


def test_mock_comps_never_enter_the_knowledge_base(tmp_path, monkeypatch):
    sc = _scanner(tmp_path, monkeypatch, MockEbayClient())
    run(sc._process(_l()))
    assert kb.observations(sc.conn, "dell 7420") == []


def test_estimate_far_above_what_we_already_knew_is_flagged(tmp_path, monkeypatch):
    sc = _scanner(tmp_path, monkeypatch, Comps500("sold", 500, 6))
    kb.add_observation(sc.conn, "dell 7420", "sold", "swappa", low=80, high=120, typical=100, n=10)
    kb.add_observation(sc.conn, "dell 7420", "sold", "ebay", low=90, high=130, typical=110, n=10)
    l = _l()
    run(sc._process(l))
    assert "ABOVE the known range" in l.extra["cross_check"]
    note = sc.conn.execute("SELECT detail_notes FROM listings").fetchone()[0]
    assert "ABOVE" in note


def test_estimate_inside_the_known_range_is_not_flagged_and_gets_a_grade(tmp_path, monkeypatch):
    sc = _scanner(tmp_path, monkeypatch, Comps500("sold", 100, 6))
    kb.add_observation(sc.conn, "dell 7420", "sold", "swappa", low=80, high=130, typical=105, n=10)
    l = _l(price=20.0)
    run(sc._process(l))
    assert "cross_check" not in l.extra and l.extra["grade"] in ("A", "B") and "known_range" in l.extra


def test_first_ever_lookup_is_not_checked_against_itself(tmp_path, monkeypatch):
    sc = _scanner(tmp_path, monkeypatch, Comps500("sold", 500, 6))
    l = _l()
    run(sc._process(l))
    assert "cross_check" not in l.extra  # nothing was known BEFORE this lookup


def test_alert_shows_range_grade_and_cross_check():
    l = _l()
    l.extra.update(known_range="$80 to $130, typically $105 [B, sold, 1 source(s)]", grade="B",
                   cross_check="ESTIMATE $500 IS ABOVE the known range")
    e = build_embed(l, Valuation(300, 6, "sold", 100, 150, 150.0), CRITICAL)
    names = [f["name"] for f in e["fields"]]
    assert "Value range on file" in names and "Confidence" in names and e["fields"][0]["name"].startswith("⚠ Cross-check")
