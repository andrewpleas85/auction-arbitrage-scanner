import json
import time
from datetime import datetime, timedelta, timezone

import pytest

import db
import free_comp_fillers as ff
import lot_sources as ls

NOW = datetime(2026, 11, 11, tzinfo=timezone.utc)
FUTURE = (NOW + timedelta(days=1)).isoformat()
RECEIVER = "Denon AVR-X1700H receiver"


@pytest.fixture
def conn(tmp_path):
    c = db.connect(str(tmp_path / "t.db"))
    for i, p in enumerate([300, 310, 290, 305, 295, 300, 298]):
        c.execute("INSERT OR IGNORE INTO sold_sales (provider, query, title, price, sold_date, fetched_at) VALUES (?,?,?,?,?,?)",
                  ("s", "q", f"Denon AVR-X1700H 7.2 Ch Receiver #{i}", p, (NOW - timedelta(days=10 + i)).isoformat(), int(time.time())))
    c.commit()
    return c


def load(conn, tmp_path, sales, leads=None):
    p = tmp_path / "in.json"
    p.write_text(json.dumps({"sales": sales, "leads": leads or []}))
    return ls.import_file(conn, p)


def sale(source="hibid", sale_id="S1", premium=None, lots=None, **kw):
    s = {"source": source, "sale_id": sale_id, "title": "Estate auction", "url": "https://example.test/s", "ends": FUTURE, "pickup_only": True, "distance_miles": 15, "lots": lots or []}
    if premium is not None:
        s["buyer_premium"] = premium
        s["premium_source"] = "terms text: 'a 15% buyer's premium applies'"
    return {**s, **kw}


def lot(title=RECEIVER, desc="Denon AVR-X1700H 7.2 channel receiver. Works great, tested.", bid=20, lot_id="1", **kw):
    return {"lot_id": lot_id, "title": title, "description": desc, "current_bid": bid, "url": "https://example.test/l", **kw}


def run(conn, source_sale):
    lot_, sale_ = ls.lot_rows(conn, NOW)[0]
    return ls.assess_lot(conn, lot_, sale_, NOW)


def test_premium_is_read_per_sale_and_changes_the_cost(conn, tmp_path):
    load(conn, tmp_path, [sale(sale_id="LOW", premium="10%", lots=[lot()]), sale(sale_id="HIGH", premium=18, lots=[lot()])])
    got = {s["sale_id"]: ls.assess_lot(conn, l, s, NOW) for l, s in ls.lot_rows(conn, NOW)}
    assert got["LOW"]["evidence"]["terms"]["premium"] == 0.10 and got["HIGH"]["evidence"]["terms"]["premium"] == 0.18   # parsed from each sale, not a source constant
    assert got["HIGH"]["evidence"]["all_in"] > got["LOW"]["evidence"]["all_in"] and got["LOW"]["evidence"]["net"] > got["HIGH"]["evidence"]["net"]
    assert got["LOW"]["status"] == "verified" and "premium" in got["LOW"]["evidence"]["terms"]["premium_source"].lower()


def test_a_sale_with_no_premium_in_its_terms_cannot_be_costed(conn, tmp_path):
    load(conn, tmp_path, [sale(sale_id="NOPREM", lots=[lot()])])
    r = run(conn, None)
    assert r["status"] == "unverified" and r["failed"] == "buyer's premium" and "varies per sale" in r["failures"][0]["why"] and "evidence" in r
    assert "net" not in r["evidence"]                                                                        # nothing was priced with a guessed premium


@pytest.mark.parametrize("source", ["hibid", "ctbids", "illinois_ibid"])
def test_per_sale_premium_is_required_for_every_estate_and_state_source(conn, tmp_path, source):
    load(conn, tmp_path, [sale(source=source, lots=[lot()])])
    assert run(conn, None)["failed"] == "buyer's premium"


def test_cws_treasury_has_a_stated_zero_premium_zero_tax_profile_and_buyer_arranged_shipping(conn, tmp_path):
    load(conn, tmp_path, [sale(source="cws_treasury", sale_id="T1", pickup_only=False, ships=True, lots=[lot()])])
    r = run(conn, None)
    t = r["evidence"]["terms"]
    assert t["premium"] == 0.0 and t["tax"] == 0.0 and "Treasury terms" in t["premium_source"] and not t["pickup_only"]
    assert r["evidence"]["inbound_basis"].startswith("ESTIMATE")
    assert r["evidence"]["all_in"] == pytest.approx(r["evidence"]["price_assumed"] + ls.INBOUND_DEFAULT, abs=0.01)   # premium 0, tax 0: price + shipping only
    load(conn, tmp_path, [sale(source="cws_treasury", sale_id="T2", premium="5%", pickup_only=False, ships=True, lots=[lot(lot_id="2")])])  # a sale record overrides the stated default
    assert {s["sale_id"]: ls.sale_terms(ls.PROFILES["cws_treasury"], s)["premium"] for _, s in ls.lot_rows(conn, NOW)} == {"T1": 0.0, "T2": 0.05}


