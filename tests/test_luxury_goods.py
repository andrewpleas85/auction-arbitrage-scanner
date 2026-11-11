import time
from datetime import datetime, timedelta, timezone

import pytest

import db
import luxury_goods as lg
import repair

NOW = datetime(2026, 11, 11, tzinfo=timezone.utc)
TITLE = "Louis Vuitton Speedy 30 Monogram Canvas Handbag"


@pytest.fixture
def conn(tmp_path):
    return db.connect(str(tmp_path / "t.db"))


def seed(conn, title, prices, grade_words, days_ago=10):
    for i, p in enumerate(prices):
        d = (NOW - timedelta(days=days_ago + i)).isoformat()
        conn.execute("INSERT OR IGNORE INTO sold_sales (provider, query, title, price, sold_date, fetched_at) VALUES (?,?,?,?,?,?)",
                     ("soldcomps", "q", f"{title} {grade_words} #{i}", p, d, int(time.time())))
    conn.commit()


def seed_lv(conn, excellent=2500, good=2000, worn=900, n=6, title=TITLE):
    seed(conn, title, [excellent + i * 10 for i in range(n)], "Excellent condition")
    seed(conn, title, [good + i * 10 for i in range(n)], "Good condition")
    seed(conn, title, [worn + i * 10 for i in range(n)], "Fair worn scuffs wear")


def item(desc, bid=400, title=TITLE, climb=None, **kw):
    return {"title": title, "description": desc, "platform": "propertyroom", "platform_label": "PropertyRoom", "listing_id": "1", "url": "https://x",
            "end": None, "bid": bid, "climb": climb, "prem": 0.0, "tax": 0.1025, "inbound": 15.0, **kw}


DOCUMENTED = "Date code SD1234 inside, authenticity card and dust bag included. "


def test_damaged_but_repairable_bag_clears_after_repair_authentication_and_post_repair_discount(conn):
    seed_lv(conn)
    r = lg.assess(conn, item(DOCUMENTED + "Corner wear and a broken zipper."), NOW)
    ev = r["evidence"]
    assert r["status"] == "verified" and ev["play"] == "repair" and ev["authenticity"]["verdict"] == "plausible"
    rp = ev["plays"]["repair"]
    assert {d["key"] for d in ev["damage"]["items"]} == {"corner_wear", "zipper"}
    assert rp["repair_cost_mid"] == (60 + 240) / 2 + (75 + 300) / 2                                            # published Luxury Bag Rehab corner price and zipper range
    assert ev["auth"]["third_party_cost"] == 30.0 == lg.AUTH_COST["bag"] and ev["auth"]["probability_genuine"] == 0.75   # authentication priced in, and the risk
    assert ev["as_is_grade"] == "fair" and ev["repaired_grade"] == "good" and rp["grade"] == "good"             # a repair lifts ONE grade
    assert rp["value"] < ev["clean_median"] and ev["post_repair_discount"] > 0.1
    assert ev["post_repair_discount"] == pytest.approx(1 - rp["value"] / ev["clean_median"], abs=0.001)
    assert rp["net_expected"] >= lg.MIN_NET + repair.REPAIR_CUSHION and rp["return_expected"] >= lg.MIN_RETURN
    assert rp["net_if_genuine"] > rp["net_expected"]
    asis = ev["plays"]["as-is"]
    assert asis["grade"] == "fair" and asis["value"] < 1000                                                     # as-is value comes from FAIR comps, never clean minus a factor
    assert ev["demand"]["sold_per_30d"] == 2.0 and ev["demand"]["time_to_sell"].startswith("unknown")          # velocity shown; time to sell not invented
    assert rp["value_tier"].startswith("25th percentile")                                                       # n=6 comps: 25th percentile


def test_a_repaired_bag_is_never_priced_as_excellent():
    assert lg.repaired_grade("fair") == "good" and lg.repaired_grade("good") == "very good"
    assert lg.repaired_grade("very good") == "very good" and lg.repaired_grade("excellent") == "very good"   # capped: at most one grade, never excellent


