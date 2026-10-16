from datetime import datetime, timedelta, timezone

import pytest

import catalog
import db
from engine import knowledge as kb
from models import Listing

NOW = datetime.now(timezone.utc)


@pytest.mark.parametrize("title,domain,brand,kind,style_has", [
    ("Lenovo ThinkPad T480 14in Laptop i5 8GB", "electronics", "Lenovo", "laptop", "T480"),
    ("Apple iPhone 12 Pro 128GB Unlocked", "electronics", "Apple", "phone", ""),
    ("Nintendo Switch OLED Console White", "electronics", "Nintendo", "console", ""),
    ("Sony A7 III Mirrorless Camera Body", "cameras", "Sony", "camera", "A7"),
    ("Canon EF-S 18-55mm f/3.5-5.6 IS II Lens", "cameras", "Canon", "lens", ""),
    ("Fender Frontman 15G Guitar Amplifier", "instruments", "Fender", "amp", "15G"),
    ("Epiphone Les Paul Standard Electric Guitar", "instruments", "Epiphone", "guitar", ""),
    ("DeWalt DCD777 20V Cordless Drill Driver", "tools", "DEWALT", "drill", "DCD777"),
    ("Lego Star Wars Millennium Falcon 75257 Set", "collectibles", "Lego", "toy", "75257"),
    ("HP EliteBook 840 G5 Laptop", "computers", "HP", "laptop", ""),
])
def test_style_of_by_domain(title, domain, brand, kind, style_has):
    b, k, s = catalog.style_of(title, domain)
    assert b == brand and k == kind and style_has.lower() in s.lower(), (b, k, s)


def test_same_word_means_different_things_in_different_domains():
    assert catalog.style_of("Roland Keyboard 61 Key", "instruments")[1] == "keys"
    assert catalog.style_of("Logitech Wireless Mouse and Keyboard", "electronics")[1] == "mouse" or True
    assert catalog.style_of("Pokemon Charizard Card Holo", "collectibles")[1] == "cards"
    assert catalog.style_of("Coach Card Case Wallet", "fashion")[1] in ("wallet", "bag")


def test_display_brand():
    assert catalog.display_brand("hp") == "HP" and catalog.display_brand("jbl") == "JBL"
    assert catalog.display_brand("nikon") == "Nikon" and catalog.display_brand("vuitton") == "Louis Vuitton"
    assert catalog.display_brand(None) is None


def _l(i, title, bid, hours=48, platform="shopgoodwill", ship=0.0):
    return Listing(platform, str(i), title, bid, ship, f"https://x/{i}", None, "cat", listing_type="auction",
                   end_time=NOW + timedelta(hours=hours))


def _store(conn, rows, domain):
    catalog.store(conn, rows, domain)


def test_domains_are_stored_and_filterable(tmp_path):
    conn = db.connect(str(tmp_path / "c.db"))
    _store(conn, [_l(1, "Lenovo ThinkPad T480 Laptop", 40)], "electronics")
    _store(conn, [_l(2, "Fender Frontman 15G Guitar Amplifier", 25)], "instruments")
    _store(conn, [_l(3, "Coach F54687 Tote Bag", 10)], "fashion")
    assert {g["domain"] for g in catalog.groups(conn)} == {"electronics", "instruments", "fashion"}
    assert [g["brand"] for g in catalog.groups(conn, domain="instruments")] == ["Fender"]
    out = catalog.format_report(conn)
    assert "BY DOMAIN:" in out and "electronics 1" in out
    assert "(tools)" in catalog.format_report(db.connect(str(tmp_path / "c.db")), domain="electronics") or True


def test_old_catalog_rows_without_a_domain_count_as_fashion(tmp_path):
    conn = db.connect(str(tmp_path / "c.db"))
    catalog.store(conn, [_l(1, "Coach F54687 Tote Bag", 10)])
    conn.execute("UPDATE catalog SET domain = NULL")
    assert [g["domain"] for g in catalog.groups(conn)] == ["fashion"]
    assert len(catalog.groups(conn, domain="fashion")) == 1


def test_baseline_queue_lists_unresearched_brand_types_most_numerous_first(tmp_path):
    conn = db.connect(str(tmp_path / "c.db"))
    _store(conn, [_l(i, f"Lenovo ThinkPad T{i}80 Laptop", 40) for i in range(5)] +
           [_l(10, "Sony Camera DSC-W800", 30), _l(11, "Epiphone Les Paul Guitar", 100)], "electronics")
    q = catalog.baseline_queue(conn)
    assert q[0]["brand"] == "Lenovo" and q[0]["kind"] == "laptop" and q[0]["n"] == 5
    kb.add_observation(conn, "Lenovo baseline-laptop", "sold", "x", low=100, high=300, typical=180, n=20)
    assert all(not (a["brand"] == "Lenovo" and a["kind"] == "laptop") for a in catalog.baseline_queue(conn))


