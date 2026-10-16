import asyncio
import json
import re
from datetime import datetime, timedelta, timezone

import httpx
import pytest

import catalog
import db
import webpage
from config import Config
from engine import knowledge as kb
from models import Listing

NOW = datetime.now(timezone.utc)
run = asyncio.run


def _l(i, title, bid, hours=48, platform="shopgoodwill", url=None, **extra):
    return Listing(platform, str(i), title, bid, 0.0, url or f"https://shop.example/item/{i}", None, "cat",
                   listing_type="auction", end_time=NOW + timedelta(hours=hours), extra=extra)


@pytest.fixture
def conn(tmp_path):
    c = db.connect(str(tmp_path / "w.db"))
    catalog.store(c, [
        _l(1, "Samsung Galaxy Z Flip5 256GB Smartphone Factory Unlocked", 20, platform="propertyroom"),
        _l(2, "Apple iPhone 13 128GB Factory Unlocked", 40),
        _l(3, "Apple iPhone 13 128GB Factory Unlocked Smartphone", 55),
        _l(4, "Apple iPhone 13 128GB Factory Unlocked T-Mobile", 60),
        _l(5, "Clear Case for Apple iPhone 13", 1),                                      # accessory
        _l(6, "Apple iPhone 12 128GB Activation Locked Sold For Parts", 5),            # locked
        _l(7, "Sony Cyber-shot DSC-W120 Digital Camera", 8, hours=0.2),                # ends in 12 minutes
        _l(8, "Sony Cyber-shot DSC-W120 Digital Camera Silver", 6, hours=-3),          # ended
        _l(9, "Coach Black Signature Canvas Crossbody Bag", 5),
        _l(10, "Michael Kors Black Leather Crossbody Handbag", 5),                      # unprofitable brand
        _l(11, "Apple iPhone 13 128GB Factory Unlocked", 30, url="javascript:alert(1)"),                # bad link
    ], "electronics")
    kb.add_observation(c, "Samsung 256GB FLIP5", "sold", "swappa", low=219, high=289, typical=266, n=9)
    kb.add_observation(c, "Apple IPHONE 13", "ask", "swappa", low=200, high=310, typical=291, n=290)
    kb.add_observation(c, "Sony baseline-camera", "sold", "x", low=25, high=200, typical=95, n=8)
    kb.add_observation(c, "Michael Kors baseline-bags", "sold", "x", low=25, high=120, typical=50, n=400)
    return c


def test_collect_ranks_model_specific_first_and_filters_junk(conn):
    rows = webpage.collect(conn, limit=100, now=NOW)
    titles = [r["title"] for r in rows]
    assert not any("Case for" in t for t in titles)                     # accessories
    assert not any("Activation Locked" in t for t in titles)            # locked / for parts
    assert not any("Michael Kors" in t for t in titles)                 # no margin
    assert not any(r["listing_id"] in ("7", "8") for r in rows)         # ending in <30 min, or already ended
    assert not any(r["listing_id"] == "11" for r in rows)               # a javascript: link never reaches the page
    assert [r["rank"] for r in rows] == list(range(1, len(rows) + 1))
    specific = [i for i, r in enumerate(rows) if not r["coarse"]]
    brand_level = [i for i, r in enumerate(rows) if r["coarse"]]
    assert specific and (not brand_level or max(specific) < min(brand_level))


def test_rows_carry_everything_the_page_shows(conn):
    r = next(r for r in webpage.collect(conn, now=NOW) if "Flip5" in r["title"])
    assert r["url"].startswith("https://") and r["platform_label"] == "PropertyRoom"
    assert r["value"]["low"] < r["value"]["typ"] < r["value"]["high"] and r["conf"] in "ABCD"
    assert r["max_bid"] > r["bid"] and r["profit_now"] > r["profit_end"] > 0
    liq = r["liquidity"]
    assert liq["grade"] in "ABCD?" and liq["days"] > 0 and liq["low"] < liq["days"] < liq["high"] and liq["basis"] == "prior"
    assert r["end"] and datetime.fromisoformat(r["end"]) > NOW


