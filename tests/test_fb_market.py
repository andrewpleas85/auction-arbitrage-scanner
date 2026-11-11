import json
import time
from datetime import datetime, timedelta, timezone

import pytest

import catalog
import db
import fb_market as fb
import repair
import webpage

NOW = datetime(2026, 11, 11, tzinfo=timezone.utc)
FIXTURE = "tests/fixtures/fb_sample.json"


@pytest.fixture
def conn(tmp_path):
    c = db.connect(str(tmp_path / "t.db"))
    fb.ensure_schema(c)
    return c


def seed_sales(conn, title, prices, days_ago=10):
    for i, p in enumerate(prices):
        d = (NOW - timedelta(days=days_ago + i)).isoformat()
        conn.execute("INSERT OR IGNORE INTO sold_sales (provider, query, title, price, sold_date, fetched_at) VALUES (?,?,?,?,?,?)",
                     ("soldcomps", "q", f"{title} #{i}", p, d, int(time.time())))
    conn.commit()


def add(conn, **kw):
    rec = {"listing_id": "1", "title": "Denon AVR-X1700H receiver", "description": "Denon AVR-X1700H 7.2 channel receiver. Works great, tested.", "asking_price": 100,
           "condition": "Used - Good", "location": "Chicago, IL", "distance_miles": 5, "seller_id": "s", "product_url": "https://www.facebook.com/marketplace/item/1",
           "creation_date": "2026-11-10", **kw}
    path = conn_path(conn, rec)
    return fb.import_file(conn, path)


def conn_path(conn, rec):
    import tempfile
    f = tempfile.NamedTemporaryFile("w", suffix=".json", delete=False)
    json.dump([rec], f)
    f.close()
    return f.name


def assess_one(conn, lid="1"):
    row = conn.execute("SELECT * FROM fb_listings WHERE listing_id=?", (lid,)).fetchone()
    return fb.assess(conn, row, NOW)


# ------------------------------------------------------------------ importer
def test_imports_the_sample_file_and_keeps_every_field(conn):
    res = fb.import_file(conn, FIXTURE)
    assert res == {"imported": 4, "updated": 0, "rejected": []}
    r = conn.execute("SELECT * FROM fb_listings WHERE listing_id='2600250133750603'").fetchone()
    assert (r["title"], r["asking_price"], r["location"], r["distance_miles"], r["seller_id"]) == ("Marantz SR6008 receiver", 100.0, "Bellwood, IL", 12.0, "s-2")
    assert r["product_url"].startswith("https://www.facebook.com/marketplace/item/") and r["creation_date"] == "2026-11-10"


def test_reimport_updates_instead_of_duplicating(conn):
    fb.import_file(conn, FIXTURE)
    first = conn.execute("SELECT first_seen FROM fb_listings WHERE listing_id='2600250133750603'").fetchone()[0]
    res = add(conn, listing_id="2600250133750603", title="Marantz SR6008 receiver", asking_price="$80")
    assert res["updated"] == 1 and res["imported"] == 0
    r = conn.execute("SELECT asking_price, first_seen FROM fb_listings WHERE listing_id='2600250133750603'").fetchone()
    assert r["asking_price"] == 80.0 and r["first_seen"] == first
    assert conn.execute("SELECT COUNT(*) FROM fb_listings").fetchone()[0] == 4


def test_csv_with_alias_headers_and_bad_rows_are_reported(conn, tmp_path):
    p = tmp_path / "x.csv"
    p.write_text("id,name,price,url,distance\n"
                 "7,Sony PS5,\"$1,250\",https://www.facebook.com/marketplace/item/7,3\n"
                 "8,No url,50,,2\n"
                 "9,Bad price,abc,https://www.facebook.com/marketplace/item/9,2\n"
                 "10,Wrong site,20,https://example.com/x,2\n")
    res = fb.import_file(conn, p)
    assert res["imported"] == 1 and len(res["rejected"]) == 3
    assert conn.execute("SELECT asking_price, distance_miles FROM fb_listings WHERE listing_id='7'").fetchone()[:] == (1250.0, 3.0)