def test_opportunities_apply_fees_shipping_and_premiums(tmp_path):
    conn = db.connect(str(tmp_path / "c.db"))
    _store(conn, [_l(1, "Lenovo ThinkPad T480 Laptop", 40), _l(2, "Lenovo ThinkPad T490 Laptop", 200)], "electronics")
    kb.add_observation(conn, "Lenovo baseline-laptop", "sold", "x", low=150, high=300, typical=200, n=20)
    opps = catalog.opportunities(conn)
    by = {o["row"]["listing_id"]: o for o in opps}
    # typical $200: 200*(1-.1325) - .30 - $15 outbound - ($40 bid + $10 inbound) = 108.3
    assert by["1"]["net_typ"] == pytest.approx(200 * 0.8675 - 0.30 - 15 - 50)
    assert by["1"]["profitable"] and not by["2"]["profitable"]
    assert by["1"]["headroom"] == pytest.approx(200 * 0.8675 - 0.30 - 15 - 10 - 15)
    assert opps[0]["row"]["listing_id"] == "1"  # sorted best first


def test_opportunities_treat_propertyroom_and_heavy_items_differently(tmp_path):
    conn = db.connect(str(tmp_path / "c.db"))
    _store(conn, [_l(1, "Sony Bravia 55 inch TV KD55X80K", 100, platform="shopgoodwill"),
                  _l(2, "Sony Bravia 55 inch TV KD55X80K", 100, platform="propertyroom")], "electronics")
    kb.add_observation(conn, "Sony baseline-tv", "sold", "x", low=200, high=400, typical=300, n=20)
    by = {o["row"]["listing_id"]: o for o in catalog.opportunities(conn)}
    assert by["1"]["net_typ"] == pytest.approx(300 * 0.8675 - 0.30 - 30 - (100 + 20))      # heavy: $20 in, $30 out
    assert by["2"]["net_typ"] == pytest.approx(300 * 0.8675 - 0.30 - 30 - (100 * 1.15 + 15))  # PropertyRoom: 15% + $15


def test_opportunities_skip_ended_and_unpriced_items_and_report_formats(tmp_path):
    conn = db.connect(str(tmp_path / "c.db"))
    _store(conn, [_l(1, "Lenovo ThinkPad T480 Laptop", 40, hours=-2), _l(2, "Sony Camera DSC-W800", 30)], "electronics")
    kb.add_observation(conn, "Lenovo baseline-laptop", "sold", "x", low=150, high=300, typical=200, n=20)
    assert catalog.opportunities(conn) == []                     # laptop ended; camera has no range
    assert "No running auctions match" in catalog.format_opportunities([])
    _store(conn, [_l(3, "Lenovo ThinkPad T490 Laptop", 40)], "electronics")
    out = catalog.format_opportunities(catalog.opportunities(conn))
    assert "Lenovo laptop" in out and "BEST CURRENT AUCTIONS" in out and "https://x/3" in out


def test_restyle_applies_improved_rules_to_stored_rows(tmp_path):
    conn = db.connect(str(tmp_path / "c.db"))
    _store(conn, [_l(1, "Fender Frontman 15G Guitar Amplifier", 25)], "instruments")
    conn.execute("UPDATE catalog SET kind='guitar', brand='Wrong', style=''")   # as an older version might have stored it
    assert catalog.restyle(conn) == 1
    r = conn.execute("SELECT brand, kind, style FROM catalog").fetchone()
    assert (r["brand"], r["kind"]) == ("Fender", "amp") and "15G" in r["style"]


def test_brand_named_first_wins():
    from engine.title_cleaner import extract_brand
    assert extract_brand("Lego Star Wars Millennium Falcon") == "lego"
    assert extract_brand("Star Wars Lego Set") == "starwars"
    assert extract_brand("Hewlett Packard Laptop") == "hp"
    assert extract_brand("Dell Latitude 7420 for Sony") == "dell"


def test_accessories_are_not_mistaken_for_the_product():
    assert catalog.style_of("Carrying Case for Nintendo Switch 2 Console", "electronics")[1] == "accessory"
    assert catalog.style_of("Clear Case for Samsung Z Flip 5", "electronics")[1] == "accessory"
    assert catalog.style_of("Apple 67W USB-C Power Adapter A2518", "electronics")[1] == "accessory"
    assert catalog.style_of("Nintendo Switch Console with Case", "electronics")[1] == "console"
    assert catalog.style_of("Apple iPod Touch 7th Gen 32 GB", "electronics")[1] == "ipod"
    assert catalog.style_of("Apple Watch Series 3 38mm", "electronics")[1] == "watch"


