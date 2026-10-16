import pytest

from config import Config
from engine.luxury_authenticator import LuxuryAuthenticator, detect_brand_and_tier
from engine.valuation import ValuationEngine

A = LuxuryAuthenticator()
ENG = ValuationEngine(Config())


@pytest.mark.parametrize("text,brand,tier", [
    ("YSL leather clutch", "saint laurent", "TIER_1_LUXURY"),
    ("Yves Saint Laurent bag", "saint laurent", "TIER_1_LUXURY"),
    ("LV monogram tote", "louis vuitton", "TIER_1_LUXURY"),
    ("Hermès Birkin", "hermes", "TIER_1_LUXURY"),
    ("Arcteryx Beta jacket", "arc'teryx", "TIER_2_STREETWEAR"),
    ("Arc'teryx Atom hoodie", "arc'teryx", "TIER_2_STREETWEAR"),
    ("Michael Kors tote bag", "michael kors", "LOW_RESALE_MARGIN"),
    ("MCM backpack", "mcm", "LOW_RESALE_MARGIN"),
    ("Burberry trench coat", "burberry", None),
    ("plain wooden chair", None, None),
])
def test_brand_and_tier(text, brand, tier):
    assert detect_brand_and_tier(text) == (brand, tier)


def test_earliest_brand_wins_and_off_white_colour_ignored():
    assert detect_brand_and_tier("Michael Kors Off White tote")[0] == "michael kors"
    assert detect_brand_and_tier("Off-White c/o Virgil Abloh hoodie")[1] == "TIER_2_STREETWEAR"


@pytest.mark.parametrize("text,code", [
    ("Certilogo code: 123456789012", "123456789012"),
    ("CLG 123 456 789 012 on tag", "123456789012"),
    ("clg-123-456-789-012", "123456789012"),
    ("code 987654321098 certilogo", "987654321098"),
])
def test_certilogo_extraction(text, code):
    assert A.find_certilogo(text) == code


def test_certilogo_ignores_unrelated_digits_and_reads_links():
    assert A.find_certilogo("Item number 123456789012 shipped from Ohio") is None
    assert A.find_certilogo("see https://www.certilogo.com/ab/xyz") is not None


def test_surfaces_in_assessment():
    a = A.assess("Stone Island jacket", "Certilogo 123456789012")
    assert a.certilogo == "123456789012" and a.tier == "verified"


def test_tier1_matrix():
    un = A.assess("Gucci leather handbag")
    assert (un.tier, un.factor, un.max_bid) == ("unverified", 0.15, 60.0)
    ver = A.assess("Gucci leather handbag", "authenticated by Entrupy")
    assert (ver.tier, ver.factor, ver.max_bid) == ("verified", 1.0, None)


def test_tier2_matrix():
    assert A.assess("Moncler puffer jacket").factor == 0.5
    assert A.assess("Moncler puffer jacket", "Certilogo 123456789012").factor == 1.0


def test_low_margin_and_generic():
    assert A.assess("Coach leather satchel bag").factor == 0.10
    assert A.assess("Kate Spade tote bag", "authenticated by Entrupy").factor == 0.10   # verification does not rescue it
    assert A.assess("Burberry trench coat").factor == 0.70


def test_counterfeit_flag_overrides_tier():
    a = A.assess("Louis Vuitton tote 1:1 mirror quality")
    assert a.high_risk_counterfeit and a.factor == 0.05
    assert A.assess("Gucci belt, not a replica").tier == "unverified"


def test_seller_coa_alone_does_not_verify():
    a = A.assess("Prada shoulder bag w/ COA")
    assert a.tier == "unverified" and any("COA" in c for c in a.concerns)


def test_engine_condition_factor_uses_matrix():
    assert ENG.condition_factor("Prada nylon backpack") == pytest.approx(0.15)
    assert ENG.condition_factor("Moncler jacket") == pytest.approx(0.50)
    assert ENG.condition_factor("Coach tote bag") == pytest.approx(0.10)
    assert ENG.condition_factor("Prada nylon backpack", "Entrupy authenticated") == pytest.approx(1.0)
    assert ENG.luxury_max_bid("Prada nylon backpack") == 60.0
    assert ENG.condition_factor("Dewalt drill") == 1.0


def test_why_explains_doubt():
    a = A.assess("Gucci leather handbag")
    assert any("counterfeited" in w for w in a.why)
    r = A.assess("Prada bag replica")
    assert any("replica" in w for w in r.why)
    assert A.assess("Prada bag", "Entrupy authenticated").why == ["no factory QA tag / NFC passport mentioned"]


def test_model_number_extraction():
    from engine.title_cleaner import extract_labelled_model, extract_models
    assert extract_labelled_model("Style #: AB1234 size M") == "ab1234"
    assert extract_labelled_model("Item number 12345678 shipped") is None   # the auction's own number, not a model
    assert extract_models("Gucci Miss GG Canvas Tote 353122 486628")[0] == "353122"


def test_false_brand_hits_are_not_flagged():
    from engine.luxury_authenticator import LuxuryAuthenticator
    a = LuxuryAuthenticator()
    for title in ("Nike Air Jordan 1 LV8D Low DH7004-101 Men's Size 9.5 Suede Sneakers",
                  "The North Face Men's Lightweight Hoodie XL Pale Beige Off-White Logo",
                  "Women's Gloria Vanderbilt Amanda Supreme Stretch Denim Jeans",
                  "Versace Parfums Black Tote Bag", "Yves Saint Laurent Beauty Womens Cosmetic Bag"):
        assert a.assess(title) is None, title
    assert a.assess("Supreme Box Logo Hoodie XL").brand == "supreme"
    assert a.assess("LV Neverfull MM tote bag").brand == "louis vuitton"


def test_handbag_evidence_levels():
    from engine.luxury_authenticator import LuxuryAuthenticator
    a = LuxuryAuthenticator()
    none = a.assess("Louis Vuitton Neverfull MM tote bag")
    assert none.evidence_level == "none" and none.factor == 0.15
    documented = a.assess("Louis Vuitton Neverfull MM tote bag", "date code SD2145, heat stamp, authenticity card, dust bag")
    assert documented.evidence_level == "documented" and documented.factor == 0.40 and documented.tier == "unverified"
    entrupy = a.assess("Dior Backpack - Doublechecked By Entrupy")
    assert entrupy.evidence_level == "third_party" and entrupy.tier == "verified" and entrupy.factor == 1.0
