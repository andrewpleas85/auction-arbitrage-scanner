from datetime import datetime, timedelta, timezone

import pytest

import catalog
import db
from engine import knowledge as kb
from models import Listing

NOW = datetime.now(timezone.utc)


@pytest.mark.parametrize("title,brand,kind,style_has", [
    ("Coach F54687 Beige Pebble Leather Tote Bag Gold Hardware", "Coach", "bag", "F54687"),
    ("Louis Vuitton Purse Alma BB Damier", "Louis Vuitton", "bag", "alma"),
    ("Michael Kors Black Leather Crossbody Handbag", "Michael Kors", "bag", ""),
    ("UGG Women's Adirondack II Dark Brown Winter Boots Sz 9", "UGG", "shoes", "adirondack"),
    ("Brooks Brothers Wool Suit 42R", "Brooks Brothers", "clothing", ""),
    ("Christian Louboutin Shoes So Kate 120mm Pumps", "Christian Louboutin", "shoes", "kate"),
    ("Kate Spade New York Black Leather Wallet", "Kate Spade", "wallet", ""),
    ("Gucci GG Marmont belt 90", "Gucci", "accessory", "marmont"),
])
def test_style_of(title, brand, kind, style_has):
    b, k, s = catalog.style_of(title)
    assert (b, k) == (brand, kind) and style_has.lower() in s.lower()


def _l(i, title, bid, kind="auction", hours=48, platform="shopgoodwill", **extra):
    return Listing(platform, str(i), title, bid, 0.0, f"https://x/{i}", None, "cat", listing_type=kind,
                   end_time=NOW + timedelta(hours=hours), extra=extra)


def _filled(conn):
    catalog.store(conn, [
        _l(1, "Louis Vuitton Purse Alma BB Damier", 150),
        _l(2, "Louis Vuitton Purse Alma BB Monogram", 220),
        _l(3, "Coach F54687 Beige Pebble Leather Tote Bag", 39.97),
        _l(4, "Coach F54687 Black Pebble Leather Tote Bag", 45),
        _l(5, "UGG Women's Adirondack II Brown Boots Sz 9", 29.99, hours=3),
        _l(6, "Gucci Purse GG Marmont", 700, kind="buy_it_now"),
        _l(7, "Prada Diagramme Quilted Red Leather Shoulder Bag", 525, hours=-5),   # already ended
        _l(8, "Plain leather purse", 10),                                            # no designer
    ])


def test_store_keeps_designer_listings_only(tmp_path):
    conn = db.connect(str(tmp_path / "c.db"))
    _filled(conn)
    assert conn.execute("SELECT COUNT(*) FROM catalog").fetchone()[0] == 7


def test_store_updates_bids_but_keeps_first_seen(tmp_path):
    conn = db.connect(str(tmp_path / "c.db"))
    catalog.store(conn, [_l(1, "Louis Vuitton Purse Alma BB Damier", 150)])
    first = conn.execute("SELECT first_seen FROM catalog").fetchone()[0]
    conn.execute("UPDATE catalog SET first_seen = first_seen - 1000")
    catalog.store(conn, [_l(1, "Louis Vuitton Purse Alma BB Damier", 190)])
    r = conn.execute("SELECT current_bid, first_seen, last_seen FROM catalog").fetchone()
    assert r["current_bid"] == 190 and r["first_seen"] == first - 1000 and r["last_seen"] >= first


def test_groups_only_running_auctions(tmp_path):
    conn = db.connect(str(tmp_path / "c.db"))
    _filled(conn)
    gs = catalog.groups(conn)
    titles = {g["sample"]["title"] for g in gs}
    assert not any("Gucci" in t for t in titles)      # fixed price
    assert not any("Prada" in t for t in titles)      # ended
    coach = next(g for g in gs if g["brand"] == "Coach")
    assert coach["n"] == 2 and coach["bid_low"] == 39.97 and coach["bid_high"] == 45
    assert coach["tier"] == "B" and next(g for g in gs if g["brand"] == "Louis Vuitton")["tier"] == "A"
    assert any(g["sample"]["title"].startswith("Gucci") for g in catalog.groups(conn, only_auctions=False))


