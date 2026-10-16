import time

import pytest

import db
from engine import knowledge as kb

DAY = 86400
NOW = int(time.time())


@pytest.fixture
def conn(tmp_path):
    return db.connect(str(tmp_path / "k.db"))


def test_item_key_normalizes():
    assert kb.item_key("Louis Vuitton Keepall 55 Damier Azur") == "louis vuitton keepall 55 damier azur"
    assert kb.item_key("  The AUTHENTIC Louis Vuitton Keepall 55, Damier Azur bag ") == "louis vuitton keepall 55 damier azur"


def test_range_from_sold_data_and_updates_as_evidence_changes(conn):
    kb.add_observation(conn, "iPhone 12 Pro 128GB", "sold", "swappa", low=216, high=223, n=50)
    r = kb.compute_range(conn, kb.item_key("iPhone 12 Pro 128GB"))
    assert (r.low, r.high, r.basis, r.confidence) == (216, 223, "sold", "B")
    # a second independent source agrees: confidence rises; the range widens to cover both
    kb.add_observation(conn, "iPhone 12 Pro 128GB", "sold", "ebay", low=190, high=240, n=12)
    r = kb.compute_range(conn, kb.item_key("iPhone 12 Pro 128GB"))
    assert (r.low, r.high, r.confidence) == (190, 240, "A")
    stored = conn.execute("SELECT low, high, confidence FROM value_ranges").fetchone()
    assert tuple(stored) == (190, 240, "A")  # the stored range tracks the evidence


def test_asking_prices_are_discounted_and_graded_c(conn):
    kb.add_observation(conn, "Dior Oblique backpack", "ask", "ebay", low=750, high=3000, typical=1800, n=10)
    r = kb.compute_range(conn, kb.item_key("Dior Oblique backpack"))
    assert (r.low, r.typical, r.high) == (600, 1440, 2400) and r.confidence == "C" and r.basis == "ask"
    assert any("discounted" in n for n in r.notes)


def test_sold_beats_ask_when_both_exist(conn):
    kb.add_observation(conn, "Fluke 87V", "ask", "ebay", low=300, high=500, typical=400)
    kb.add_observation(conn, "Fluke 87V", "sold", "soldcomps", low=250, high=330, typical=290, n=20)
    r = kb.compute_range(conn, kb.item_key("Fluke 87V"))
    assert r.basis == "sold" and r.typical == 290


def test_new_retail_caps_the_estimate(conn):
    kb.add_observation(conn, "Sony WH-1000XM5", "sold", "soldcomps", low=250, high=420, typical=380, n=9)
    kb.add_observation(conn, "Sony WH-1000XM5", "new_retail", "bestbuy", typical=400)
    r = kb.compute_range(conn, kb.item_key("Sony WH-1000XM5"))
    assert r.typical == pytest.approx(340) and r.high <= 340 and r.retail == 400
    assert any("capped" in n for n in r.notes)


def test_retail_only_has_no_value(conn):
    kb.add_observation(conn, "Hobart HL200", "new_retail", "burkett", typical=8346)
    r = kb.compute_range(conn, kb.item_key("Hobart HL200"))
    assert r.typical is None and r.confidence == "D" and r.retail == 8346


def test_old_evidence_ages_out_and_history_is_kept(conn):
    old = NOW - 400 * DAY
    kb.add_observation(conn, "Widget", "sold", "ebay", typical=100, observed_at=old)
    assert kb.compute_range(conn, "widget") is None  # outside the 180-day window
    kb.add_observation(conn, "Widget", "sold", "ebay", typical=150)
    r = kb.compute_range(conn, "widget")
    assert r.typical == 150
    assert len(kb.observations(conn, "widget")) == 2          # nothing was overwritten
    assert kb.compute_range(conn, "widget", as_of=old + DAY).typical == 100  # "what did we think back then"