def test_parse_price():
    assert fb.parse_price("$1,250") == 1250.0 and fb.parse_price(99) == 99.0 and fb.parse_price("Free") == 0.0 and fb.parse_price("call") is None


# ------------------------------------------------------------------ cost model
def test_facebook_cost_model_has_no_premium_tax_climb_or_shipping_in(conn):
    ch = fb.channels(400.0, "audio", "Denon AVR-X1700H receiver", 100.0)
    terms = catalog.ebay_fee_terms({"kind": "audio", "title": ""})
    fee, order = catalog.fees_for(dict(terms), 400.0)
    ship = fb.ship_out_estimate("audio", "Denon AVR-X1700H receiver")
    want = 400.0 * (1 - fee - catalog.RETURN_RESERVE) - order - ship - 100.0  # buy cost is the asking price and nothing else
    assert ch["eBay shipped"]["net"] == pytest.approx(want, abs=0.01)
    assert ch["local cash"]["net"] == pytest.approx(400.0 * fb.LOCAL_FACTOR - 100.0, abs=0.01)
    assert ch["local cash"]["costs"] == {"fee_pct": 0.0, "returns_pct": 0.0, "order_fee": 0.0, "ship_out": 0.0}


def test_max_buy_hits_both_bars_exactly():
    for rev in (150.0, 305.0, 1000.0):
        m = fb.max_buy(rev)
        net = rev - m
        assert net >= fb.MIN_NET - 0.01 and net / m >= fb.MIN_RETURN - 1e-6
        assert abs(net - fb.MIN_NET) < 0.01 or abs(net / m - fb.MIN_RETURN) < 1e-6  # one of the two bars binds


# ------------------------------------------------------------------ the gate
def test_verified_when_every_check_passes(conn):
    seed_sales(conn, "Denon AVR-X1700H 7.2 Ch Receiver", [400, 420, 380, 410, 395, 405])
    add(conn)
    r = assess_one(conn)
    assert r["status"] == "verified" and r["failed"] is None and r["failures"] == []
    ev = r["evidence"]
    assert ev["comps"] == 6 and ev["sold_median"] == pytest.approx(402.5) and ev["demand"]["sold_90d"] == 6
    assert ev["best_channel"] == "eBay shipped" and ev["net"] >= fb.MIN_NET and ev["return"] >= fb.MIN_RETURN
    assert 0 < ev["max_buy"] and ev["demand"]["sold_per_30d"] == 2.0 and ev["demand"]["time_to_sell"].startswith("unknown")  # velocity shown, time to sell NOT invented
    assert ev["value_tier"]["tier"].startswith("25th percentile") and ev["value_tier"]["value"] < ev["sold_median"]


def test_rejected_for_thin_comps(conn):
    seed_sales(conn, "Denon AVR-X1700H 7.2 Ch Receiver", [400, 420, 380, 410])  # 4 < 5
    add(conn)
    r = assess_one(conn)
    assert r["status"] == "unverified" and r["failed"] == "sold comps" and r["needs_check"]
    assert "4 matching sold sale" in r["failures"][0]["why"]


def test_asking_price_only_evidence_is_rejected(conn):
    # Other Marketplace/active asking prices exist, but with no SOLD sales stored nothing may be valued from them
    add(conn, listing_id="2", title="Denon AVR-X1700H receiver", asking_price=450, product_url="https://www.facebook.com/marketplace/item/2")
    add(conn, listing_id="3", title="Denon AVR-X1700H receiver", asking_price=500, product_url="https://www.facebook.com/marketplace/item/3")
    r = assess_one(conn, "2")
    assert r["status"] == "unverified" and r["failed"] == "sold comps"
    assert "sold_median" not in r["evidence"] and r["evidence"]["comps"] == 0 and "channels" not in r["evidence"]


