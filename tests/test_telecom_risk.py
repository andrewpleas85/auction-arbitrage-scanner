import asyncio
from datetime import datetime, timedelta, timezone

import pytest

import db
from alerting.discord import CRITICAL, build_embed
from clients.ebay import Comp, Comps
from config import Config
from engine.details import parse_description
from engine.title_cleaner import (TELECOM_DISCLAIMER_RE, has_telecom_disclaimer, is_mobile_device, telecom_status)
from engine.valuation import ValuationEngine, Valuation
from models import Listing

# The exact government / police-auction boilerplate from PropertyRoom listings
BOILER = ("This item does not come with telecommunications service, such as Verizon, T-Mobile, AT&T, Dish Network and "
          "DirecTV necessary to make the item useful. Telecommunications service may not be available for various reasons "
          "including obsolescence, identification of this item as stolen on a telecommunication carriers records, liens and "
          "termination of the relevant telecommunication carriers business.")
ENG = ValuationEngine(Config())


# ---------------------------------------------------------------- A. disclaimer detection
@pytest.mark.parametrize("text", [
    BOILER,
    "This item does not come with telecommunications service, such as Verizon.",
    "Telecommunications service may not be available for various reasons",
    "may be identified as stolen on a telecommunication carriers records",
    "No carrier service is included with this device",
    "Device has been blocked in the carrier records",
    "THIS ITEM DOES NOT COME WITH TELECOMMUNICATION SERVICE",
    "necessary to make the item useful",
    "termination of the relevant telecommunication carrier's business",
])
def test_boilerplate_and_variants_are_detected(text):
    assert has_telecom_disclaimer(text)


@pytest.mark.parametrize("text", [
    "", None, "Works great, comes with charger and case", "Unlocked iPhone, tested with T-Mobile SIM, works",
    "Sold as is, all sales final", "Includes cellular plan receipt", "carrier unlocked",
])
def test_ordinary_text_is_not_flagged(text):
    assert not has_telecom_disclaimer(text)


def test_the_disclaimer_flags_but_never_discards():
    ts = telecom_status("Apple iPhone 14 128GB T-Mobile", BOILER)
    assert ts.unverified_telecom_status is True and ts.mobile and ts.status == "unverified"
    assert any("government telecom disclaimer" in r for r in ts.reasons)
    # still valued (not dropped): the factor is a haircut, not zero
    assert ENG.condition_factor("Apple iPhone 14 128GB T-Mobile", BOILER) == pytest.approx(0.2)


# ---------------------------------------------------------------- mobile devices
@pytest.mark.parametrize("title,mobile", [
    ("Apple iPhone 14 128GB T-Mobile", True), ("Samsung Galaxy S21 5G", True), ("Samsung Galaxy A02, Unknown Carrier, 32 GB", True),
    ("Google Pixel 7 128GB", True), ("Samsung Galaxy Luna 8GB Tracfone", True), ("Android smartphone lot", True),
    ("Apple iPad Air 64GB Wi-Fi + Cellular", True), ("Samsung Galaxy Watch 4 LTE", True), ("Apple Watch Series 8 GPS + Cellular", True),
    ("Apple iPad Air 64GB Wi-Fi", False), ("Samsung Galaxy Tab A 8in Wi-Fi", False), ("Apple Watch Series 3 GPS", False),
    ("Dell Latitude 7420 Laptop", False), ("Sony PlayStation 5 Console", False),
    ("Clear Case for Apple iPhone 13", False), ("iPhone 12 Charger and Cable", False), ("Tempered Glass Screen Protector iPhone 14", False),
    ("Samsung Gear VR Smartphone Virtual Reality Headset", False),
    ("Apple iPhone 13 128GB with Case and Charger", True),
])
def test_is_mobile_device(title, mobile):
    assert is_mobile_device(title) is mobile


