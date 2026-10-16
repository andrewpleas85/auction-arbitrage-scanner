from engine.lots import lot_quantity, lot_value


def test_quantity_forms():
    assert lot_quantity("This lot contains ten laptops with hard drives removed.") == 10
    assert lot_quantity("(10) laptops: (5) HP ProBook 630 G8 and (5) HP EliteBook x360") == 10
    assert lot_quantity("This lot contains (7) HP ProBook 630 G8 laptops and (3) HP EliteBook x360 laptops.") == 10
    assert lot_quantity("(1) LOT OF DELL LATITUDE 7410 LAPTOPS. 1 Lot of 15 Dell Latitude 7410 workstations.") == 15
    assert lot_quantity("a single laptop") is None


def test_lot_value_scales_by_units_and_flags_removed_drives():
    v = lot_value("HP ProBook 630 G8 laptop. This lot contains ten laptops with hard drives removed.")
    assert v["qty"] == 10 and v["typ"] == 1900 and v["drives_removed"]
    assert lot_value("ten laptops of unknown make") is None   # no known model: no value guessed


def test_models_weighted_by_stated_counts():
    one_nine = lot_value("This lot contains ten laptops: one HP ProBook 630 G8 and nine HP EliteBook x360 convertible notebooks.")
    assert one_nine["typ"] == 190 + 9 * 330
    split = lot_value("This lot contains (7) HP ProBook 630 G8 laptops and (3) HP EliteBook x360 laptops.")
    assert split["typ"] == 7 * 190 + 3 * 330
    even = lot_value("(10) laptops: (5) HP ProBook 630 G8 and (5) HP EliteBook x360")
    assert even["typ"] == 5 * 190 + 5 * 330


def test_more_models_and_big_lots():
    v = lot_value("This lot contains 99 Dell 5400 laptops. Hard Drive Status: Removed")
    assert v["qty"] == 99 and v["models"] == ["Dell Latitude 5400"] and v["drives_removed"]
    assert lot_value("This lot contains (48) HP EliteBook 840 G3 laptops with power cords.")["qty"] == 48
    assert lot_value("This lot contains (6) Dell Latitude laptop computers. Model: Latitude E5570")["qty"] == 6
    assert lot_value("This lot contains 38 Dell and HP laptops in repairable condition.") is None