def test_untested_item_is_priced_at_parts_value_never_as_working(conn):
    seed_sales(conn, "Denon AVR-X1700H 7.2 Ch Receiver", [400, 420, 380, 410, 395, 405])
    add(conn, description="Denon AVR-X1700H. No power cable, untested for a year.", asking_price=100)
    r = assess_one(conn)
    ev = r["evidence"]
    assert r["status"] == "unverified" and r["failed"] == "condition"
    assert ev["condition"]["class"] == "untested_unknown"
    assert ev["as_is_value"] == pytest.approx(ev["working_value"] * repair.PARTS_FACTOR, abs=0.01)  # parts value, not the ~$400 working price
    assert ev["as_is_value"] < ev["working_value"] / 2 and ev["play"] is None
    assert ev["prob_weighted_value"] == pytest.approx(0.5 * ev["working_value"] + 0.5 * ev["as_is_value"], abs=0.01)  # shown, never used to pass
    assert ev["net"] < 0  # at parts value a $100 ask loses money: it was never priced as a working receiver
    assert "PARTS value" in r["failures"][0]["why"]


def test_stale_sales_fail_the_demand_check(conn):
    seed_sales(conn, "Denon AVR-X1700H 7.2 Ch Receiver", [400, 420, 380, 410, 395, 405], days_ago=200)
    add(conn)
    r = assess_one(conn)
    assert r["status"] == "unverified" and r["failed"] == "demand"


def test_profit_bar_net_dollars(conn):
    seed_sales(conn, "Denon AVR-X1700H 7.2 Ch Receiver", [130, 135, 125, 140, 128, 132])  # ~$132: little left after fees and $40 freight
    add(conn, asking_price=20)
    r = assess_one(conn)
    assert r["status"] == "unverified" and r["failed"] == "profit bar" and "minimum" in r["failures"][0]["why"]


def test_profit_bar_return_on_cost(conn):
    seed_sales(conn, "Denon AVR-X1700H 7.2 Ch Receiver", [400, 420, 380, 410, 395, 405])
    add(conn, asking_price=330)  # nets well over $75 on a 400 sale? only about 75: return is far under 50%
    r = assess_one(conn)
    assert r["status"] == "unverified" and any("return" in f["why"] for f in r["failures"])


def test_multi_item_lots_are_never_verified(conn):
    seed_sales(conn, "Denon AVR-X1700H 7.2 Ch Receiver", [400, 420, 380, 410, 395, 405])
    add(conn, title="Denon AVR-X1700H receiver + Sony CD changer bundle")
    r = assess_one(conn)
    assert r["status"] == "unverified" and r["failed"] == "multi-item lot"


def test_spec_conflicting_sales_do_not_count(conn):
    seed_sales(conn, "Apple MacBook Pro 14 M2 16GB 512GB", [1100, 1120, 1080, 1150, 1090, 1110])
    add(conn, title="Apple MacBook Pro 14 M1 16GB 512GB", asking_price=500, description="Apple MacBook Pro 14 M1 16GB 512GB. Works.")
    r = assess_one(conn)
    assert r["status"] == "unverified" and r["failed"] == "sold comps" and r["evidence"]["comps"] == 0


def test_local_cash_is_information_only_by_default(conn):
    seed_sales(conn, "Denon AVR-X1700H 7.2 Ch Receiver", [400, 420, 380, 410, 395, 405])
    add(conn)
    ev = assess_one(conn)["evidence"]
    assert ev["best_channel"] == "eBay shipped" and "local cash" in ev["local_note"]
    assert ev["channels"]["local cash"]["net"] != ev["net"]


# ------------------------------------------------------------------ page
def test_page_payload_keeps_verified_and_unverified_apart_and_renders(conn):
    seed_sales(conn, "Denon AVR-X1700H 7.2 Ch Receiver", [400, 420, 380, 410, 395, 405])
    add(conn)
    add(conn, listing_id="2", title="Marantz SR6008 receiver", product_url="https://www.facebook.com/marketplace/item/2")
    pay = fb.page_payload(conn, NOW)
    assert [r["listing_id"] for r in pay["verified"]] == ["1"] and [r["listing_id"] for r in pay["unverified"]] == ["2"]
    assert pay["unverified"][0]["failed"] == "identity"  # the Marantz listing's description does not corroborate the model
    html = webpage.render([], fb=pay)
    assert 'id="fbbox"' in html and "Verified profitable" in html and "needs pricing check" in html and "renderFb()" in html
    assert "facebook.com/marketplace/item/2" in html


# ------------------------------------------------------------------ identity: claimed vs actual
def seed_denon(conn):
    seed_sales(conn, "Denon AVR-X1700H 7.2 Ch Receiver", [400, 420, 380, 410, 395, 405])