def test_newest_observation_per_source_wins(conn):
    kb.add_observation(conn, "Widget", "sold", "ebay", typical=100, observed_at=NOW - 20 * DAY)
    kb.add_observation(conn, "Widget", "sold", "ebay", typical=130)
    assert kb.compute_range(conn, "widget").typical == 130


def test_stale_flag(conn):
    kb.add_observation(conn, "Widget", "sold", "ebay", typical=100, observed_at=NOW - 120 * DAY)
    r = kb.compute_range(conn, "widget")
    assert r.stale and any("days old" in n for n in r.notes) and kb.grade("sold", r).endswith("stale")


def test_replace_within_window_does_not_spam_duplicates(conn):
    for p in (100, 110, 120):
        kb.add_observation(conn, "Widget", "sold", "soldcomps", typical=p, replace_within_hours=24)
    assert len(kb.observations(conn, "widget")) == 1 and kb.compute_range(conn, "widget").typical == 120


def test_trade_creates_realized_evidence(conn):
    kb.add_observation(conn, "Widget", "ask", "ebay", typical=200)
    kb.add_trade(conn, "Widget", buy_price=60, sell_price=140, channel="ebay", sell_fees=20)
    r = kb.compute_range(conn, "widget")
    assert r.basis == "realized" and r.typical == 140
    t = conn.execute("SELECT buy_price, sell_price, channel FROM trades").fetchone()
    assert tuple(t) == (60, 140, "ebay")


def test_find_items_matches_loosely(conn):
    kb.add_observation(conn, "Louis Vuitton Keepall Bandouliere 55 Damier Azur", "ask", "ebay", low=1100, high=1250)
    kb.add_observation(conn, "Dior Oblique backpack", "ask", "ebay", typical=1800)
    assert kb.find_items(conn, "keepall azur") == [kb.item_key("Louis Vuitton Keepall Bandouliere 55 Damier Azur")]
    assert kb.find_items(conn, "dior backpack") == ["dior oblique backpack"]
    assert kb.find_items(conn, "gucci") == []


def test_cross_check_and_grade(conn):
    kb.add_observation(conn, "Widget", "sold", "ebay", low=80, high=120, typical=100, n=8)
    r = kb.compute_range(conn, "widget")
    assert kb.cross_check(r, 105) is None and kb.cross_check(None, 500) is None
    assert "ABOVE" in kb.cross_check(r, 400) and "below" in kb.cross_check(r, 30)
    assert kb.grade("sold", r) == "B" and kb.grade("active", None) == "C" and kb.grade("mock", r) == "TEST"
    assert kb.grade("melt", None).startswith("melt")


def test_bad_inputs(conn):
    with pytest.raises(ValueError):
        kb.add_observation(conn, "x", "bogus", "s", typical=1)
    with pytest.raises(ValueError):
        kb.add_observation(conn, "x", "ask", "s")


def test_a_couple_of_sales_do_not_outrank_many_listings(conn):
    kb.add_observation(conn, "Louboutin So Kate", "ask", "ebay", low=325, high=645, typical=400, n=8)
    kb.add_observation(conn, "Louboutin So Kate", "sold", "ebay", typical=150, n=1)
    r = kb.compute_range(conn, "louboutin so kate")
    assert r.basis == "ask" and r.typical == pytest.approx(320) and r.low == 150 and r.confidence == "C"
    assert any("only 1 sold sample" in n for n in r.notes)
    kb.add_observation(conn, "Louboutin So Kate", "sold", "poshmark", typical=380, n=4)  # now 5 sales: sold data takes over
    r = kb.compute_range(conn, "louboutin so kate")
    assert r.basis == "sold" and r.confidence in ("A", "B")


def test_realized_trades_always_count_even_when_few(conn):
    kb.add_observation(conn, "Widget", "ask", "ebay", typical=300, n=20)
    kb.add_trade(conn, "Widget", 50, 120, channel="ebay")
    assert kb.compute_range(conn, "widget").basis == "realized"
