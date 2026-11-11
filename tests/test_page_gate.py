import time
from datetime import datetime, timedelta, timezone

import pytest

import catalog
import db
import evidence
import page_gate as pg
import repair

NOW = datetime(2026, 11, 11, tzinfo=timezone.utc)
TITLE = "Denon AVR-X1700H receiver"
DESC = "Denon AVR-X1700H 7.2 channel receiver, works great."


@pytest.fixture
def conn(tmp_path):
    return db.connect(str(tmp_path / "t.db"))


def seed(conn, prices, title="Denon AVR-X1700H 7.2 Ch Receiver", days_ago=10):
    for i, p in enumerate(prices):
        conn.execute("INSERT OR IGNORE INTO sold_sales (provider, query, title, price, sold_date, fetched_at) VALUES (?,?,?,?,?,?)",
                     ("soldcomps", "q", f"{title} #{i}", p, (NOW - timedelta(days=days_ago + i)).isoformat(), int(time.time())))
    conn.commit()


def row(title=TITLE, bid=20.0, **kw):
    econ = {"prem": 0.0, "tax": 0.1025, "inbound": 10.0, "fee": catalog.EBAY_FEE, "order_fee": catalog.EBAY_ORDER_FEE, "fee_hi": None, "hi_at": None,
            "returns": catalog.RETURN_RESERVE, "ship_out": 40.0, "climb": 0.25}
    return {"title": title, "platform": "shopgoodwill", "platform_label": "ShopGoodwill", "listing_id": "9", "url": "https://x", "end": None, "bid": bid, "kind": "other",
            "econ": econ, "factor": 1.0, "fix": 0.0, "value": {"low": 400, "typ": 900, "high": 1200}, "range_notes": ["brand-level baseline"], "conf": "C",
            "profit_end": 500.0, "roi_end": 3.0, "profit_now": 600.0, "max_bid": 300.0, **kw}


def test_a_row_with_spec_matched_comps_and_high_identity_is_verified_from_the_comps_not_the_old_claim(conn):
    seed(conn, [300, 310, 290, 305, 295, 300, 298])
    g = pg.assess_row(conn, row(), NOW, description=DESC)
    ev = g["evidence"]
    assert g["status"] == "verified" and ev["play"] == "as-is" and ev["identity"]["confidence"] == "high"
    assert ev["value_tier"]["tier"].startswith("25th percentile") and ev["working_value"] < 300 < 900     # valued from the comps, not the old $900 claim
    assert ev["net"] >= evidence.MIN_NET and ev["return"] >= evidence.MIN_RETURN and ev["demand"]["sold_per_30d"] > 0
    assert ev["demand"]["time_to_sell"].startswith("unknown")
    assert g["claimed"]["worth_typ"] == 900 and "UNVERIFIED" in g["claimed"]["note"]                        # the old numbers are kept and labelled


def test_thin_comps_are_unverified_and_say_what_the_thin_tier_would_have_given(conn):
    seed(conn, [300, 310, 290, 305])
    g = pg.assess_row(conn, row(), NOW, description=DESC)
    assert g["status"] == "unverified" and g["failed"] == "sold comps" and "$150" in g["failures"][0]["why"]


def test_uncorroborated_identity_is_unverified_even_with_plenty_of_comps(conn):
    seed(conn, [300, 310, 290, 305, 295, 300, 298])
    g = pg.assess_row(conn, row(), NOW, description="Great receiver.")
    assert g["status"] == "unverified" and g["failed"] == "identity" and g["claimed"]["profit_at_expected_close"] == 500.0


def test_wide_spread_is_flagged_and_valued_low(conn):
    seed(conn, [100, 120, 400, 420, 800, 850, 900, 950, 1000, 1100, 1200, 1300])
    g = pg.assess_row(conn, row(), NOW, description=DESC)
    assert g["evidence"]["value_tier"]["wide"] and "wide spread" in g["evidence"]["flags"][0]


def test_repairable_lot_becomes_a_repair_play_not_a_main_table_row(conn):
    seed(conn, [450, 470, 430, 460, 440, 455], title="Apple iPhone 13 128GB Unlocked")
    r = row("Apple iPhone 13 128GB unlocked", bid=40.0, kind="phone")
    r["econ"]["ship_out"] = 7.0
    g = pg.assess_row(conn, r, NOW, description="Apple iPhone 13 128GB unlocked. Cracked screen, otherwise works.")
    ev = g["evidence"]
    assert ev["condition"]["class"] == "repairable" and "repair" in ev["plays"] and ev["plays"]["repair"]["repair_cost"] == 110.0
    assert ev["plays"]["as-is"]["value"] < ev["plays"]["repair"]["value"] / 2          # both numbers: parts value vs repaired value
    assert g["status"] == "verified" and ev["play"] == "repair" and ev["repair_play"]
    sp = pg.split(conn, [r | {"description": "Apple iPhone 13 128GB unlocked. Cracked screen, otherwise works."}], NOW)
    assert sp["main"] == [] and len(sp["repair"]) == 1 and sp["verified"] == [] and sp["unverified"] == []                               # a repair play never enters the main table


def test_untested_lot_is_priced_at_parts_value_and_unverified(conn):
    seed(conn, [300, 310, 290, 305, 295, 300, 298])
    g = pg.assess_row(conn, row(), NOW, description=DESC + " Untested, no power cord.")
    assert g["status"] == "unverified" and g["failed"] == "condition" and g["evidence"]["as_is_value"] == pytest.approx(g["evidence"]["working_value"] * repair.PARTS_FACTOR, abs=0.01)


def test_split_keeps_only_verified_tracked_and_scrap_rows_in_the_main_table(conn):
    seed(conn, [300, 310, 290, 305, 295, 300, 298])
    good = row()
    bad = row("Marantz SR6008 receiver", listing_id="2") | {"listing_id": "2"}
    tracked = row("Marantz SR6008 receiver") | {"listing_id": "3", "tracked": True}
    scrap = row("14k gold scrap lot") | {"listing_id": "4", "kind": "scrap"}
    sp = pg.split(conn, [bad, tracked, scrap], NOW)
    ids = {r["listing_id"] for r in sp["main"]}
    assert ids == {"3", "4"} and {g["listing_id"] for g in sp["unverified"]} == {"2", "3"}   # tracked rows stay visible; scrap is exempt (melt, not comps)
    assert all("gate" in r for r in sp["main"])


def test_repair_candidates_only_include_lots_that_state_a_repairable_defect(conn):
    end = (NOW + timedelta(hours=20)).isoformat()
    cols = "(platform, listing_id, title, brand, kind, style, category, listing_type, current_bid, buy_now, shipping, bids, end_time, url, first_seen, last_seen, domain, description, location)"
    for lid, title, desc in (("1", "Apple iPhone 13 128GB", "cracked screen, otherwise works"), ("2", "Apple iPhone 13 128GB", "works great"), ("3", "Apple iPhone 13 128GB", "won't turn on")):
        conn.execute(f"INSERT INTO catalog {cols} VALUES ('shopgoodwill',?,?,'Apple','phone','','','auction',25,NULL,NULL,0,?,'https://shopgoodwill.com/item/'||?,1,1,'electronics',?,'')", (lid, title, end, lid, desc))
    conn.commit()
    got = pg.repair_candidates(conn, NOW)
    assert [c["listing_id"] for c in got] == ["1"] and got[0]["kind"] == "phone"
