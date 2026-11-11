import pytest

import sell_channels as sc


def test_swappa_models_both_the_lower_fee_and_the_lower_price():
    o = sc.options("phone", "Apple iPhone 13", 500.0, 7.0)["Swappa"]
    assert o["sale_price"] == pytest.approx(500 * 0.90) and o["fee"] == pytest.approx(450 * 0.065) and o["revenue"] == pytest.approx(450 - 450 * 0.065 - 7.0, abs=0.01)
    assert "Swappa" not in sc.options("shoes", "Nike Air Max", 500.0, 7.0) and "stated" in o["price_basis"]


def test_depop_is_clothing_only_with_only_processing_fees():
    d = sc.options("clothing", "Patagonia fleece", 100.0, 10.0)["Depop"]
    assert d["fee"] == pytest.approx(90 * 0.033 + 0.45, abs=0.01)
    assert "Depop" not in sc.options("laptop", "Dell", 100.0, 10.0)


def test_mercari_charges_on_price_plus_buyer_paid_shipping_so_heavy_items_cost_more():
    light = sc.options("other", "thing", 200.0, 10.0)["Mercari"]
    heavy = sc.options("other", "thing", 200.0, 60.0)["Mercari"]
    assert light["ship_mode"] == "buyer" and light["fee"] == pytest.approx(0.10 * 180, abs=0.01)  # fee on (price + shipping) = the buyer's total
    assert heavy["revenue"] < light["revenue"] - 40                                              # the heavier label comes out of the item price


def test_chrono24_watch_only_and_grailed_tiered_fee():
    assert "Chrono24" in sc.options("watch", "Omega Seamaster", 1000.0, 7.0) and "Chrono24" not in sc.options("bag", "Prada", 1000.0, 7.0)
    assert sc.options("clothing", "x", 100.0, 5.0)["Grailed"]["fee"] == pytest.approx(90 * (0.06 + 0.0349), abs=0.01)    # sale 90 < $120: 6%
    assert sc.options("clothing", "x", 200.0, 5.0)["Grailed"]["fee"] == pytest.approx(180 * (0.09 + 0.0349), abs=0.01)   # 9%


def test_stockx_for_sneakers_and_streetwear_titles_and_sweetwater_for_gear():
    assert "StockX" in sc.options("shoes", "Air Jordan 4", 300.0, 15.0) and "StockX" in sc.options("clothing", "Supreme box logo hoodie", 300.0, 10.0)
    assert "StockX" not in sc.options("clothing", "Plain blazer", 300.0, 10.0)
    assert sc.options("guitar", "Fender Strat", 800.0, 40.0)["Sweetwater Gear Exchange"]["fee"] == pytest.approx(720 * 0.075, abs=0.01)


def test_local_and_live_variants_are_shown_but_not_counted():
    o = sc.options("other", "thing", 300.0, 20.0)
    assert o["eBay Live"]["counts"] is False and o["OfferUp (local)"]["counts"] is False and o["OfferUp (local)"]["ship_mode"] == "none"
    assert o["eBay Live"]["fee"] == pytest.approx(300 * 0.089)


def test_skipped_channels_are_not_in_the_registry():
    names = " ".join(sc.REGISTRY).lower()
    for gone in ("back market", "1stdibs", "amazon renewed", "decluttr", "guitar center"):
        assert gone not in names


def test_buyout_floors_for_bags_and_watches_only_and_the_signal():
    f = sc.buyout_floors("bag", "Louis Vuitton Speedy", 1000.0)
    assert f[0]["name"] == "Rebag buyout" and (f[0]["low"], f[0]["high"]) == (400.0, 700.0)
    w = sc.buyout_floors("watch", "Omega", 1000.0)
    assert len(w) == 2 and w[1]["low"] is None and "no rate" in w[1]["source"]       # dealer rate not supplied: named, not guessed
    assert sc.buyout_floors("laptop", "Dell", 1000.0) == []
    assert sc.floor_signal(150.0, 0.5, 100.0, 0.4) and not sc.floor_signal(90.0, 0.5, 100.0, 0.4) and not sc.floor_signal(None, None, 100.0, 0.4)