def test_known_ranges_attach_to_matching_groups(tmp_path):
    conn = db.connect(str(tmp_path / "c.db"))
    _filled(conn)
    kb.add_observation(conn, "Coach F54687 pebble leather tote", "sold", "ebay", low=40, high=70, typical=50, n=5)
    gs = catalog.groups(conn)
    coach = next(g for g in gs if g["brand"] == "Coach")
    assert coach["range"] is not None and coach["range"].typical == 50
    assert next(g for g in gs if g["brand"] == "UGG")["range"] is None


def test_research_priority_prefers_luxury_cheap_and_numerous():
    base = dict(n=2, bid_low=50, style="", sample=None)
    lux = catalog.priority({**base, "tier": "A"})
    assert lux > catalog.priority({**base, "tier": "B"}) > catalog.priority({**base, "tier": "C"})
    assert catalog.priority({**base, "tier": "A", "n": 6}) > lux
    assert catalog.priority({**base, "tier": "A", "bid_low": 900}) < lux
    assert catalog.priority({**base, "tier": "A", "style": "F54687"}) > lux


def test_report_lists_research_queue_and_known_ranges(tmp_path):
    conn = db.connect(str(tmp_path / "c.db"))
    _filled(conn)
    kb.add_observation(conn, "Coach F54687 pebble leather tote", "sold", "ebay", low=40, high=70, typical=50, n=5)
    out = catalog.format_report(conn)
    assert "CATALOG: 5 auctions" in out and "RESEARCH QUEUE" in out and "WITH A KNOWN VALUE RANGE" in out
    assert out.index("Louis Vuitton", out.index("RESEARCH QUEUE")) > 0
    assert "$40-$70" in out or "$40" in out
    assert "The catalog is empty" in catalog.format_report(db.connect(str(tmp_path / "empty.db")))


def test_a_group_only_inherits_a_range_when_its_style_matches(tmp_path):
    conn = db.connect(str(tmp_path / "c.db"))
    catalog.store(conn, [_l(1, "Nike Air Jordan Kids Sneakers Size 12C", 5), _l(2, "Prada Diagramme Quilted Red Leather Shoulder Bag", 500)])
    kb.add_observation(conn, "Air Jordan 11 Retro Win Like 96 men's size 10.5", "ask", "ebay", low=65, high=175, n=6)
    kb.add_observation(conn, "Prada Diagramme quilted chain shoulder bag", "ask", "ebay", low=800, high=2250, n=8)
    gs = {g["brand"]: g for g in catalog.groups(conn)}
    assert gs["Jordan"]["range"] is None          # different shoe: no inherited range
    assert gs["Prada"]["range"] is not None       # same style words: matched


def test_strict_find_requires_every_query_word(tmp_path):
    conn = db.connect(str(tmp_path / "c.db"))
    kb.add_observation(conn, "Air Jordan 11 Retro Win Like 96", "ask", "ebay", typical=100)
    assert kb.find_items(conn, "jordan kids", strict=True) == []
    assert kb.find_items(conn, "jordan retro 96", strict=True) == ["air jordan 11 retro win like 96"]
    assert kb.find_items(conn, "air jordan retro")  # loose lookup for people still finds related items


def test_groups_fall_back_to_a_brand_baseline_when_no_style_matches(tmp_path):
    conn = db.connect(str(tmp_path / "c.db"))
    catalog.store(conn, [_l(1, "Michael Kors Black Leather Crossbody Handbag", 15), _l(2, "Michael Kors Pink Wristlet Wallet", 8)])
    kb.add_observation(conn, "Michael Kors baseline-bags", "ask", "ebay", low=30, high=120, typical=60, n=30)
    gs = {g["kind"]: g for g in catalog.groups(conn)}
    assert gs["bag"]["range"] is not None and any("baseline" in n for n in gs["bag"]["range"].notes)
    assert gs["wallet"]["range"] is None  # only bags have a baseline
    assert catalog.baseline_key("Michael Kors", "bag") == "michael kors baseline-bags"
    assert catalog.baseline_key("UGG", "shoes") == "ugg baseline-footwear"