# ---------------------------------------------------------------- B. the three tiers
def test_tier1_clean_or_factory_unlocked_is_100_percent():
    for title, text in [("Apple iPhone 13 128GB Factory Unlocked", ""), ("Apple iPhone 13 128GB", "Clean IMEI, never reported"),
                        ("Samsung Galaxy S21", "IMEI is clean"), ("Apple iPhone 12", "ESN clean and not blacklisted"),
                        ("Apple iPhone 13 128GB Factory Unlocked", BOILER)]:       # an explicit clean statement beats the boilerplate
        ts = telecom_status(title, text)
        assert ts.status == "clean", (title, text)
        assert ENG.condition_factor(title, text) == pytest.approx(1.0)


def test_tier2_setup_screen_or_no_information_is_50_percent():
    for title, text in [("Apple iPhone 13 128GB", ""), ("Apple iPhone 13 128GB", "Shows the Hello setup screen, IMEI not verified"),
                        ("Samsung Galaxy S21", "Restored to factory settings")]:
        ts = telecom_status(title, text)
        assert ts.status == "moderate" and not ts.unverified_telecom_status
        assert ENG.condition_factor(title, text) == pytest.approx(0.5)


def test_tier3_disclaimer_or_untested_is_20_percent():
    for title, text in [("Apple iPhone 13 128GB T-Mobile", BOILER), ("Apple iPhone 13 128GB Untested", ""),
                        ("Samsung Galaxy S21", "Unable to test SIM card functionality"), ("Samsung Galaxy S21", "not tested"),
                        ("Apple iPhone 13 128GB", "Restored to factory settings. " + BOILER)]:      # boilerplate beats the setup screen
        ts = telecom_status(title, text)
        assert ts.status == "unverified", (title, text)
        assert ENG.condition_factor(title, text) == pytest.approx(0.2)


def test_an_imei_number_alone_is_not_proof_of_a_clean_imei():
    ts = telecom_status("Apple iPhone 12 Pro 128GB", "IMEI: 35 390959 650205 9. Tested, powered on.")
    assert ts.status == "moderate"


def test_non_phones_are_untouched():
    assert ENG.condition_factor("Dell Latitude 7420 Laptop", BOILER) == pytest.approx(1.0)
    assert telecom_status("Dell Latitude 7420 Laptop", BOILER).status is None
    assert telecom_status("Dell Latitude 7420 Laptop", BOILER).unverified_telecom_status is True   # flag is still reported
    assert ENG.condition_factor("Apple iPad Air Wi-Fi", "") == pytest.approx(1.0)
    assert ENG.condition_factor("Apple iPad Air Wi-Fi + Cellular", "") == pytest.approx(0.5)


def test_telecom_risk_combines_with_other_condition_signals_by_taking_the_worse():
    assert ENG.condition_factor("Apple iPhone 13 128GB Untested", "") == pytest.approx(0.2)                # 0.6 untested vs 0.2 telecom
    assert ENG.condition_factor("Apple iPhone 13 128GB iCloud Locked", "") == pytest.approx(0.2)
    assert ENG.condition_factor("Apple iPhone 13 128GB Refurbished Factory Unlocked", "") == pytest.approx(1.10)   # clean keeps the uplift
    assert ENG.condition_factor("Apple iPhone 13 128GB Refurbished", "") == pytest.approx(0.5)


def test_factors_are_configurable():
    eng = ValuationEngine(Config(telecom_factor_clean=1.0, telecom_factor_moderate=0.7, telecom_factor_unverified=0.1))
    assert eng.condition_factor("Apple iPhone 13", "") == pytest.approx(0.7)
    assert eng.condition_factor("Apple iPhone 13", BOILER) == pytest.approx(0.1)


# ---------------------------------------------------------------- valuation outcome
def _comps(price=300.0):
    return Comps("sold", [Comp("Apple iPhone 13 128GB", price + i, datetime.now(timezone.utc)) for i in range(6)])


