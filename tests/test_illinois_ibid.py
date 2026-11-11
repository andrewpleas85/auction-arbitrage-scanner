import asyncio
from datetime import datetime, timedelta, timezone

import httpx
import pytest

import db
import free_comp_fillers as ff
import lot_sources as ls
from clients import illinois_ibid as ib

FX = "tests/fixtures/"
NOW = datetime(2026, 11, 11, tzinfo=timezone.utc)


def fx(name):
    return open(FX + name).read()


def test_browse_page_parses_real_rows():
    rows = ib.parse_browse(fx("ibid_browse_misc.html"))
    assert len(rows) == 5 and rows[0] == {"id": "418818", "lot_label": "418818", "title": "MARKET FORGE STERILMATIC (I0320003)", "current_bid": 27.0, "bids": 6, "ends_in": "2h 3m"}
    assert rows[1]["title"] == "MARATHON CENTRIFUGE 21K/BR (I0320003)" and rows[1]["current_bid"] == 8.02


def test_item_page_parses_location_end_time_in_utc_and_bids():
    it = ib.parse_item(fx("ibid_item_lab.html"))
    assert it["lot"] == "6350" and it["category"] == "All > Lab Equipment" and it["seller"] == "CMS iBid" and "Springfield, IL 62703" in it["seller_location"]
    assert it["ends"] == "2026-11-14T01:02:00+00:00"           # "Nov 13, 2026 - 07:02 PM" Springfield time = 01:02 UTC the next day
    assert (it["bids"], it["current_bid"], it["shipping_fee"]) == (11, 101.0, 0.0) and it["description"].startswith("PALLET OF MISC LAB TEST EQUIPMENT")


def test_the_buyers_fee_and_the_no_sales_tax_clause_are_read_from_the_sites_own_pages():
    assert ib.parse_buyer_fee(fx("ibid_fees.html")) == 0.02                    # the 'Final Price Fee' rows are SELLER fees and are not read as the buyer's
    assert ib.parse_no_sales_tax(fx("ibid_terms_clause.html")) is True and ib.parse_no_sales_tax("<p>Tax applies</p>") is False


def test_karat_and_gram_weights_are_parsed_into_melt_inputs():
    assert ib.parse_weight_karat("14K YELLOW GOLD CHAIN 12.4 GRAMS") == (12.4, 14)
    assert ib.parse_weight_karat("18KT RING", "weighs 3 dwt") == (pytest.approx(4.666, abs=0.001), 18)
    assert ib.parse_weight_karat("1.5 ozt .999 silver round") == (pytest.approx(46.655, abs=0.001), None)
    assert ib.parse_weight_karat("sterling spoon") == (None, None)


def test_robots_disallow_lines_are_honoured():
    assert ib.disallowed("User-agent: *\nDisallow: /admin/\nDisallow: /js/\n# x\nUser-agent: other\nDisallow: /all/") == ["/admin/", "/js/"]


def jewelry_item_page():
    """SYNTHETIC: the real item-page template with jewelry text swapped in (the jewelry category had no lots when the fixtures were captured)."""
    page = fx("ibid_item_lab.html")
    page = page.replace("PALLET OF MISC LAB TEST EQUIPMNET &amp; VARIOUS ELECTRONICS", "14K YELLOW GOLD CHAIN NECKLACE 12.4 GRAMS")
    page = page.replace("PALLET OF MISC LAB TEST EQUIPMENT &amp; VARIOUS ELECTRONICS", "14K YELLOW GOLD CHAIN NECKLACE 12.4 GRAMS, no stones")
    return page.replace("All &gt; Lab Equipment", "All &gt; Jewelry &amp; Exotic Collectibles")


def test_build_import_makes_a_sale_with_premium_tax_pickup_and_melt_fields_read_from_the_pages():
    item = {**ib.parse_item(jewelry_item_page()), "id": "999001", "current_bid": 150.0}
    data = ib.build_import(fx("ibid_fees.html"), fx("ibid_terms_clause.html"), [item], "2026-11-11")
    sale = data["sales"][0]
    assert sale["buyer_premium"] == 0.02 and "fees.php" in sale["premium_source"] and sale["sales_tax"] == 0.0 and "item 9" in sale["tax_source"]
    assert sale["pickup_only"] is True and sale["ships"] is False and sale["distance_miles"] == ib.SPRINGFIELD_MILES and "does not ship" in sale["shipping_note"]
    lot = sale["lots"][0]
    assert (lot["weight_g"], lot["karat"]) == (12.4, 14) and lot["url"].endswith("item.php?id=999001")