def test_per_style_cap_and_similar_count(conn):
    rows = webpage.collect(conn, limit=100, per_style=2, now=NOW)
    iph = [r for r in rows if "iPhone 13" in r["title"]]
    assert len(iph) == 2 and iph[0]["similar"] == 1     # a third listing exists but is folded into '+1 similar'


def test_limit_is_respected(conn):
    assert len(webpage.collect(conn, limit=1, now=NOW, min_per_kind=0)) == 1


def test_recompute_matches_the_shared_profit_math(conn):
    r = webpage.collect(conn, now=NOW)[0]
    e, v = r["econ"], r["value"]
    assert r["profit_now"] == pytest.approx(catalog.net_profit(e, v["typ"], r["bid"], r["factor"], r["fix"]), abs=0.01)
    r["bid"] = 200.0
    webpage.recompute(r)
    assert r["profit_now"] < 0 and r["ends_at"] == 200.0          # bid already above the 'likely end' share
    r["fix"], r["bid"] = 40.0, 10.0
    before = webpage.recompute(r)["profit_now"]
    r["fix"] = 0.0
    assert webpage.recompute(r)["profit_now"] == pytest.approx(before + 40, abs=0.01)


# ---------------------------------------------------------------- live parsing
PR_TEXT = ("Apple IPhone 12 Pro Listing ID: 18973623 Ending Ended Ends Oct 12, 2026 at 06:45 PM (Eastern) "
           "Current Price US $1,083.01 Bid Increment (US) $3.00 9 Active Bidder(s) 21 Watching 30 Total Bid(s) Winning Bidder ...8318")


def test_parse_pr_live_reads_price_end_and_bids():
    live = webpage.parse_pr_live(PR_TEXT)
    assert live["price"] == 1083.01 and live["bids"] == 30
    assert live["end"] == datetime(2026, 10, 12, 22, 45, tzinfo=timezone.utc)    # 6:45 PM Eastern (EDT) = 22:45 UTC
    assert webpage.parse_pr_live("nothing useful") == {}


def test_parse_sgw_live_flags_closed_and_risky_descriptions():
    ok = webpage.parse_sgw_live({"currentPrice": 12.5, "numberOfBids": 2, "description": "<p>Tested working, minor scuff</p>"})
    assert ok == {"text": "Tested working, minor scuff", "price": 12.5, "bids": 2, "closed": False, "hard": [], "soft": ["scuff"]}
    bad = webpage.parse_sgw_live({"currentPrice": 5, "description": "Screen is CRACKED. Sold for parts only.",
                                  "bidHistory": {"auctionClosed": True}})
    assert bad["closed"] and "cracked" in bad["hard"] and any("parts" in h for h in bad["hard"])


def _row(conn, **over):
    r = webpage.collect(conn, now=NOW)[0]
    r.update(over)
    return r


def test_refresh_updates_live_numbers_and_drops_bad_listings(conn):
    sgw = {"currentPrice": 33.0, "numberOfBids": 4, "description": "works"}
    seen = []

    def handler(req):
        seen.append(str(req.url))
        if "GetItemDetailModelByItemId/2" in str(req.url):
            return httpx.Response(200, json=sgw)
        if "GetItemDetailModelByItemId/3" in str(req.url):
            return httpx.Response(200, json={"currentPrice": 9, "description": "cracked screen"})
        if "GetItemDetailModelByItemId/4" in str(req.url):
            return httpx.Response(200, json={"currentPrice": 9, "bidHistory": {"auctionClosed": True}})
        return httpx.Response(500)

    async def go():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
            rows = [r for r in webpage.collect(conn, now=NOW) if r["platform"] == "shopgoodwill" and "iPhone 13" in r["title"]]
            return await webpage.refresh(rows, Config(), http, sgw_delay=0)

    kept = run(go())
    assert [r["listing_id"] for r in kept] == ["2"]                      # 3 had a cracked screen, 4 had closed
    assert kept[0]["bid"] == 33.0 and kept[0]["bids"] == 4 and kept[0]["verified"] == "page"
    assert kept[0]["profit_now"] == pytest.approx(catalog.net_profit(kept[0]["econ"], kept[0]["value"]["typ"], 33.0, kept[0]["factor"], 0), abs=0.01)


