import pytest
from clients.ebay import Comp, Comps
from config import Config
from engine.matching import build_query, clean_title, is_relevant
from engine.valuation import valuate


def test_clean_title_strips_noise():
    assert clean_title("MINT Tested Roland Juno-60 Synthesizer L@@K OBO!") == "roland juno-60 synthesizer"


def test_build_query_caps_tokens():
    assert len(build_query("a1 b2 c3 d4 e5 f6 g7 h8 i9 j10").split()) == 7


def test_relevance_filters_accessories():
    q = "roland juno-60"
    assert is_relevant(q, "Roland Juno-60 Polyphonic Synthesizer")
    assert not is_relevant(q, "Yamaha DX7 Synth")


def test_valuate_profit_math():
    cfg = Config(min_comps=3, outbound_shipping=10, resale_fee_pct=10, active_ask_discount=1.0)
    comps = Comps("sold", [Comp("roland juno-60", p) for p in (500, 500, 500, 500)])
    v = valuate(cfg, "roland juno-60", comps, price=200, shipping=20)
    assert v.estimated_value == 500
    assert v.net_profit == 500 * 0.9 - 10 - 220 - 0.30  # incl. $0.30 fixed selling fee
    assert v.meets(cfg)


def test_valuate_needs_enough_comps():
    cfg = Config(min_comps=5)
    comps = Comps("sold", [Comp("roland juno-60", 500)] * 2)
    assert valuate(cfg, "roland juno-60", comps, 100, 0) is None


# ---- Sprint 2 ----
from engine.title_cleaner import build_search_query, comp_matches, extract_brand, extract_models, is_risky
from engine.valuation import valuate_lot


def test_extract_brand_and_models():
    t = "Old Vintage Black Box Receiver Marantz 2275 Stereo Untested L@@K"
    assert extract_brand(t) == "marantz"
    assert extract_models(t)[0] == "2275"
    assert is_risky(t)
    assert build_search_query(t) == "marantz 2275"


def test_model_patterns():
    assert extract_models("Cisco WS-C3850-24T Switch 24 Port 1GB")[0] == "ws-c3850-24t"
    assert extract_models("Leica M6 35mm Rangefinder 1990")[0] == "m6"


def test_comp_matches_is_strict_on_model():
    t = "Vintage Marantz 2230 Receiver Untested"
    assert comp_matches(t, "Marantz 2230 Stereo Receiver Tested Works")
    assert not comp_matches(t, "Marantz 2235B Stereo Receiver")
    assert not comp_matches(t, "Pioneer 2230 Receiver")
    assert not comp_matches(t, "Marantz 2230 Receiver FOR PARTS")
    assert comp_matches("Cisco WS-C3850-24T switch", "Cisco WSC3850-24T Catalyst Switch")


def test_valuate_lot():
    cfg = Config(outbound_shipping=10, resale_fee_pct=10, lot_haircut=0.5)
    v = valuate_lot(cfg, [(4, 100.0), (2, None)], lot_cost=100)
    assert v.estimated_value == 200  # 4*100*0.5
    assert v.net_profit == pytest.approx(200 * 0.9 - 40 - 100 - 4 * 0.30)  # fixed fee per unit sold
    assert "4/6" in v.detail


def test_db_migration(tmp_path):
    import sqlite3, db
    p = str(tmp_path / "old.db")
    c = sqlite3.connect(p)
    c.execute("CREATE TABLE listings (listing_id TEXT, platform TEXT, title TEXT, price REAL, "
              "shipping_cost REAL, estimated_market_value REAL, profit_margin REAL, "
              "first_seen_timestamp INTEGER, status TEXT, PRIMARY KEY (platform, listing_id))")
    c.commit(); c.close()
    conn = db.connect(p)
    cols = {r["name"] for r in conn.execute("PRAGMA table_info(listings)")}
    assert {"listing_type", "end_time", "location", "gross_margin_pct", "net_profit_usd"} <= cols