def test_a_clean_comp_is_discounted_by_tier_in_dollars():
    est = {}
    for name, title, text in [("clean", "Apple iPhone 13 128GB Factory Unlocked", ""), ("moderate", "Apple iPhone 13 128GB", ""),
                              ("unverified", "Apple iPhone 13 128GB", BOILER)]:
        v = ENG.valuate("iphone 13", _comps(), 40, 15, title, condition_factor=ENG.condition_factor(title, text))
        est[name] = v.estimated_value
    base = est["clean"]
    assert est["moderate"] == pytest.approx(base * 0.5, abs=0.01) and est["unverified"] == pytest.approx(base * 0.2, abs=0.01)
    assert est["unverified"] < est["moderate"] < est["clean"]


def test_upside_is_retained_when_an_unverified_phone_turns_out_clean():
    cheap = ENG.valuate("iphone 13", _comps(), 25, 15, "Apple iPhone 13 128GB",
                        condition_factor=ENG.condition_factor("Apple iPhone 13 128GB", BOILER))
    if_clean = ENG.valuate("iphone 13", _comps(), 25, 15, "Apple iPhone 13 128GB", condition_factor=1.0)
    assert not cheap.meets(Config()) or cheap.net_profit < if_clean.net_profit * 0.35     # bid is justified by parts value only
    assert if_clean.net_profit > 150 and if_clean.estimated_value / cheap.estimated_value == pytest.approx(5.0, abs=0.01)


# ---------------------------------------------------------------- from a real listing page
def _page(*lines, intro="Get a great deal with this online auction presented by PropertyRoom.com on behalf of a law enforcement or public agency client."):
    lis = "".join(f"<li><div><span>{x}</span></div>" for x in lines)
    return f"<html><body><p>{intro}</p><ul>{lis}</ul><h3>Shipping and Returns</h3></body></html>"


PR_IPHONE = _page("Model: A2341", "IMEI: 35 390959 650205 9", "Unable to test SIM card functionality", "Apple Account Activation Lock: No",
                  "Carrier Activation Status: Unknown", "Testing Results: Tested, Powered On, and Responded to Touch; Restored to Factory Settings",
                  BOILER)


def test_the_page_assessment_carries_the_disclaimer_and_text():
    a = parse_description(PR_IPHONE, "Apple iPhone 12 Pro 128GB")
    assert a.found and a.telecom_disclaimer and "does not come with telecommunications service" in a.text
    assert a.tested_ok and a.activation_lock is False                       # 'powers on' alone does not clear the carrier risk
    assert ENG.detail_condition_factor("Apple iPhone 12 Pro 128GB", a) == pytest.approx(0.2)


def test_a_page_with_a_clean_imei_statement_is_valued_in_full():
    page = _page("Model: A2341", "IMEI: 123", "Clean IMEI, verified on the carrier website", "Testing Results: Powers on, tested")
    a = parse_description(page, "Apple iPhone 12 Pro 128GB")
    assert ENG.detail_condition_factor("Apple iPhone 12 Pro 128GB", a) == pytest.approx(1.0)


def test_a_page_without_any_phone_info_gets_the_moderate_haircut():
    a = parse_description(_page("Model: A2341", "Testing Results: Powers on"), "Apple iPhone 12 Pro 128GB")
    assert ENG.detail_condition_factor("Apple iPhone 12 Pro 128GB", a) == pytest.approx(0.5)


def test_a_laptop_page_with_the_boilerplate_is_not_haircut():
    a = parse_description(_page("Processor: i7", "Testing Results: Powers on, displays", BOILER), "Dell Latitude 7420 Laptop")
    assert a.telecom_disclaimer and ENG.detail_condition_factor("Dell Latitude 7420 Laptop", a) == pytest.approx(1.0)


# ---------------------------------------------------------------- scanner + db + alert
class Comps500:
    async def comps(self, q, limit=50):
        return Comps("sold", [Comp("Apple iPhone 14 128GB", 500.0 + i, datetime.now(timezone.utc) - timedelta(days=i)) for i in range(8)])


