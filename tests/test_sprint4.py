from datetime import datetime, timedelta, timezone

import pytest

import db
from clients import gsa, propertyroom
from clients.ebay import Comp, Comps
from config import Config
from engine.title_cleaner import is_locked
from engine.valuation import valuate


@pytest.mark.parametrize("title", [
    "Apple iPhone 12 iCloud Locked", "iPad Activation Lock", "Galaxy S21 Bad ESN", "Dell Laptop Parts Only",
    "Laptop For Parts", "MacBook Pro Passcode", "Carrier locked Galaxy", "Pixel 7 MDM",
])
def test_is_locked_flags_lock_conditions(title):
    assert is_locked(title)


@pytest.mark.parametrize("title", [
    "Factory Unlocked iPhone 13", "iPhone 11 iCloud removed", "Phone no passcode",
    "Laptop - no power adapter", "Nikon D750 body", "Pixel MDM removed", "Camera No Power", "Laptop Unchecked",
])
def test_is_locked_ignores_clean_titles(title):
    assert not is_locked(title)


def test_db_stores_is_locked(tmp_path):
    conn = db.connect(str(tmp_path / "t.db"))
    db.record(conn, platform="gsa", listing_id="1", title="x iCloud", price=10, shipping_cost=0,
              status="skipped", is_locked=True)
    assert conn.execute("SELECT is_locked FROM listings").fetchone()[0] == 1


def test_gov_premium_matches_spec_formula():
    cfg = Config()
    comps = Comps("sold", [Comp("Fluke 87V multimeter", 400.0, datetime.now(timezone.utc)) for _ in range(6)])
    price, ship = 100.0, 0.0
    val = valuate(cfg, "fluke 87v", comps, price, ship, title="Fluke 87V multimeter",
                  extra_cost=price * 0.15)
    expected = 400 - price * 1.15 - cfg.outbound_shipping - 400 * 0.1325 - 0.30
    assert val.net_profit == pytest.approx(expected, abs=0.01)


def test_locked_factor_cuts_value_to_20_percent():
    cfg = Config()
    comps = Comps("sold", [Comp("Apple iPhone 12 64GB", 300.0, datetime.now(timezone.utc)) for _ in range(6)])
    val = valuate(cfg, "iphone 12", comps, 50, 0, title="Apple iPhone 12 64GB",
                  condition_factor=cfg.locked_condition_factor)
    assert val.estimated_value == pytest.approx(60.0)


def _gsa_raw(**kw):
    end = (datetime.now(timezone.utc) + timedelta(days=2)).date().isoformat()
    raw = {"saleNo": "3-1-QSC-I-27-002", "lotNo": "010", "auctionStatus": "Active", "itemName": "Cisco Switch Lot",
           "highBidAmount": "125.0", "aucEndDt": end, "propertyCity": "Erie", "propertyState": "PA",
           "propertyZip": "16504", "itemDescURL": "https://www.gsaauctions.gov/auctions/preview/1",
           "imageURL": "https://img", "biddersCount": "3", "lotInfo": "<p>Tested &amp; working</p>"}
    raw.update(kw)
    return raw


def test_gsa_parse_item():
    l = gsa.parse_item(_gsa_raw(), premium_pct=15, freight=75)
    assert (l.platform, l.listing_id, l.price, l.listing_type) == ("gsa", "3-1-QSC-I-27-002-010", 125.0, "daily_auction")
    assert l.location == "Erie, PA 16504" and l.shipping_cost == 75 and l.extra_cost_pct == 15
    assert l.extra["description"] == "Tested & working"
    assert l.end_time.tzinfo is not None


@pytest.mark.parametrize("kw", [
    {"itemName": "2020 Chevrolet Suburban"}, {"itemName": "Copy Paper Lot"}, {"highBidAmount": None},
    {"auctionStatus": "Preview"}, {"aucEndDt": "2020-01-01"}, {"itemName": "Trailer Mounted Generator Tool Kit"},
])
def test_gsa_parse_item_filters(kw):
    assert gsa.parse_item(_gsa_raw(**kw)) is None


CARD = '''<a href="/l/apple-ipod-touch/18973400" class="listing-card" data-listing-id="18973400" data-slug="x" data-format="English"
   data-photos='[]'>
    <div class="listing-image"><img type="" src="https://img/1.webp" alt="" /></div>
    <div class="listing-title">Apple IPod Touch 6th Gen, ICloud Locked &amp; Parts </div>
    <div class="listing-meta">
        <span class="listing-time" data-end="{end}">1h</span>
        <span class="listing-meta-sep">|</span><span class="listing-price">$1,204.50</span>
        <span class="listing-meta-sep">|</span><span class="listing-bids">3 bids</span>
    </div>
</a>
<a href="/l/nintendo/1" class="listing-card" data-listing-id="1" data-slug="n" data-format="FixedPrice" data-photos='[]'>
    <div class="listing-title">Nintendo System</div>
    <div class="listing-meta"><span class="listing-time" data-end="{end}">1h</span>
        <span class="listing-price">$149.00</span></div>
</a>'''


def test_propertyroom_parse_cards():
    end = (datetime.now(timezone.utc) + timedelta(hours=1)).strftime("%Y-%m-%dT%H:%M:%S.0000000Z")
    auction, bin_ = propertyroom.parse_cards(CARD.format(end=end), "electronics", premium_pct=15, shipping=15)
    assert (auction.listing_id, auction.price, auction.listing_type) == ("18973400", 1204.5, "auction")
    assert auction.title == "Apple IPod Touch 6th Gen, ICloud Locked & Parts"
    assert auction.extra["bid_count"] == 3 and auction.extra["local_pickup_only"] is None
    assert auction.shipping_cost == 15 and auction.extra_cost_pct == 15 and auction.category == "electronics"
    assert (bin_.listing_type, bin_.price, bin_.extra["bid_count"]) == ("buy_it_now", 149.0, None)
    assert is_locked(auction.title)


def test_propertyroom_skips_ended():
    end = (datetime.now(timezone.utc) - timedelta(hours=1)).strftime("%Y-%m-%dT%H:%M:%S.0000000Z")
    assert propertyroom.parse_cards(CARD.format(end=end), "electronics") == []
