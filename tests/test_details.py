import asyncio
from datetime import datetime, timezone

import httpx
import pytest

import db
from alerting.discord import CRITICAL, build_embed
from clients.ebay import Comp, Comps
from clients.propertyroom import PropertyRoomClient
from config import Config
from engine.details import (Assessment, DEFAULT_FIX_COSTS, parse_description, parse_fix_costs,
                            title_missing_parts)
from engine.valuation import ValuationEngine, Valuation
from models import Listing

run = asyncio.run


def page(*items, intro="Get a great deal with this online auction presented by PropertyRoom.com on behalf of a law enforcement or public agency client."):
    """A listing page shaped like PropertyRoom's: Word-pasted <ul><li><div><span> bullets, then the terms."""
    lis = "".join(f'<li><div class=MsoNormal style="MARGIN: 0in"><span>{i}</span></div>' for i in items)
    return (f"<html><body><nav>Cell Phones Home &amp; Pro Audio-Video Tools &amp; Equipment</nav>"
            f"<p><span>{intro}</span></p><ul>{lis}</ul>"
            f"<p>All sales are final and cannot be returned for any reason.</p>"
            f"<h3>Shipping and Returns</h3><p>Shipping Weight: 3 pounds</p></body></html>")


IPHONE12 = page("Mode</span><span>l: A2341", "Service Provider: Unknown", "Unable to test&nbsp;SIM card functionality",
                "Apple Account Activation&nbsp;Lock: No", "Carrier Activation Status: Unknown",
                "Testing Results: Tested, Powered On, and Responded to Touch; Restored to Factory Settings",
                "Condition: Fair")
DELL = page("Model: P135G", "Processor: 11th Gen Intel Core i7-1185G7", "Hard Drive: Removed", "RAM: 16 GB", "Battery: Yes",
            "Power Adapter: None", "Testing Results: Powers on, displays, hard drive removed", "Condition: Fair",
            "Battery life on this unit has not been tested.",
            "Due to licensing restrictions, this item will be shipped without any software, including operating system software.")
LOCKED = page("Model: A2846", "Apple Account Activation Lock: Yes", "Testing Results: Powered on, activation lock screen displayed")
DEAD = page("Model: X1", "Testing Results: Does not power on")
UNTESTED_PAGE = page("Model: X1", "Testing Results: Untested, unable to test without power cord")
DESKTOP = page("Model: OptiPlex", "Battery: None", "Power Adapter: Yes", "Testing Results: Powers on")


def test_real_style_iphone_page():
    a = parse_description(IPHONE12, "Apple iPhone 12 Pro")
    assert a.found and a.activation_lock is False and a.tested_ok is True and not a.dead and not a.untested
    assert a.missing == {} and "SIM untested" in a.notes and "carrier status unknown" in a.notes
    assert a.attrs["Model"] == "A2341"  # the broken <span>Mode</span><span>l: ...</span> markup is stitched together


def test_dell_missing_drive_and_adapter_are_costed():
    a = parse_description(DELL, "Dell Latitude 7420 Laptop")
    assert a.tested_ok and a.missing == {"storage drive": 35.0, "power adapter": 25.0}
    assert a.fix_cost == 60.0 and "battery untested" in a.notes and "no operating system" in a.notes
    assert "Needs: storage drive (~$35), power adapter (~$25)" in a.summary()


@pytest.mark.parametrize("html_page,field,expected", [
    (LOCKED, "activation_lock", True), (DEAD, "dead", True), (UNTESTED_PAGE, "untested", True),
    (IPHONE12, "tested_ok", True), (DEAD, "tested_ok", None), (UNTESTED_PAGE, "tested_ok", None),
])
def test_assessment_flags(html_page, field, expected):
    assert getattr(parse_description(html_page, "x"), field) == expected


def test_battery_none_only_costs_real_money_on_portable_devices():
    assert parse_description(DESKTOP, "Dell OptiPlex 5090 Desktop").missing == {"battery": 5.0}
    laptop = page("Battery: None", "Testing Results: Powers on")
    assert parse_description(laptop, "Dell Latitude laptop").missing == {"battery": 40.0}