class StubPR:
    def __init__(self, page, title):
        self.a = parse_description(page, title)

    async def assess(self, listing, costs=None, ttl=900):
        return self.a


def _scanner(tmp_path, monkeypatch, page):
    from main import Scanner
    monkeypatch.setenv("DB_PATH", str(tmp_path / "t.db"))
    sc = Scanner.__new__(Scanner)
    sc.cfg, sc.conn = Config(min_market_value=0, liquidity_enabled=False), db.connect(str(tmp_path / "t.db"))
    sc.engine, sc.ebay, sc._comp_cache, sc.dry_run = ValuationEngine(sc.cfg), Comps500(), {}, True
    sc._ebay_gate, sc._warned_no_sold, sc.retail = asyncio.Semaphore(2), False, None
    sc.propertyroom = StubPR(page, "Apple iPhone 14 128GB T-Mobile")
    return sc


def _l(price):
    return Listing("propertyroom", "P1", "Apple iPhone 14 128GB T-Mobile", price, 15.0, "https://pr/l/x/1", None, "c", extra_cost_pct=15)


def test_scanner_values_an_unverified_government_phone_as_parts_and_flags_it(tmp_path, monkeypatch):
    sc = _scanner(tmp_path, monkeypatch, PR_IPHONE)
    l = _l(95.0)       # fine for a clean $500 phone (50% factor passes the title-only check), ruinous at 20%
    asyncio.run(sc._process(l))
    row = sc.conn.execute("SELECT status, estimated_market_value, unverified_telecom FROM listings").fetchone()
    assert row["unverified_telecom"] == 1 and row["status"] in ("skipped", "watchlist")
    assert row["estimated_market_value"] == pytest.approx(0.2 * 503.5, abs=2)   # 20% of the clean trimmed-mean comp
    assert l.extra["telecom"]["status"] == "unverified" and l.extra["telecom"]["unverified_telecom_status"]
    assert any("GOVERNMENT TELECOM DISCLAIMER" in n for n in l.extra["details"]["summary"].split(" | "))


def test_the_same_phone_with_a_clean_statement_still_alerts(tmp_path, monkeypatch):
    page = _page("Model: A2341", "Clean IMEI", "Testing Results: Tested, powered on", BOILER)
    sc = _scanner(tmp_path, monkeypatch, page)
    asyncio.run(sc._process(_l(95.0)))
    row = sc.conn.execute("SELECT status, estimated_market_value, unverified_telecom FROM listings").fetchone()
    assert row["status"] == "alerted" and row["estimated_market_value"] > 450 and row["unverified_telecom"] == 1


def test_alert_explains_the_phone_risk_and_the_upside():
    l = _l(30)
    l.extra["telecom"] = {"status": "unverified", "unverified_telecom_status": True, "factor": 0.2,
                          "reasons": ["government telecom disclaimer: lock/blacklist status unverified"]}
    e = build_embed(l, Valuation(100, 6, "sold", 50, 40, 80.0), CRITICAL)
    f = next(x for x in e["fields"] if x["name"] == "Phone risk")
    assert "UNVERIFIED" in f["value"] and "20%" in f["value"] and "$500" in f["value"]      # 100 / 0.2 = the clean value


def test_plain_unlocked_is_trusted_unless_the_listing_says_otherwise():
    from engine.title_cleaner import telecom_status
    assert telecom_status("Apple iPad 9th Gen 64GB Wi-Fi + Cellular Tablet - Unlocked").status == "clean"
    assert telecom_status("Apple iPhone 12 64GB Unlocked").status == "clean"
    assert telecom_status("Apple iPhone 12 Activation Locked Unlocked Carrier").status == "unverified"
    assert telecom_status("Samsung Galaxy S9 Not Unlocked Verizon").status != "clean"
    assert telecom_status("Apple iPhone 13 128GB Carrier Locked T-Mobile").status == "unverified"
