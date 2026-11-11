import pytest

import identity
import repair


def test_classes():
    c = repair.classify("Apple iPhone 13", "Cracked screen but works great", "phone")
    assert c["class"] == "repairable" and c["fix"] == "screen" and c["repair_cost"] == 110.0 and c["repair_days"] == repair.REPAIR_DAYS
    assert repair.classify("Dell Latitude 7420", "bad battery, needs a new battery", "laptop")["repair_cost"] == 90.0
    assert repair.classify("Apple iPhone 13", "cracked screen, won't turn on", "phone")["class"] == "untested_unknown"
    assert repair.classify("Marantz SR6008 receiver", "untested, no power cable", "other")["class"] == "untested_unknown"
    assert repair.classify("Samsung TV", "for parts only", "tv")["class"] == "parts_only"
    assert repair.classify("Denon receiver", "light scratches on top", "other")["class"] == "cosmetic"
    assert repair.classify("Denon receiver", "works great", "other")["class"] == "working"


def test_damage_without_a_repair_estimate_is_not_guessed():
    c = repair.classify("Canon EOS R6", "cracked screen", "camera")
    assert c["class"] == "untested_unknown" and "no repair estimate" in c["reason"] and c["repair_cost"] == 0.0


def test_paths_price_untested_at_parts_value_and_show_the_weighting():
    p = repair.paths(400.0, repair.classify("x", "untested", "other"))
    assert p["as_is_value"] == pytest.approx(400 * repair.PARTS_FACTOR) and p["repaired_value"] is None
    assert p["prob_weighted"] == pytest.approx(0.5 * 400 + 0.5 * 120)
    q = repair.paths(400.0, repair.classify("Apple iPhone", "cracked screen", "phone"))
    assert q["repaired_value"] == 400.0 and q["as_is_value"] == pytest.approx(120.0)


def test_max_buy_repair_clears_both_repair_bars():
    for rev in (200.0, 400.0, 900.0):
        m = repair.max_buy_repair(rev, 90.0, 75.0, 0.5)
        net = rev - 90.0 - m
        assert net >= 100.0 - 0.01 and net / (m + 90.0) >= 0.5 - 1e-6


# identity unit cases: the title claims one model, the text reveals another
def test_title_model_vs_description_model():
    r = identity.check("Marantz SR7002/U1B receiver", "This is actually the SR6008")
    assert r["confidence"] == "low" and r["contradictions"] and "SR6008" in r["contradictions"][0]


def test_variant_suffix_is_the_same_model():
    assert identity.check("Marantz SR7002/U1B receiver", "Model SR7002U1B 7.1 receiver")["verified"]


def test_spec_conflicts_between_title_and_description():
    r = identity.check("MacBook Pro 13 M1 16GB 512GB", "M2 chip, 256GB")
    assert not r["verified"] and len(r["contradictions"]) == 2


def test_lens_mount_conflict_and_unverifiable_title():
    assert identity.check("Sigma 35mm lens for Canon EF", "fits Sony E-mount bodies")["contradictions"]
    r = identity.check("Marantz receiver", "great sound")
    assert r["confidence"] == "low" and "unverifiable" in r["reason"]


def test_specs_parse_from_string_and_json():
    assert identity.parse_specs("storage=256;chip=m1") == {"storage": "256", "chip": "m1"}
    assert identity.parse_specs('{"Storage_GB": 256}') == {"storage_gb": 256}


def test_batch_prices_damaged_lots_instead_of_dropping_them():
    import batch_evaluate as be
    title = "Apple MacBook Pro 14 M1 Pro 16GB 512GB cracked screen"
    assert be.classify(title, "laptop") is None                       # the junk filter still drops it by default
    assert be.classify(title, "laptop", allow_damaged=True) == ("macbook pro", "macbook pro")  # but the damaged path can price it
    assert be.PART_LISTING.search("MacBook Pro LCD Display Assembly") and not be.PART_LISTING.search(title)