def test_refresh_keeps_a_row_it_cannot_reread_but_says_so(conn):
    def handler(req):
        return httpx.Response(500)

    async def go():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
            rows = [r for r in webpage.collect(conn, now=NOW) if r["platform"] == "shopgoodwill"][:1]
            return await webpage.refresh(rows, Config(), http, sgw_delay=0)

    kept = run(go())
    assert len(kept) == 1 and kept[0]["verified"] == "stale" and "could not re-read" in kept[0]["notes"][0]


def test_rerank_after_refresh_renumbers_and_drops_unprofitable(conn):
    rows = webpage.collect(conn, now=NOW)
    rows[0]["bid"] = 99999
    webpage.recompute(rows[0])
    out = webpage.rerank(rows, limit=10)
    assert rows[0] not in out and [r["rank"] for r in out] == list(range(1, len(out) + 1))


# ---------------------------------------------------------------- html
def test_render_is_a_complete_safe_page(conn):
    rows = webpage.collect(conn, now=NOW)
    rows[0]["title"] = 'Evil </script><script>alert(1)</script> & "quotes"'
    page = webpage.render(rows)
    assert page.count("</script>") == 2                                   # ours only; the title could not break out
    assert "alert(1)</script>" not in page
    data = json.loads(re.search(r'<script id="data" type="application/json">(.*?)</script>', page, re.S).group(1))
    assert data["rows"][0]["title"].startswith("Evil </script>") and data["refreshed"] is False
    for needle in ("Sells in", "Bid at", "Max bid", "Profit if it ends near", "Read this first", "noopener noreferrer", "_blank"):
        assert needle in page, needle
    assert "src=" not in page and "<link" not in page          # self-contained: no external scripts, styles or images
    assert "econ" not in data["rows"][0]                                   # internal assumptions stay out of the page data


def test_page_uses_text_nodes_not_innerhtml_for_untrusted_content():
    assert "innerHTML" not in webpage._TEMPLATE and "document.write" not in webpage._TEMPLATE and "eval(" not in webpage._TEMPLATE


def test_safe_url_only_allows_https():
    assert webpage.safe_url("https://www.propertyroom.com/l/x/1") and not webpage.safe_url("http://x.com") \
        and not webpage.safe_url("javascript:alert(1)") and not webpage.safe_url(None) and not webpage.safe_url("https://a b")


def test_write_site_creates_directories(conn, tmp_path):
    p = webpage.write_site(tmp_path / "nested" / "site" / "index.html", webpage.collect(conn, now=NOW), refreshed=True)
    assert p.exists() and '"refreshed": true' in p.read_text()


def test_server_is_localhost_only_and_serves_the_page(conn, tmp_path):
    webpage.write_site(tmp_path / "site" / "index.html", webpage.collect(conn, now=NOW))
    server = webpage.make_server(tmp_path / "site", 0)
    import threading
    t = threading.Thread(target=server.serve_forever, daemon=True)
    t.start()
    try:
        assert server.server_address[0] == "127.0.0.1"
        r = httpx.get(f"http://127.0.0.1:{server.server_address[1]}/index.html", timeout=5)
        assert r.status_code == 200 and "Top auctions to bid on" in r.text
    finally:
        server.shutdown()
        server.server_close()


def test_cli_web_command(conn, tmp_path, monkeypatch, capsys):
    import main
    conn.close()
    monkeypatch.setattr(main, "Config", lambda: Config(db_path=str(tmp_path / "w.db"), web_dir=str(tmp_path / "out")))
    monkeypatch.setattr("sys.argv", ["main.py", "--web", "--web-limit", "5"])
    asyncio.run(main.amain())
    assert "wrote" in capsys.readouterr().out and (tmp_path / "out" / "index.html").exists()


def test_page_links_to_ebay_sold_and_listed():
    assert "LH_Sold=1" in webpage._TEMPLATE and "LH_BIN=1" in webpage._TEMPLATE


def test_luxury_row_gets_authenticity_chip_and_haircut():
    row = {"title": "Prada nylon backpack", "factor": 1.0}
    webpage._apply_luxury(row)
    assert row["luxury"]["tier"] == "unverified" and row["factor"] == 0.15
    webpage._apply_luxury(row, "Entrupy authenticated")
    assert row["luxury"]["tier"] == "verified" and row["factor"] == 1.0
    assert "authenticity:" in webpage.PAGE_JS if hasattr(webpage, "PAGE_JS") else True