def test_springfield_pickup_lot_fails_the_chicago_distance_rule_but_is_costed_with_the_read_terms(tmp_path, monkeypatch):
    monkeypatch.setattr(ff, "spot_price_gram_24k", lambda metal="gold", http=None: (133.0, "test spot"))
    conn = db.connect(str(tmp_path / "t.db"))
    item = {**ib.parse_item(jewelry_item_page()), "id": "999001", "current_bid": 50.0, "ends": (NOW + timedelta(days=1)).isoformat()}
    ls.import_data(conn, ib.build_import(fx("ibid_fees.html"), fx("ibid_terms_clause.html"), [item], "2026-11-11"))
    lot, sale = ls.lot_rows(conn, NOW)[0]
    r = ls.assess_lot(conn, lot, sale, NOW)
    t = r["evidence"]["terms"]
    assert t["premium"] == 0.02 and t["tax"] == 0.0 and t["pickup_only"] and t["distance_miles"] == 200.0
    assert "pickup distance" in [f["check"] for f in r["failures"]] and r["status"] == "unverified"
    assert r["evidence"]["melt"]["weight_grams"] == 12.4 and r["evidence"]["play"] == "melt"       # karat/grams reached the melt model


def test_sync_walks_the_site_politely_with_a_mock_transport(tmp_path):
    calls = []

    def handler(req: httpx.Request) -> httpx.Response:
        calls.append(str(req.url))
        path = req.url.path + ("?" + req.url.query.decode() if req.url.query else "")
        if path == "/robots.txt":
            return httpx.Response(200, text="User-agent: *\nDisallow: /admin/\n")
        if path == "/fees.php":
            return httpx.Response(200, text=fx("ibid_fees.html"))
        if path.startswith("/contents.php"):
            return httpx.Response(200, text=fx("ibid_terms_clause.html"))
        if path == "/browse.php?id=561":
            return httpx.Response(200, text=fx("ibid_browse_misc.html"))
        if path.startswith("/browse.php"):
            return httpx.Response(200, text="<html>No items found</html>")
        if path.startswith("/item.php"):
            return httpx.Response(200, text=jewelry_item_page())
        return httpx.Response(404)

    async def go():
        conn = db.connect(str(tmp_path / "t.db"))
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
            return await ib.sync(conn, http, categories=("jewelry", "misc"), delay=0.0, max_items=3), conn

    res, conn = asyncio.run(go())
    assert res["buyer_fee"] == 0.02 and res["no_sales_tax"] and res["listed"] == 5 and res["detail_pages"] == 3 and res["sales"] == 1 and res["lots"] == 3
    assert all("/admin/" not in c for c in calls) and conn.execute("SELECT buyer_premium FROM auction_sales").fetchone()[0] == 0.02


def test_the_html_title_tag_is_ignored_and_a_leading_dash_in_the_title_is_stripped():
    page = ("<html><head><title>Lot # 20713 - 5 TESTED iPhones 13 64 GB State of Illinois Online Auction</title></head><body>"
            "Item Category: All &gt; Computer Equipment and Accessories Lot # 20713 - 5 TESTED iPhones 13 64 GB Auction ID: 418530 "
            "Item Description Seller: **CMS iBid Sellers auctions: View active auctions Seller location: 1924 S. 10 1/2 St., Springfield, IL 62703 * Highest bidder: X "
            "Auction Ends In: 2 days (Nov 13, 2026 - 05:09 PM) # of bids: 4 Current Bid: $ 520.00 Shipping Fee: $ 0.00 Item Description 5 TESTED iPhones 13 64 GB Guidelines x</body></html>")
    it = ib.parse_item(page)
    assert it["title"] == "5 TESTED iPhones 13 64 GB" and it["lot"] == "20713" and it["current_bid"] == 520.0 and it["bids"] == 4 and it["ends"] == "2026-11-13T23:09:00+00:00"


def test_illinois_ibid_is_part_of_the_normal_catalog_run(tmp_path):
    import catalog
    from config import Config
    assert "illinois_ibid" in catalog.ALL_DOMAINS                      # --catalog with no domain list includes it

    def handler(req: httpx.Request) -> httpx.Response:
        path = req.url.path + ("?" + req.url.query.decode() if req.url.query else "")
        if path == "/robots.txt":
            return httpx.Response(200, text="User-agent: *\nDisallow: /admin/\n")
        if path == "/fees.php":
            return httpx.Response(200, text=fx("ibid_fees.html"))
        if path.startswith("/contents.php"):
            return httpx.Response(200, text=fx("ibid_terms_clause.html"))
        if path == "/browse.php?id=561":
            return httpx.Response(200, text=fx("ibid_browse_misc.html"))
        if path.startswith("/browse.php"):
            return httpx.Response(200, text="<html>No items found</html>")
        if path.startswith("/item.php"):
            return httpx.Response(200, text=jewelry_item_page())
        return httpx.Response(404)

    async def go():
        conn = db.connect(str(tmp_path / "t.db"))
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
            orig = ib.sync.__defaults__
            ib.sync.__defaults__ = (orig[0], 0.0, orig[2], orig[3])          # no pause under test
            try:
                stats = await catalog.crawl(conn, http, Config(), domains=["illinois_ibid"])
            finally:
                ib.sync.__defaults__ = orig
        return stats, conn

    stats, conn = asyncio.run(go())
    assert stats["illinois_ibid"] == 5 and stats["illinois_ibid_scanned"] == 5 and "illinois_ibid_error" not in stats
    assert conn.execute("SELECT COUNT(*) FROM auction_lots WHERE source='illinois_ibid'").fetchone()[0] == 5