def test_title_model_contradicted_by_description_is_never_verified_even_with_comps(conn):
    seed_denon(conn)
    seed_sales(conn, "Marantz SR7002 Receiver", [200, 210, 190, 205, 195, 200])
    add(conn, title="Marantz SR7002 receiver", description="Great receiver. This is actually the SR6008, works fine.")
    r = assess_one(conn)
    assert r["status"] == "unverified" and r["failed"] == "identity"
    assert "SR6008" in r["failures"][0]["why"] and r["evidence"]["identity"]["confidence"] == "low"


def test_description_naming_another_chip_or_storage_is_a_contradiction(conn):
    seed_sales(conn, "Apple MacBook Pro 14 M1 16GB 512GB", [1100, 1120, 1080, 1150, 1090, 1110])
    add(conn, title="Apple MacBook Pro 14 M1 16GB 512GB", asking_price=300, description="M2 chip, 256GB SSD, battery 90%")
    r = assess_one(conn)
    assert r["failed"] == "identity" and "chip" in r["failures"][0]["why"] and "storage" in r["failures"][0]["why"]


def test_unverifiable_and_uncorroborated_identity_go_to_unverified_with_a_next_step(conn):
    seed_denon(conn)
    add(conn, title="Denon AVR-X1700H receiver", description="Nice receiver, works.")
    r = assess_one(conn)
    assert r["failed"] == "identity" and r["evidence"]["identity"]["confidence"] == "medium" and "rear label" in r["needs_check"]
    add(conn, listing_id="5", title="Denon receiver", description="Nice receiver, works.", product_url="https://www.facebook.com/marketplace/item/5")
    r5 = assess_one(conn, "5")
    assert r5["status"] == "unverified" and r5["failed"] in ("model not identified", "identity")


def test_model_number_field_corroborates_the_title(conn):
    seed_denon(conn)
    add(conn, title="Denon AVR-X1700H receiver", description="Nice receiver, works.", model_number="AVR-X1700H")
    r = assess_one(conn)
    assert r["status"] == "verified" and r["evidence"]["identity"]["confidence"] == "high"


def test_spec_field_contradicting_the_title_is_flagged(conn):
    seed_sales(conn, "Apple MacBook Pro 14 M1 16GB 512GB", [1100, 1120, 1080, 1150, 1090, 1110])
    add(conn, title="Apple MacBook Pro 14 M1 16GB 512GB", asking_price=300, description="Apple MacBook Pro 14 M1.", specs={"storage_gb": 256})
    r = assess_one(conn)
    assert r["failed"] == "identity" and "spec field says 256" in r["failures"][0]["why"]


def test_bundle_contents_hidden_in_the_description_are_named(conn):
    seed_denon(conn)
    add(conn, description="Denon AVR-X1700H receiver. Also includes a Sony CD changer CDP-CE375 and Sherwood RX-4103.")
    r = assess_one(conn)
    assert r["failed"] == "identity" and "bundle contents" in r["failures"][0]["why"]


def test_for_parts_buried_in_the_description_is_caught_even_with_a_clean_title(conn):
    seed_denon(conn)
    add(conn, description="Denon AVR-X1700H receiver. Sold for parts only, no sound.")
    r = assess_one(conn)
    ev = r["evidence"]
    assert ev["condition"]["class"] == "parts_only" and r["status"] == "unverified"
    assert any("only in the description" in x for x in ev["identity"]["risks"])


def test_comps_for_another_lens_mount_do_not_match(conn):
    seed_sales(conn, "Sigma 35mm f/1.4 DG HSM Art Lens for Canon EF", [420, 430, 410, 440, 415, 425])
    add(conn, title="Sigma 35mm f/1.4 DG HSM Art Lens for Sony E-mount", description="Sigma 35mm f/1.4 DG HSM Art Lens for Sony E-mount, mint.", asking_price=150)
    r = assess_one(conn)
    assert r["evidence"]["comps"] == 0 and r["failed"] == "sold comps"