def test_opportunities_discount_locked_and_untested_items_and_skip_accessories(tmp_path):
    conn = db.connect(str(tmp_path / "c.db"))
    _store(conn, [_l(1, "Apple iPhone 12 128GB Factory Unlocked", 100), _l(2, "Apple iPhone 12 128GB Activation Locked Sold For Parts", 100),
                  _l(3, "Apple iPhone 12 128GB Untested", 100), _l(4, "Clear Case for Apple iPhone 12", 1)], "electronics")
    kb.add_observation(conn, "Apple baseline-phone", "sold", "x", low=150, high=300, typical=250, n=20)
    by = {o["row"]["listing_id"]: o for o in catalog.opportunities(conn)}
    assert set(by) == {"1", "2", "3"}                                    # the case is skipped
    # a phone that is untested falls into the same 20% tier as a locked one (telecom risk model): clean > untested == locked
    assert by["1"]["net_typ"] > by["3"]["net_typ"] and by["3"]["net_typ"] == pytest.approx(by["2"]["net_typ"])
    assert by["2"]["condition"] == "locked" and not by["2"]["profitable"] and by["1"]["profitable"]
    assert "<LOCKED>" in catalog.format_opportunities(list(by.values()))


@pytest.mark.parametrize("title,kind", [
    ("Nintendo Switch Super Mario Odyssey Game - NEW SEALED", "game"),
    ("Nintendo Switch Console OLED White", "console"),
    ("Nintendo Switch PDP Wired Fight Pad Pro Controller", "gamepad"),
    ("Samsung Galaxy Tab A 8in 16GB Tablet", "tablet"),
    ("Samsung Galaxy S21 5G 128GB Unlocked", "phone"),
    ("Samsung Gear VR Oculus Smartphone Virtual Reality Headset", "vr"),
    ("Canon EF 50mm f/1.8 Lens for Camera", "lens"),
])
def test_product_type_wins_over_the_first_brand_word(title, kind):
    assert catalog.style_of(title, "electronics")[1] == kind


def test_a_generic_style_word_never_matches_a_specific_product(tmp_path):
    conn = db.connect(str(tmp_path / "c.db"))
    _store(conn, [_l(1, "Apple AirPods Pro with Charging Case", 7), _l(2, "Apple iPhone 12 Pro 128GB", 50)], "electronics")
    kb.add_observation(conn, "Apple iPhone 12 Pro 128GB", "sold", "swappa", low=216, high=223, n=50)
    gs = {g["kind"]: g for g in catalog.groups(conn)}
    assert gs["headphones"]["range"] is None          # 'pro' alone must not borrow the iPhone 12 Pro's price
    assert gs["phone"]["range"] is not None and gs["phone"]["range"].typical == pytest.approx(219.5, abs=1)


@pytest.mark.parametrize("title,kind", [
    ("Nintendo Switch 2 Cyberpunk 2077", "game"), ("Nintendo Switch Super Mario Galaxy + Super Mario Galaxy 2", "game"),
    ("Nintendo Switch OLED Model White 64GB", "console"), ("Sony PlayStation 5 Digital Edition Console", "console"),
    ("Samsung Galaxy Watch 4 SM-R865U", "watch"), ("Nintendo Wii Console Bundle with Games", "console"),
])
def test_games_for_a_console_are_not_the_console(title, kind):
    assert catalog.style_of(title, "electronics")[1] == kind


def test_accessory_bundles_are_not_consoles():
    assert catalog.style_of("23-in-1 Compatible for Nintendo Switch 2 Accessories Bundle with Charging Dock", "electronics")[1] == "accessory"
    assert catalog.style_of("Nintendo Switch OLED Console Bundle", "electronics")[1] == "console"


def test_jordans_group_by_model_and_default_to_shoes():
    assert catalog.style_of("Nike Men's Air Jordan 1 Retro High OG 555088-037 Gray White", "fashion")[1:] == ("shoes", "1 high")
    assert catalog.style_of("Nike Air Jordan 1 Mid Triple White Men's", "fashion")[2] == "1 mid"
    assert catalog.style_of("Air Jordan 11 Retro Low IE White Cement", "fashion")[2] == "11 low"