def test_pickup_only_far_away_is_marked_and_fails_the_distance_rule_unless_a_shipper_exists(conn, tmp_path):
    load(conn, tmp_path, [sale(source="illinois_ibid", sale_id="IL", premium="0%", distance_miles=200, lots=[lot()])])
    r = run(conn, None)
    assert r["pickup_only"] and r["distance_miles"] == 200 and "pickup distance" in [f["check"] for f in r["failures"]]
    load(conn, tmp_path, [sale(source="illinois_ibid", sale_id="IL2", premium="0%", distance_miles=200, pickup_only=False, ships=True, lots=[lot(lot_id="2")])])
    r2 = [ls.assess_lot(conn, l, s, NOW) for l, s in ls.lot_rows(conn, NOW) if s["sale_id"] == "IL2"][0]
    assert not r2["pickup_only"] and "pickup distance" not in [f["check"] for f in r2["failures"]]


def test_jewelry_with_karat_and_grams_feeds_the_melt_model(conn, tmp_path, monkeypatch):
    monkeypatch.setattr(ff, "spot_price_gram_24k", lambda metal="gold", http=None: (133.0, "test spot"))
    load(conn, tmp_path, [sale(source="ctbids", sale_id="C1", premium="15%", lots=[lot("14k yellow gold chain necklace", "14k yellow gold chain, 20 grams", bid=1, weight_g=20, karat=14)])])
    r = run(conn, None)
    ev = r["evidence"]
    assert ev["play"] == "melt" and ev["melt"]["weight_grams"] == 20 and ev["melt"]["purity_pct"] == pytest.approx(58.33, abs=0.01)
    assert ev["value"] == pytest.approx(20 * 14 / 24 * 133 * 0.85, abs=0.5) and ev["max_bid"] > 0
    assert r["status"] == "unverified" and r["failed"] == "profit bar"                                       # at the expected close a 15% premium + tax eats the margin


def test_stones_in_the_weight_make_melt_an_upper_bound_only(conn, tmp_path, monkeypatch):
    monkeypatch.setattr(ff, "spot_price_gram_24k", lambda metal="gold", http=None: (133.0, "test spot"))
    load(conn, tmp_path, [sale(source="cws_treasury", sale_id="C2", premium="0%", lots=[lot("14k gold ring with diamond", "14k gold ring with diamond, 12 grams", bid=1, weight_g=12, karat=14)])])
    r = run(conn, None)
    assert "stones in the weight" in [f["check"] for f in r["failures"]]


def test_discovery_sources_are_never_priced_but_leads_are_listed(conn, tmp_path):
    res = load(conn, tmp_path, [{"source": "estatesales", "sale_id": "E1", "lots": [lot()]}],
               leads=[{"source": "estatesales", "lead_id": "L1", "title": "Oak Park estate sale", "location": "Oak Park, IL", "distance_miles": 9, "dates": "2026-11-14"}])
    assert res["sales"] == 0 and res["leads"] == 1 and res["rejected"] and "discovery-only" in res["rejected"][0][1]
    pay = ls.page_payload(conn, NOW)
    assert pay["leads"][0]["priced"] is False and pay["leads"][0]["label"] == "EstateSales.net" and pay["verified"] == [] and pay["unverified"] == []


def test_designer_bags_from_these_sources_go_through_the_authenticity_gate(conn, tmp_path):
    load(conn, tmp_path, [sale(source="cws_treasury", sale_id="B1", premium="0%", pickup_only=False, ships=True,
                               lots=[lot("Louis Vuitton Speedy 30 Monogram Handbag", "Nice bag.", bid=10)])])
    r = run(conn, None)
    assert r["failed"] == "authenticity" and r["status"] == "unverified"


def test_rate_parsing():
    assert ls.parse_rate("15%") == 0.15 and ls.parse_rate(15) == 0.15 and ls.parse_rate(0.15) == 0.15 and ls.parse_rate("") is None


def test_page_renders_the_other_sources_section_with_leads(conn, tmp_path):
    import webpage
    load(conn, tmp_path, [sale(sale_id="P1", premium="12%", lots=[lot()])], leads=[{"source": "estatesales", "lead_id": "L1", "title": "Evanston estate sale", "dates": "2026-11-14"}])
    html = webpage.render([], ext=ls.page_payload(conn, NOW))
    assert 'id="extbox"' in html and "buyer's premium read per sale" in html and "renderExt()" in html and "Evanston estate sale" in html


def test_sample_fixture_loads_and_is_costed_per_sale(conn):
    res = ls.import_file(conn, "tests/fixtures/lots_sample.json")
    assert res == {"sales": 2, "lots": 2, "leads": 1, "rejected": []}
    terms = {s["sale_id"]: ls.sale_terms(ls.PROFILES[s["source"]], s) for _, s in ls.lot_rows(conn, datetime(2026, 11, 11, tzinfo=timezone.utc))}
    assert terms["H-1001"]["premium"] == 0.15 and terms["H-1001"]["tax"] == 0.1025 and terms["T-77"]["premium"] == 0.0 and terms["T-77"]["tax"] == 0.0