# ------------------------------------------------------------------ repair path
def seed_iphone(conn, price=450):
    seed_sales(conn, "Apple iPhone 13 128GB Unlocked", [price, price + 20, price - 20, price + 10, price - 10, price + 5])


def phone(conn, ask, desc):
    add(conn, title="Apple iPhone 13 128GB unlocked", description=f"Apple iPhone 13 128GB unlocked. {desc}", asking_price=ask)
    return assess_one(conn)


def test_cracked_screen_phone_clears_the_gate_after_repair_costs(conn):
    seed_iphone(conn)
    r = phone(conn, 120, "Cracked screen, otherwise works perfectly.")
    ev = r["evidence"]
    assert r["status"] == "verified" and ev["play"] == "repair" and ev["condition"]["class"] == "repairable"
    rp, asis = ev["plays"]["repair"], ev["plays"]["as-is"]
    assert rp["repair_cost"] == 110.0 and "ESTIMATE" in rp["repair_source"]
    assert rp["net"] >= fb.MIN_NET + repair.REPAIR_CUSHION and rp["return"] >= fb.MIN_RETURN and rp["all_in_cost"] == 230.0
    assert asis["net"] < 0 and not asis["passes"]                     # both numbers are on the row: as-is loses money, repaired wins
    assert ev["repaired_value"] > ev["as_is_value"] * 2
    assert ev["demand"]["repair_days_included"] == repair.REPAIR_DAYS  # repair time is added to the time to sell


def test_repair_play_needs_the_extra_cushion(conn):
    seed_sales(conn, "Apple iPhone 13 128GB Unlocked", [250, 260, 240, 255, 245, 250])
    add(conn, title="Apple iPhone 13 128GB unlocked", description="Apple iPhone 13 128GB unlocked. Bad battery, needs a new battery.", asking_price=35)
    r = assess_one(conn)
    rp = r["evidence"]["plays"]["repair"]
    assert fb.MIN_NET <= rp["net"] < fb.MIN_NET + repair.REPAIR_CUSHION and rp["return"] >= fb.MIN_RETURN  # would pass a normal $75 bar
    assert r["status"] == "unverified" and r["failed"] == "repair economics" and "cushion" in r["failures"][0]["why"]


def test_damage_with_a_second_unknown_fault_is_not_a_repair_play(conn):
    seed_iphone(conn)
    r = phone(conn, 120, "Cracked screen and it won't turn on.")
    assert r["evidence"]["condition"]["class"] == "untested_unknown" and r["evidence"]["play"] is None and r["status"] == "unverified"


def test_cosmetic_wear_takes_a_haircut_but_stays_a_working_unit(conn):
    seed_denon(conn)
    add(conn, description="Denon AVR-X1700H receiver. Light scratches on the top panel, works perfectly.", asking_price=60)
    ev = assess_one(conn)["evidence"]
    assert ev["condition"]["class"] == "cosmetic" and ev["as_is_value"] == pytest.approx(ev["working_value"] * repair.COSMETIC_FACTOR, abs=0.01)


def test_muses_first_json_batch_runs_through_the_whole_pipeline(conn):
    res = fb.import_file(conn, "tests/fixtures/fb_muse_batch1.json")          # Muse's keys: price, source_url, created_at, category
    assert res == {"imported": 4, "updated": 0, "rejected": []}
    got = {r["listing_id"]: r for r in fb.assess_all(conn, NOW)}
    assert all(r["status"] == "unverified" for r in got.values())
    r1, r2, r3, r4 = (got[k] for k in ("1674700490849000", "2600250133750603", "1483441930349655", "1104690612166468"))
    assert r1["failed"] == "identity" and {f["check"] for f in r1["failures"]} == {"identity", "sold comps", "demand", "condition"}
    assert r1["evidence"]["condition"]["class"] == "untested_unknown"
    assert r2["failed"] == "identity" and r2["evidence"]["identity"]["confidence"] == "medium"
    assert r3["failed"] == "asking price" and {f["check"] for f in r3["failures"]} == {"asking price", "multi-item lot"}
    assert r4["failed"] == "multi-item lot"
    assert conn.execute("SELECT product_url FROM fb_listings WHERE listing_id='2600250133750603'").fetchone()[0].endswith("2600250133750603")