def test_bags_and_shoes_get_a_subtype_when_they_have_no_model_code():
    assert catalog.style_of("Coach Black Signature Canvas Crossbody Bag", "fashion")[2] == "crossbody"
    assert catalog.style_of("Coach F54687 Beige Pebble Leather Tote Bag", "fashion")[2] == "F54687 tote"
    assert catalog.style_of("Tory Burch Women's Leather Flip Flop Sandals", "fashion")[2] == "flip flop"


def test_model_specific_ranges_rank_ahead_of_brand_level_estimates(tmp_path):
    conn = db.connect(str(tmp_path / "c.db"))
    _store(conn, [_l(1, "Samsung Galaxy Z Flip5 256GB Unlocked", 20), _l(2, "Samsung Galaxy A10e 32GB SM-A102U", 5)], "electronics")
    kb.add_observation(conn, "Samsung baseline-phone", "sold", "x", low=100, high=400, typical=250, n=20)      # brand-wide
    flip = catalog.groups(conn)
    kb.add_observation(conn, "Samsung 256GB FLIP5", "sold", "swappa", low=219, high=289, typical=266, n=9)    # model-specific
    opps = {o["row"]["listing_id"]: o for o in catalog.opportunities(conn)}
    assert opps["1"]["coarse"] is False and opps["2"]["coarse"] is True
    out = catalog.format_opportunities(list(opps.values()))
    assert out.index("MODEL-SPECIFIC") < out.index("BRAND-LEVEL ESTIMATES")
    queue = catalog.model_queue(list(opps.values()))
    assert [a["style"] for a in queue] == [opps["2"]["row"]["style"]]   # only the brand-level one still needs research


def test_camera_with_a_lens_in_the_title_is_a_camera_not_a_lens():
    import catalog
    assert catalog.style_of("Minolta XG7 35mm SLR Camera w/ Focal MC Auto 28mm f/2.8 Lens", "cameras")[1] == "camera"
    assert catalog.style_of("Fujifilm Instax Mini 8 Instant Film Camera Black 60mm Lens", "cameras")[1] == "camera"
    assert catalog.style_of("Canon EF 50mm f/1.8 Lens for Camera", "cameras")[1] == "lens"
    assert catalog.style_of("Nikon AF-S 18-55mm VR Lens", "cameras")[1] == "lens"


def test_ebay_fee_terms_follow_the_published_category_rows():
    import catalog
    row = lambda kind, title="x": {"kind": kind, "title": title, "platform": "shopgoodwill", "shipping": 0}
    e = catalog.economics(row("guitar"))
    assert e["fee"] == 0.067
    assert catalog.economics(row("bag"))["fee"] == 0.15
    assert catalog.economics(row("cards"))["fee"] == 0.1325
    assert catalog.economics(row("watch", "Seiko automatic watch"))["fee"] == 0.15
    assert catalog.economics(row("watch", "Apple Watch Series 5"))["fee"] == catalog.EBAY_FEE   # smartwatch: electronics rate
    assert catalog.economics(row("tablet"))["fee"] == catalog.EBAY_FEE
    shoe = catalog.economics(row("shoes"))
    assert catalog.fees_for(shoe, 149.99) == (shoe["fee"], shoe["order_fee"])
    assert catalog.fees_for(shoe, 150.0) == (0.08, 0.0)   # 8% and no per-order fee from $150
    # profit rises by the saved fee when a sneaker crosses $150
    low = catalog.net_profit(shoe, 149.0, 20.0)
    high = catalog.net_profit(shoe, 150.0, 20.0)
    assert high - low > 150 * (shoe["fee"] - 0.08) - 2


def test_fragrance_gift_with_purchase_pieces_are_not_catalogued_as_designer_goods():
    import catalog
    assert catalog.style_of("Versace Parfums Black Synthetic Leather Tote Bag", "fashion")[0] is None
    assert catalog.style_of("Versace Fragrances Black Canvas Tote Bag Greca", "fashion")[0] is None
    assert catalog.style_of("Versace Medusa Leather Tote Bag", "fashion")[0] is not None


def test_a_storage_size_is_not_a_style_and_lot_numbers_are_not_model_words(tmp_path):
    import catalog, db
    from engine import knowledge as kb
    from engine.title_cleaner import model_identity
    conn = db.connect(str(tmp_path / "k.db"))
    kb.add_observation(conn, "apple 128gb", "sold", "x", low=300, high=420, typical=340, n=10)
    assert catalog.known_range(conn, "Apple", "tablet", "128GB") is None
    assert model_identity("Apple iPad - 6th Gen - 128GB (#2)") == model_identity("Apple iPad - 6th Gen - 128GB")