def test_discovery_leads_carry_address_company_and_a_photos_link_and_are_never_priced(conn, tmp_path):
    load(conn, tmp_path, [], leads=[{"source": "estatesales", "lead_id": "NAP-1", "title": "Naperville estate sale", "address": "123 Main St, Naperville, IL 60540", "company": "Acme Estate Sales",
                                     "photos": "https://www.estatesales.net/IL/Naperville/60540/1", "dates": "2026-11-14 to 2026-11-16", "distance_miles": 30, "notes": "jewelry, designer bags"}])
    lead = ls.leads(conn)[0]
    assert lead["address"].startswith("123 Main") and lead["company"] == "Acme Estate Sales" and lead["photos_url"].endswith("/1") and lead["priced"] is False
    import webpage
    html = webpage.render([], ext=ls.page_payload(conn, NOW))
    assert "Naperville estate sale" in html and "Acme Estate Sales" in html and "<th>Photos</th>" in html


def test_every_source_has_a_status_and_manual_only_sources_say_what_blocks_automation(conn):
    st = {s["source"]: s for s in ls.source_status(conn)}
    assert st["illinois_ibid"]["status"] == ls.AUTOMATED and st["illinois_ibid"]["lots"] == 0
    assert st["facebook_marketplace"]["status"] == ls.INTAKE
    for key in ("cws_treasury", "hibid", "ctbids", "estatesales", "auctionzip", "whatnot"):
        assert st[key]["status"] == ls.MANUAL_ONLY and len(st[key]["blocker"]) > 40
    assert "403" in st["hibid"]["blocker"] and "403" in st["cws_treasury"]["blocker"] and "$2,500" in st["cws_treasury"]["blocker"]
    assert "Terms of Service" in st["estatesales"]["blocker"] and "live" in st["whatnot"]["blocker"]


def test_an_automated_source_shows_what_the_last_crawl_stored(conn, tmp_path):
    load(conn, tmp_path, [sale(source="illinois_ibid", sale_id="IL", premium="2%", lots=[lot()])])
    row = [s for s in ls.source_status(conn) if s["source"] == "illinois_ibid"][0]
    assert row["lots"] == 1 and row["last_fetched"]


def test_blocked_sources_wait_for_muses_independent_attempt_and_the_status_follows_it(conn, tmp_path):
    st = lambda: {s["source"]: s for s in ls.source_status(conn)}
    assert st()["hibid"]["effective_status"] == ls.BLOCKED_PENDING and st()["hibid"]["muse_attempt"]["result"] == "pending"
    assert st()["illinois_ibid"]["effective_status"] == ls.AUTOMATED and st()["facebook_marketplace"]["effective_status"] == ls.COVERED_BY_MUSE
    ls.record_attempt(conn, "hibid", "muse", True, "catalog readable in Muse's browser; exported 40 lots")
    assert st()["hibid"]["effective_status"] == ls.COVERED_BY_MUSE and "40 lots" in st()["hibid"]["muse_attempt"]["note"]
    ls.record_attempt(conn, "cws_treasury", "muse", False, "needs the $2,500 authorization just to view")
    assert st()["cws_treasury"]["effective_status"] == ls.MANUAL_ONLY                      # both attempts failed: manual-only stands, both on record
    assert st()["cws_treasury"]["claude_attempt"]["blocker_type"].startswith("bot block") and "$2,500" in st()["cws_treasury"]["muse_attempt"]["note"]
    ls.record_attempt(conn, "cws_treasury", "muse", True, "reached it after all")           # the latest attempt wins
    assert st()["cws_treasury"]["effective_status"] == ls.COVERED_BY_MUSE
    with pytest.raises(ValueError):
        ls.record_attempt(conn, "nowhere", "muse", True)


def test_lots_from_blocked_sources_are_labelled_by_who_supplied_them(conn, tmp_path):
    load(conn, tmp_path, [sale(source="hibid", sale_id="H", premium="15%", lots=[lot()])])
    r = run(conn, None)
    assert r["supply"] == "muse_file" and r["manual_only"] is False and r["source_status"] == ls.BLOCKED_PENDING
    ls.record_attempt(conn, "hibid", "muse", False, "also blocked")
    r2 = run(conn, None)
    assert r2["manual_only"] is True and r2["source_status"] == ls.MANUAL_ONLY
    load(conn, tmp_path, [sale(source="illinois_ibid", sale_id="IL", premium="2%", lots=[lot(lot_id="9")])])
    claude = [ls.assess_lot(conn, l, s, NOW) for l, s in ls.lot_rows(conn, NOW) if s["source"] == "illinois_ibid"][0]
    assert claude["supply"] == "claude" and claude["manual_only"] is False
    import webpage
    html = webpage.render([], ext=ls.page_payload(conn, NOW))
    assert "supplied by file (Muse)" in html and "Muse (browser / connected accounts)" in html and "blocker_type" in html


def test_page_shows_the_source_table_even_with_no_lots(conn):
    import webpage
    html = webpage.render([], ext=ls.page_payload(conn, NOW))
    assert 'id="extst"' in html and "AUTOMATED" in html