def test_unrecognised_page_assumes_nothing():
    noise = "<html><body><nav>Cell Phones Computers Tools</nav><p>Sign in</p></body></html>"
    a = parse_description(noise, "Dell laptop")
    assert not a.found and a.tested_ok is None and a.activation_lock is None and a.missing == {} and not a.dead


def test_title_missing_parts():
    assert title_missing_parts("Behringer Xenyx 802 Used No Power Cord") == {"power cord": 8.0}
    assert title_missing_parts("Piano (Power cord not included)") == {"power cord": 8.0}
    assert title_missing_parts("TV without remote") == {"remote": 12.0}
    assert title_missing_parts("Dell laptop missing charger, no stand") == {"charger": 15.0, "stand": 20.0}
    assert title_missing_parts("Camera w/o battery") == {"battery": 40.0}
    assert title_missing_parts("Gas indicator No Battery") == {"battery": 5.0}
    assert title_missing_parts("Sony TV with remote and stand") == {}


def test_fix_cost_overrides():
    c = parse_fix_costs("power adapter=40, remote=bad, foo")
    assert c["power adapter"] == 40.0 and c["remote"] == DEFAULT_FIX_COSTS["remote"]
    assert title_missing_parts("TV no remote", {"remote": 99.0}) == {"remote": 99.0}


@pytest.mark.parametrize("title,a,expected", [
    ("Dell Latitude 7420", Assessment(found=True, tested_ok=True), 1.0),
    ("Dell Latitude 7420", Assessment(found=True, tested_ok=True, activation_lock=True), 0.2),
    ("Dell Latitude 7420", Assessment(found=True, dead=True), 0.2),
    ("Dell Latitude 7420", Assessment(found=True, untested=True), 0.6),
    ("Dell Latitude 7420", Assessment(found=True), 1.0),
    ("Dell Latitude 7420 Untested", Assessment(found=True, tested_ok=True), 1.0),   # page evidence beats title guess
    ("iPhone iCloud Locked", Assessment(found=True, tested_ok=True), 0.2),           # a lock word in the title always wins
    ("Dell Latitude Refurbished", Assessment(found=True, tested_ok=True), 1.10),
])
def test_detail_condition_factor(title, a, expected):
    assert ValuationEngine(Config()).detail_condition_factor(title, a) == pytest.approx(expected)


def _comps(v):
    return Comps("sold", [Comp("Dell Latitude 7420 Laptop", v, datetime.now(timezone.utc)) for _ in range(6)])


def test_fix_cost_raises_cost_and_lowers_roi():
    eng = ValuationEngine(Config())
    base = eng.valuate("dell 7420", _comps(200), 65, 15, "Dell Latitude 7420 Laptop", buyer_premium_pct=15)
    fixed = eng.valuate("dell 7420", _comps(200), 65, 15, "Dell Latitude 7420 Laptop", buyer_premium_pct=15, fix_cost=60)
    assert fixed.cost == pytest.approx(base.cost + 60) and fixed.net_profit == pytest.approx(base.net_profit - 60)
    assert "includes $60" in fixed.detail and fixed.roi_pct < base.roi_pct


# ------------------------------------------------ scanner integration
class FixedComps:
    def __init__(self, v):
        self.v = v

    async def comps(self, q, limit=50):
        return _comps(self.v)


class StubPR:
    def __init__(self, assessment):
        self.assessment, self.calls = assessment, []

    async def assess(self, listing, costs=None, ttl=900):
        self.calls.append(listing.listing_id)
        return self.assessment


def _scanner(tmp_path, monkeypatch, assessment, comps=200):
    from main import Scanner
    monkeypatch.setenv("DB_PATH", str(tmp_path / "t.db"))
    sc = Scanner.__new__(Scanner)
    sc.cfg, sc.conn = Config(min_market_value=0), db.connect(str(tmp_path / "t.db"))
    sc.engine, sc.ebay, sc._comp_cache, sc.dry_run = ValuationEngine(sc.cfg), FixedComps(comps), {}, True
    sc._ebay_gate, sc._warned_no_sold, sc.retail = asyncio.Semaphore(2), False, None
    sc.propertyroom = StubPR(assessment)
    return sc


def _l(platform="propertyroom", title="Dell Latitude 7420 Laptop", price=65.0):
    return Listing(platform, "L1", title, price, 15.0, "u", None, "c", extra_cost_pct=15)