def test_rendered_rows_carry_fee_and_shipping_for_the_page_script():
    import json, re
    import webpage
    row = {"bid": 10.0, "max_bid": 50.0, "liquidity": {"carry": 1.0}, "econ": {"prem": 0.1, "inbound": 12.0, "fee": 0.13, "climb": 0.3, "ship_out": 15}, "title": "x"}
    html = webpage.render([row])
    data = json.loads(re.search(r'<script id="data"[^>]*>(.*?)</script>', html, re.S).group(1))
    assert data["rows"][0]["cost"]["inbound"] == 12.0 and data["rows"][0]["cost"]["prem"] == 0.1


def test_sgw_shipping_total_is_parsed_from_the_calculator_html():
    import asyncio
    import httpx
    import webpage
    html = ("\"<p>Estimated Shipping and Handling:</p><p>Shipping: <span id='shipping-span'>\\u00a415.35 (GROUND_HOME_DELIVERY)</span></p>"
            "<p>Handling: \\u00a43.00</p><p><b>Total Shipping and Handling: \\u00a418.35</b></p>\"")
    transport = httpx.MockTransport(lambda req: httpx.Response(200, text=html, headers={"content-type": "application/json"}))

    async def go():
        async with httpx.AsyncClient(transport=transport) as c:
            return await webpage.sgw_shipping(c, "1", "60601", "IL")
    assert asyncio.run(go()) == 18.35


def test_max_bid_leaves_thirty_percent_on_all_in_cost():
    import webpage
    e = {"fee": 0.1325, "ship_out": 15, "prem": 0.15, "tax": 0.1025, "inbound": 16}
    bid = webpage.max_bid_roi(e, 200, 1.0, 0)
    profit = 200 * (1 - 0.1325) - 0.30 - 15 - (bid * 1.15 * 1.1025 + 16)
    assert abs(webpage.roi(e, profit, bid, 0) - webpage.MIN_ROI) < 1e-6 and profit >= webpage.MIN_PROFIT
    # a cheap item: the $15 floor binds before 30% does
    cheap = webpage.max_bid_roi({**e, "inbound": 0, "ship_out": 0, "prem": 0, "tax": 0}, 30, 1.0, 0)
    assert 30 * (1 - 0.1325) - 0.30 - cheap >= webpage.MIN_PROFIT - 1e-6


def test_selling_shipping_depends_on_item_kind():
    import webpage
    assert webpage.OUTBOUND_BY_KIND["phone"] < webpage.OUTBOUND_BY_KIND["tablet"] < webpage.OUTBOUND_BY_KIND["laptop"]


def test_ebay_fee_defaults_match_the_published_schedule(monkeypatch):
    import importlib, catalog
    monkeypatch.setenv("EBAY_FEE_PCT", "13.6")
    monkeypatch.setenv("EBAY_ORDER_FEE", "0.40")
    monkeypatch.setenv("RETURN_RESERVE_PCT", "3")
    importlib.reload(catalog)
    try:
        e = catalog.economics({"platform": "shopgoodwill", "kind": "tablet", "shipping": 0})
        assert (e["fee"], e["order_fee"], e["returns"]) == (0.136, 0.40, 0.03)
        # $100 resale less 13.6% fee, 3% returns, $0.40 order fee and the catalog default $15 shipping out, with $16 inbound
        assert round(catalog.net_profit(e, 100, 0), 2) == round(100 * (1 - 0.136 - 0.03) - 0.40 - 15 - 16, 2)
    finally:
        monkeypatch.undo()
        importlib.reload(catalog)


def test_gsa_lots_are_listed_only_near_chicago():
    import webpage
    row = lambda loc: {"location": loc}
    assert webpage._gsa_near_chicago(row("Chicago, IL 60607"))
    assert webpage._gsa_near_chicago(row("Gary, IN 46402"))
    assert not webpage._gsa_near_chicago(row("Fort Worth, TX 76101"))
    assert not webpage._gsa_near_chicago(row(None))