def test_cosmetic_only_damage_is_good_as_is_and_very_good_repaired(conn):
    seed(conn, TITLE, [2200 + i * 10 for i in range(6)], "Very good condition")
    seed(conn, TITLE, [2000 + i * 10 for i in range(6)], "Good condition")
    ev = lg.assess(conn, item(DOCUMENTED + "Light scuffs on the corners."), NOW)["evidence"]
    assert ev["as_is_grade"] == "good" and ev["repaired_grade"] == "very good" and ev["plays"]["repair"]["grade"] == "very good"


def test_grade_medians_are_checked_against_the_published_value_bands(conn):
    seed(conn, TITLE, [3000 + i for i in range(4)], "New with tags")
    seed(conn, TITLE, [2700 + i for i in range(6)], "Excellent condition")  # 90% of new: inside the 85-94% band
    seed(conn, TITLE, [900 + i for i in range(6)], "Good condition")         # 30% of new: far outside the 50-69% band
    ev = lg.assess(conn, item(DOCUMENTED), NOW)["evidence"]
    bands = {b["grade"]: b["in_band"] for b in ev["band_check"]}
    assert bands == {"excellent": True, "good": False}


@pytest.mark.parametrize("desc,key", [
    ("Cream vachetta tote. There is an ink stain on the light leather.", "ink_on_light_leather"),
    ("Strong smoke odor, smells like cigarette smoke.", "mould_or_deep_odour"),
    ("Musty and some mildew inside.", "mould_or_deep_odour"),
    ("The canvas is cracked and peeling all over.", "cracked_or_peeling_coating"),
])
def test_value_killing_damage_hard_fails_whatever_the_price(conn, desc, key):
    seed_lv(conn)
    r = lg.assess(conn, item(DOCUMENTED + desc, bid=1), NOW)  # $1 bid: the discount is irrelevant
    assert r["status"] == "unverified" and r["failed"] == "damage (value-killing)"
    assert key in [k["key"] for k in r["evidence"]["damage"]["killers"]] and "hard fail" in r["failures"][0]["why"]


def test_ink_on_dark_leather_is_repairable_not_a_killer():
    d = lg.find_damage("Prada black leather tote handbag", "Black leather. Small ink mark on the side.")
    assert not d["killers"] and [i["key"] for i in d["items"]] == ["ink_on_dark_leather"]


def test_counterfeit_risk_fails_on_authenticity_even_with_great_price_math(conn):
    seed_lv(conn)
    r = lg.assess(conn, item("AAA replica style Louis Vuitton Speedy, mirror quality, great condition", bid=20), NOW)
    assert r["status"] == "unverified" and r["failed"] == "authenticity" and "counterfeit" in r["failures"][0]["why"]


def test_brand_with_no_proof_cannot_be_supported_and_fails_before_any_price_math(conn):
    seed_lv(conn)
    r = lg.assess(conn, item("Nice bag, carried a few times.", bid=20), NOW)
    assert r["failed"] == "authenticity" and r["evidence"]["authenticity"]["verdict"] == "unsupported" and "plays" not in r["evidence"]


def test_clean_comps_minus_a_factor_are_never_used_for_a_damaged_bag(conn):
    seed(conn, TITLE, [2500, 2510, 2520, 2530, 2540, 2550], "Excellent condition")   # only CLEAN comps exist
    seed(conn, TITLE, [1500, 1510, 1520, 1530, 1540, 1550], "")                      # ungraded comps never count either
    r = lg.assess(conn, item(DOCUMENTED + "Corner wear and a broken zipper."), NOW)
    assert r["status"] == "unverified" and r["failed"] == "condition-matched comps"
    assert "fair" in r["failures"][0]["why"] and "no flat discount" in r["failures"][0]["why"]