def test_govdeals_rss_item_parsing():
    import xml.etree.ElementTree as ET
    from clients.govdeals import GovDealsClient
    g = GovDealsClient(None, [], ["oscilloscope"], "Denver", 75, 10)
    item = ET.fromstring(
        "<item><title>Tektronix TDS2024 Oscilloscope</title>"
        "<link>https://www.govdeals.com/index.cfm?fa=Main.Item&amp;itemid=123</link>"
        "<description>Current Bid: $240.00 Location: Boulder, CO Auction Ends: 10/20/2026 02:00 PM"
        "</description></item>")
    l = g._parse(item)
    assert (l.listing_id, l.price, l.location, l.shipping_cost) == ("123", 240.0, "Boulder, CO", 75)


# ---- Sprint 3 ----
from datetime import datetime, timedelta, timezone
from alerting.discord import CRITICAL, WATCHLIST, alert_tier, build_embed
from engine.valuation import trimmed_mean
from models import Listing


def test_trimmed_mean_drops_high_and_low():
    assert trimmed_mean([10, 100, 110, 120, 1000]) == 110


def test_true_market_value_uses_latest_sales_and_fee_formula():
    cfg = Config(min_comps=5, sold_comps_max=7, resale_fee_pct=13.25, outbound_shipping=20)
    now = datetime.now(timezone.utc)
    sales = [Comp("marantz 2230 receiver", p, now - timedelta(days=i)) for i, p in
             enumerate([300, 310, 320, 330, 340, 350, 360])]
    sales += [Comp("marantz 2230 receiver", 9999, now - timedelta(days=90))]  # stale: excluded
    v = valuate(cfg, "marantz 2230", Comps("sold", sales), price=100, shipping=10,
                title="Marantz 2230 Receiver")
    tmv = (310 + 320 + 330 + 340 + 350) / 5  # 7 newest, drop 300 and 360
    assert v.estimated_value == tmv
    assert v.net_profit == round(tmv - 110 - 20 - tmv * 0.1325 - 0.30, 2)


def test_active_comps_rejected_by_default():
    cfg = Config(min_comps=3)
    comps = Comps("active", [Comp("marantz 2230", 500)] * 6)
    assert valuate(cfg, "marantz 2230", comps, 100, 0, title="Marantz 2230") is None


def _val(cfg, profit=200):
    from engine.valuation import Valuation
    return Valuation(500, 8, "sold", 100, profit, 200.0)


def _listing(kind, minutes):
    return Listing("shopgoodwill", "1", "x", 50, 5, "https://x", None, "c", kind,
                   datetime.now(timezone.utc) + timedelta(minutes=minutes))


def test_alert_tiers():
    cfg = Config(min_profit=50, min_margin_pct=35, ending_window_minutes=15)
    assert alert_tier(_listing("buy_it_now", 5000), _val(cfg), cfg) == CRITICAL
    assert alert_tier(_listing("auction", 10), _val(cfg), cfg) == WATCHLIST
    assert alert_tier(_listing("auction", 120), _val(cfg), cfg) is None  # too early: park it
    assert alert_tier(_listing("buy_it_now", 5000), _val(cfg, profit=10), cfg) is None
    assert build_embed(_listing("auction", 10), _val(cfg), WATCHLIST)["color"] != \
        build_embed(_listing("buy_it_now", 9), _val(cfg), CRITICAL)["color"]


def test_shopgoodwill_item_pricing():
    from clients.shopgoodwill import parse_item
    future = (datetime.now(timezone.utc) + timedelta(days=1)).strftime("%Y-%m-%dT%H:%M:%S")
    base = {"itemId": 1, "title": "t", "currentPrice": 10.0, "shippingPrice": 5, "endTime": future,
            "imageURL": "a\\b.jpg"}
    # A Buy It Now ABOVE the current bid means an auction in progress: priced at the bid, BIN kept as an alternative.
    auction = parse_item({**base, "buyNowPrice": 25.0, "numBids": 2})
    assert (auction.listing_type, auction.price, auction.extra["buy_now_price"]) == ("auction", 10.0, 25.0)
    assert parse_item({**base, "buyNowPrice": 10.0}).listing_type == "buy_it_now"  # fixed price: BIN == price
    a = parse_item({**base, "buyNowPrice": 0})
    assert (a.listing_type, a.price) == ("auction", 10.0)  # current bid, no uplift