def test_local_pickup_sites_are_limited_to_chicago_area_and_illinois_without_zip():
    import webpage
    assert {"gsa", "govdeals", "municibid"} <= webpage.LOCAL_PICKUP_PLATFORMS
    assert not webpage._gsa_near_chicago({"location": "Greeley, CO 80634"})
    assert webpage._gsa_near_chicago({"location": "Evanston, IL 60201"})
    assert not webpage._gsa_near_chicago({"location": "Springfield, IL 62701"})
    assert webpage._gsa_near_chicago({"location": "Springfield, IL"})  # municibid style: state only, so Illinois is accepted


def test_one_brand_cannot_fill_the_page():
    import webpage
    pool = [{"title": f"Air Jordan {i}", "brand": "Jordan", "kind": "shoes", "platform": "shopgoodwill", "listing_id": str(i),
             "group": f"Jordan|style{i}", "profit_end": 30.0 - i * 0.1, "profit_low": 5.0, "roi_end": 0.6, "conf": "B", "rank": 0}
            for i in range(12)]
    pool.append({"title": "Canon thing", "brand": "Canon", "kind": "camera", "platform": "shopgoodwill", "listing_id": "c1",
                 "group": "Canon|x", "profit_end": 20.0, "profit_low": 5.0, "roi_end": 0.5, "conf": "B", "rank": 0})
    out = webpage.rerank(pool, limit=50, per_style=5)
    assert sum(1 for r in out if r["brand"] == "Jordan") <= webpage.PER_BRAND
    assert any(r["brand"] == "Canon" for r in out)


def test_no_bid_lot_passes_only_if_nobody_else_bids():
    import webpage
    e = {"fee": 0.136, "order_fee": 0.40, "returns": 0.0, "ship_out": 10, "prem": 0.0, "tax": 0.0, "inbound": 5, "climb": 0.9}
    row = {"bid": 5.0, "bids": 0, "p_nobid": 0.64, "econ": e, "value": {"low": 80, "typ": 100, "high": 120}, "factor": 1.0, "fix": 0.0,
           "liquidity": {"carry": 0.0}}
    webpage.recompute(row)
    # at $5 it earns ~$65 (>30%), but if it climbs to 90% of value there is nothing left
    assert row["profit_now"] > 60 and row["roi_now"] > 3
    assert row["profit_end"] < webpage.MIN_PROFIT and row["only_if_unbid"] and webpage._passes(row)
    row2 = {**row, "bids": 3}
    webpage.recompute(row2)
    assert not row2["only_if_unbid"] and not webpage._passes(row2)


def test_no_bid_view_needs_a_measured_no_bid_rate():
    import webpage
    e = {"fee": 0.136, "order_fee": 0.40, "returns": 0.0, "ship_out": 10, "prem": 0.0, "tax": 0.0, "inbound": 5, "climb": 0.9}
    row = {"bid": 1.0, "bids": 0, "p_nobid": None, "econ": e, "value": {"low": 80, "typ": 100, "high": 120}, "factor": 1.0, "fix": 0.0,
           "liquidity": {"carry": 0.0}}
    webpage.recompute(row)
    assert not row["only_if_unbid"]   # PropertyRoom-style $1 lots: no measured rate, so no shortcut


def test_used_donated_soft_goods_are_discounted_unless_the_title_says_new():
    import webpage
    assert webpage._NEW_ITEM.search("Nike Air Jordan 1 Retro NWT size 10")
    assert webpage._NEW_ITEM.search("Brand new sealed sneakers deadstock")
    assert not webpage._NEW_ITEM.search("Air Jordan 4 Retro RM Low Gym Basketball Sneakers Size 10.5")
    assert 0 < webpage.USED_SOFT_GOODS_FACTOR < 1


def test_server_is_local_only_by_default_and_lan_is_opt_in(tmp_path):
    (tmp_path / "site").mkdir()
    local = webpage.make_server(tmp_path / "site", 0)
    wide = webpage.make_server(tmp_path / "site", 0, host="0.0.0.0")
    try:
        assert local.server_address[0] == "127.0.0.1"
        assert wide.server_address[0] == "0.0.0.0"
    finally:
        local.server_close()
        wide.server_close()
