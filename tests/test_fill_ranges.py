import asyncio
import sqlite3

import fill_ranges
from clients.ebay import Comp, Comps
from engine import knowledge as kb


class FakeClient:
    def __init__(self, prices):
        self.prices = prices

    async def comps(self, query):
        return Comps("sold", [Comp(f"Sony Bloggie MHS-PM5 camera {i}", p) for i, p in enumerate(self.prices)])


def test_summarize_needs_enough_matching_sales_and_trims_extremes():
    comps = Comps("sold", [Comp("Sony Bloggie MHS-PM5 camera", p) for p in (10, 40, 42, 45, 47, 50, 52, 55, 58, 300)]
                  + [Comp("Canon EOS 5D body", 900)])
    low, typ, high, n = fill_ranges.summarize("Sony Bloggie MHS-PM5 camcorder", comps)
    assert n == 10 and low == 40 and high == 58 and 40 < typ < 60  # the $10 and $300 outliers are cut, the Canon is ignored
    assert fill_ranges.summarize("Sony Bloggie MHS-PM5 camcorder", Comps("sold", comps.items[:2])) is None


def test_junk_labels_are_never_looked_up():
    for junk in ("sold", "touch icloud", "pro charging", "pieces sold", "pro"):
        assert fill_ranges.is_junk_style(junk), junk
    for ok in ("35MM eos", "soundlink mobile", "SS-CBX5", "10G frontman"):
        assert not fill_ranges.is_junk_style(ok), ok


def test_model_identity_names_the_model_not_the_lens_or_noise():
    from engine.title_cleaner import model_identity
    assert model_identity("Canon EOS Rebel XTi Camera with EFS 18-55mm image stabilizer") == "canon eos rebel xti"
    assert model_identity("Canon EOS Rebel 2000 35mm Film SLR Camera Body") == "canon eos rebel 2000"
    assert model_identity("Bose Companion 2 Series II Multimedia Speaker System") == "bose companion 2"
    assert model_identity("Unbranded thing") is None


def test_sold_range_applies_only_to_the_same_model():
    import catalog
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    import db
    conn.executescript(db.SCHEMA) if hasattr(db, "SCHEMA") else None
    kb.ensure_schema(conn) if hasattr(kb, "ensure_schema") else None
    kb.add_observation(conn, "canon eos rebel 2000", "sold", "soldcomps-fill", low=29, high=100, typical=63, n=45)
    assert catalog.model_range(conn, "Canon EOS Rebel 2000 35mm Film SLR Camera Body").typical == 63
    assert catalog.model_range(conn, "Canon EOS Rebel XTi Camera with EFS 18-55mm") is None
    assert catalog.model_range(conn, "Canon EOS C100 Mark II digital cinema camera") is None
    assert catalog.model_range(conn, "Canon EOS Rebel 2000 II Quartz Date") is not None  # a more specific listing still maps to the model


def test_every_sale_is_kept_and_ranges_rebuild_without_the_api(tmp_path):
    import db
    from clients.ebay import CompCache
    conn = db.connect(str(tmp_path / "t.db"))
    cache = CompCache(conn, "soldcomps", 3600)
    sales = [Comp(f"Bose Companion 2 Series II speakers {i}", p) for i, p in enumerate((30, 35, 38, 40, 42, 45, 50, 55))]
    cache.put("bose companion 2 multimedia speaker", sales)
    cache.put("bose companion 2 multimedia speaker", sales[:3] + [Comp("Bose Companion 2 Series II speakers new", 60)])  # a refresh adds, never drops
    assert len(cache.stored("bose companion 2 multimedia speaker")) == 9
    res = fill_ranges.rebuild(conn)
    assert res[0]["status"] == "stored" and res[0]["name"] == "bose companion 2"
    assert kb.compute_range(conn, "bose companion 2").typical is not None


def test_variant_of_reads_storage_and_connectivity():
    from engine.title_cleaner import variant_of
    assert variant_of("Apple iPad 7th Gen 10.2in 32GB Silver Wi‑Fi 3GB RAM") == ("32gb", "wifi")
    assert variant_of("Apple iPad (7th Generation) A2200 32 GB, Wi-Fi + 4G") == ("32gb", "cellular")
    assert variant_of("MacBook Pro 16GB RAM 1TB SSD") == ("1tb", None)
    assert variant_of("Samsung Galaxy Tab A") == (None, None)


def test_accessories_and_broken_units_do_not_set_the_working_price():
    comps = Comps("sold", [Comp(t, p) for t, p in (
        ("For Apple iPad 9th 8th 7th Generation 10.2 Leather Case Smart Stand", 8), ("Apple iPad 7th Gen 128GB Wi-Fi", 120),
        ("Apple iPad 7th Gen 128GB Wi-Fi", 125), ("Apple iPad 7th Gen 128GB Wi-Fi Very Good", 118), ("Apple iPad 7th Gen 128GB Wi-Fi", 130),
        ("Apple iPad 7th Gen 128GB Wi-Fi iCloud locked for parts", 30), ("Lot of 3 Apple iPad 7th Gen 128GB", 300))])
    s = fill_ranges.summarize("apple ipad 7th", comps)
    assert s is not None and s[3] == 4 and 118 <= s[1] <= 130
    v = fill_ranges.variant_ranges("apple ipad 7th", comps)
    assert "128gb" in v and "128gb wifi" in v


def test_listing_gets_the_range_for_its_own_variant(tmp_path):
    import catalog, db
    conn = db.connect(str(tmp_path / "v.db"))
    kb.add_observation(conn, "apple ipad 7th", "sold", "soldcomps-fill", low=60, high=140, typical=90, n=30)
    kb.add_observation(conn, "apple ipad 7th 128gb", "sold", "soldcomps-fill", low=110, high=140, typical=125, n=10)
    kb.add_observation(conn, "apple ipad 7th 32gb wifi", "sold", "soldcomps-fill", low=60, high=85, typical=72, n=8)
    assert catalog.model_range(conn, "Apple iPad 7th Gen 128GB Wi-Fi").typical == 125
    assert catalog.model_range(conn, "Apple iPad 7th Gen 32GB Wi-Fi").typical == 72
    assert catalog.model_range(conn, "Apple iPad 7th Gen 64GB").typical == 90   # no 64gb sales: pooled, and the note says so
    assert catalog.model_range(conn, "Apple iPad 7th Gen").typical == 90