def test_supported_authenticity_removes_the_authentication_cost(conn):
    seed_lv(conn)
    r = lg.assess(conn, item("Original receipt, date code SD1234. Corner wear."), NOW)
    assert r["evidence"]["authenticity"]["verdict"] == "supported" and r["evidence"]["auth"]["third_party_cost"] == 0.0


def test_brand_tier_changes_the_demand_bar_and_the_channels(conn):
    gucci = "Gucci Pelham Medium Tote Black Leather Handbag"
    seed(conn, gucci, [900, 910, 905, 895], "Good condition")                         # 4 recent sales: enough for a top brand, not a mid brand
    r = lg.assess(conn, item("Gucci. Date code, authenticity card and receipt included.", bid=50, title=gucci), NOW)
    assert r["failed"] == "condition-matched comps"                                   # 4 < MIN_COMPS: comps first
    seed(conn, gucci, [900, 910, 905, 895, 920], "Good condition", days_ago=20)
    seed(conn, "Chanel Classic Flap Jumbo Caviar Black Handbag", [4000, 4010, 4020, 4030, 4040, 4050], "Good condition", days_ago=30)
    gcr = lg.assess(conn, item("Gucci. Date code, authenticity card and receipt included.", bid=50, title=gucci), NOW)
    assert gcr["evidence"]["brand_tier"] == "mid" and gcr["evidence"]["demand"]["recent_needed"] == 5
    ch = lg.assess(conn, item("Chanel. Date code, authenticity card and receipt included.", bid=50, title="Chanel Classic Flap Jumbo Caviar Black Handbag"), NOW)
    assert ch["evidence"]["brand_tier"] == "top" and ch["evidence"]["demand"]["recent_needed"] == 3
    assert ch["evidence"]["demand"]["time_to_sell"].startswith("unknown")


def test_low_tier_brands_cannot_use_the_consignment_houses():
    best, chans = lg._best_channel(500.0, "other", "bag")
    assert "The RealReal (consign)" not in chans and "Fashionphile (consign)" not in chans and best in chans
    best_top, chans_top = lg._best_channel(500.0, "top", "bag")
    assert {"The RealReal (consign)", "Fashionphile (consign)", "Vestiaire Collective"} <= set(chans_top)


def test_clothing_damage_taxonomy():
    d = lg.find_damage("Burberry trench coat", "Two moth holes and a missing button. Seam split at hem.")
    assert {i["key"] for i in d["items"]} == {"moth_holes", "missing_buttons", "seam_split"} and not d["killers"] and not d["structural"] is None
    assert lg.find_damage("Burberry trench coat", "Extensive moth holes throughout.")["killers"]
    assert lg.find_damage("Burberry trench coat", "Faint smoke smell.")["items"][0]["key"] == "mild_odour"
    assert not lg.find_damage("Burberry trench coat", "No stains, no odor, no holes.")["items"]  # denials are not damage


def test_every_repairable_entry_names_its_source_and_range():
    for d in lg.DAMAGE:
        if d["verdict"] == "repairable":
            assert d["cost"][0] <= d["cost"][1] and d["src"] and d["post"] in ("good", "excellent")


def test_page_renders_verified_and_unverified_designer_sections(conn):
    import webpage
    seed_lv(conn)
    ok = lg.assess(conn, item(DOCUMENTED + "Corner wear and a broken zipper."), NOW)
    bad = lg.assess(conn, item(DOCUMENTED + "Strong smoke odor.", bid=5), NOW)
    payload = {"verified": [ok], "unverified": [bad], "bar": {"min_net": lg.MIN_NET, "min_return": lg.MIN_RETURN, "min_comps": lg.MIN_COMPS,
                                                             "repair_cushion": repair.REPAIR_CUSHION, "recent_needed": lg.MIN_RECENT_SALES, "p_auth": lg.P_AUTH}}
    html = webpage.render([], lux=payload)
    assert 'id="luxbox"' in html and "authenticity first" in html and "renderLux()" in html and "damage (value-killing)" in html