def _status(sc):
    return sc.conn.execute("SELECT status, fix_cost, detail_notes, estimated_market_value FROM listings").fetchone()


def test_without_the_page_this_laptop_would_have_alerted(tmp_path, monkeypatch):
    sc = _scanner(tmp_path, monkeypatch, None)  # page unreadable => title valuation stands
    run(sc._process(_l()))
    assert _status(sc)["status"] == "alerted"


def test_missing_drive_and_adapter_kill_a_thin_deal(tmp_path, monkeypatch):
    sc = _scanner(tmp_path, monkeypatch, parse_description(DELL, "Dell Latitude 7420 Laptop"))
    run(sc._process(_l()))
    row = _status(sc)
    assert row["status"] == "skipped" and row["fix_cost"] == 60.0 and "storage drive" in row["detail_notes"]


def test_activation_locked_listing_is_dropped(tmp_path, monkeypatch):
    sc = _scanner(tmp_path, monkeypatch, parse_description(LOCKED, "Apple iPhone 15"))
    run(sc._process(_l(title="Dell Latitude 7420 Laptop")))
    assert _status(sc)["status"] == "skipped" and "ACTIVATION LOCKED" in _status(sc)["detail_notes"]


def test_clean_tested_listing_still_alerts_and_records_what_was_checked(tmp_path, monkeypatch):
    sc = _scanner(tmp_path, monkeypatch, parse_description(IPHONE12, "Dell Latitude 7420 Laptop"))
    run(sc._process(_l()))
    row = _status(sc)
    assert row["status"] == "alerted" and row["fix_cost"] is None and "Tested: powers on" in row["detail_notes"]


def test_details_are_only_fetched_for_profitable_propertyroom_listings(tmp_path, monkeypatch):
    sc = _scanner(tmp_path, monkeypatch, parse_description(IPHONE12, "x"), comps=70)  # nothing profitable
    run(sc._process(_l()))
    assert sc.propertyroom.calls == []
    sc2 = _scanner(tmp_path, monkeypatch, parse_description(IPHONE12, "x"))
    sc2.conn.execute("DELETE FROM listings"); sc2.conn.commit()
    run(sc2._process(_l(platform="shopgoodwill")))
    assert sc2.propertyroom.calls == []  # other sources never trigger a PropertyRoom page fetch


def test_title_missing_part_is_costed_on_any_source(tmp_path, monkeypatch):
    sc = _scanner(tmp_path, monkeypatch, None)
    run(sc._process(_l(platform="shopgoodwill", title="Dell Latitude 7420 Laptop No Power Adapter")))
    row = _status(sc)
    assert row["fix_cost"] == 25.0


# ------------------------------------------------ PropertyRoomClient.assess (HTTP)
def test_assess_fetches_parses_caches_and_fails_soft():
    calls = []

    def handler(req):
        calls.append(str(req.url))
        return httpx.Response(200, text=DELL) if "ok" in str(req.url) else httpx.Response(500)

    async def go():
        http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        c = PropertyRoomClient(http, [], delay=0)
        good = Listing("propertyroom", "1", "Dell Latitude 7420 Laptop", 65, 15, "https://pr/l/ok/1", None, "c")
        bad = Listing("propertyroom", "2", "Dell Latitude 7420 Laptop", 65, 15, "https://pr/l/bad/2", None, "c")
        a = await c.assess(good)
        again = await c.assess(good)
        failed = await c.assess(bad)
        await http.aclose()
        return a, again, failed

    a, again, failed = run(go())
    assert a.fix_cost == 60.0 and again is a and failed is None and len(calls) == 2


def test_embed_shows_listing_page_findings_and_title_needs():
    l = _l()
    l.extra["details"] = {"summary": "Needs: power adapter (~$25) | Tested: powers on"}
    e = build_embed(l, Valuation(300, 6, "sold", 100, 150, 150.0), CRITICAL)
    assert any(f["name"] == "Listing page says" and "power adapter" in f["value"] for f in e["fields"])
    l2 = _l(platform="shopgoodwill")
    l2.extra["needs"] = {"power cord": 8.0}
    e2 = build_embed(l2, Valuation(300, 6, "sold", 100, 150, 150.0), CRITICAL)
    assert any(f["name"] == "Needs" and "power cord (~$8)" in f["value"] for f in e2["fields"])
