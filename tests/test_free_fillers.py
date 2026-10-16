import free_comp_fillers as ff

IPAD = '<p>Average price: $365</p><table><tr><th>Storage</th><th>Avg list price</th><th>Avg sale price</th></tr><tr><td>64 GB</td><td>$360</td><td>$314</td></tr><tr><td>256 GB</td><td>$399</td><td>$368</td></tr></table>'
MBP = '<p>Average price: $901</p> Processor / Storage Avg list price Avg sale price Apple M1 Pro 10-core / 512 GB $902 $730 Apple M1 Max / 1 TB $1288 $1265'


def test_swappa_parses_storage_row_chip_row_and_headline():
    assert ff.parse_swappa(IPAD, 64) == 314.0
    assert ff.parse_swappa(IPAD, 256) == 368.0
    assert ff.parse_swappa(IPAD) == 365.0
    assert ff.parse_swappa(MBP, 512, "m1 pro") == 730.0
    assert ff.parse_swappa(MBP, 1000, "m1 max") == 1265.0
    assert ff.parse_swappa("<html>nothing</html>", 64) is None


def test_spec_matrix_floors_modifiers_and_decay():
    assert ff.evaluate_laptop_by_spec_matrix("dell", "intel11", 16, 512, 2022) == 235.0  # 180 + 30 + 25, no decay in 2022
    assert ff.evaluate_laptop_by_spec_matrix("dell", "intel10", 32, 1000, 2020) == round((120 + 70 + 60) * 0.88 ** 2, 2)
    assert ff.evaluate_laptop_by_spec_matrix("dell", "m1", 8, 256, 2022) == 420.0
    assert ff.evaluate_laptop_by_spec_matrix("dell", "intel8", 16, 512, 2018) == 0.0  # not in the matrix


def test_cpu_family_detection():
    assert ff.cpu_family_from_text("Latitude i7-1185G7") == "intel11"
    assert ff.cpu_family_from_text("EliteBook i5-10210U") == "intel10"
    assert ff.cpu_family_from_text("Latitude i5-8365U") is None
    assert ff.cpu_family_from_text("ThinkPad Ryzen 7 5800U") == "ryzen5000"


def test_metal_melt_math_with_fallback_spot(monkeypatch):
    monkeypatch.setattr(ff, "spot_price_gram_24k", lambda metal="gold", http=None: (133.0, "fallback benchmark"))
    r = ff.calculate_precious_metal_melt(10, "gold", 18)
    assert r["melt_value"] == round(10 * 0.75 * 133.0, 2) and r["purity_pct"] == 75.0
    s = ff.calculate_precious_metal_melt(100, "silver", purity_fraction=0.925)
    assert s["melt_value"] == round(100 * 0.925 * 133.0, 2)
