from datetime import datetime, timedelta, timezone

import pytest

import evidence as E

NOW = datetime(2026, 11, 11, tzinfo=timezone.utc)


def test_bar_constants_follow_the_ruling():
    assert (E.MIN_NET, E.MIN_RETURN, E.REPAIR_CUSHION, E.THIN_NET) == (100.0, 0.40, 25.0, 150.0)


def test_tier_n_at_least_10_values_at_the_median():
    t = E.value_tier([100 + i for i in range(12)])
    assert t["tier"].startswith("median") and t["value"] == t["median"] == 105.5 and t["net_bar"] == 100.0 and t["verifiable"]


def test_tier_5_to_9_values_at_the_25th_percentile():
    t = E.value_tier([100, 110, 120, 130, 140, 150, 160])
    assert t["value"] == t["p25"] == 115.0 and t["value"] < t["median"] and t["net_bar"] == 100.0 and t["verifiable"]


def test_tier_3_to_4_uses_p25_and_raises_the_net_bar_but_is_not_verifiable_at_5():
    t = E.value_tier([100, 110, 120, 130])
    assert t["value"] == t["p25"] and t["net_bar"] == 150.0 and not t["verifiable"] and "bar raised" in t["tier"]


def test_fewer_than_3_comps_give_no_value():
    assert E.value_tier([100, 200])["value"] is None and E.value_tier([])["value"] is None and E.value_tier([100, 200])["tier"] == "none"


def test_wide_spread_forces_the_25th_percentile_even_with_many_comps():
    prices = [20, 30, 40, 100, 120, 200, 220, 300, 320, 400, 420, 500]  # IQR/median > 0.5
    t = E.value_tier(prices)
    assert t["wide"] and t["iqr_ratio"] > 0.5 and t["value"] == t["p25"] < t["median"] and "wide spread" in t["tier"]
    assert not E.value_tier([100 + i for i in range(12)])["wide"]


def sales(days):
    return [(f"t{i}", 100.0, (NOW - timedelta(days=d)).isoformat()) for i, d in enumerate(days)]


def test_velocity_comes_from_comp_dates_and_time_to_sell_is_unknown_without_active_listings():
    d = E.demand(sales([5, 20, 40, 80, 200]), NOW)
    assert d["sold_30d"] == 2 and d["sold_90d"] == 4 and d["sold_per_30d"] == pytest.approx(1.33, abs=0.01)
    assert d["p_sell_90d"] is None and d["time_to_sell"].startswith("unknown")


def test_chance_of_selling_within_90_days_only_with_an_active_count():
    d = E.demand(sales([5, 20, 40, 80]), NOW, active=3)
    assert d["active_listings"] == 3 and 0 < d["p_sell_90d"] < 1 and "model estimate" in d["time_to_sell"]
    busy = E.demand(sales([5, 20, 40, 80]), NOW, active=30)
    assert busy["p_sell_90d"] < d["p_sell_90d"]  # more competing listings, lower chance for one more
