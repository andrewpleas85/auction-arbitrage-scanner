"""Bid escalation for watchlisted auctions.

A watchlisted auction was valued at the bid we saw. The ending loop only re-values it inside the
final window, so on a $1 lot we'd otherwise ignore a bidding war for hours. This decides when a
scan should re-run the profit math sooner: when the current bid has CROSSED a configured dollar
threshold since we last valued it, or has reached (a fraction of) 50% of the estimated resale value.
Only upward moves matter: a bid never falls.
"""


def parse_thresholds(raw: list[str] | tuple[float, ...]) -> tuple[float, ...]:
    return tuple(sorted(float(x) for x in raw if str(x).strip()))


def bid_escalation(prev_bid: float | None, cur_bid: float, est_value: float | None,
                   thresholds: tuple[float, ...] = (10, 25, 50), value_fraction: float = 0.5,
                   approach: float = 0.9) -> str | None:
    """Why this auction should be re-valued now, or None.

    prev_bid is the bid at the last valuation (what we stored). Triggers, in order:
      * cur_bid crossed one of `thresholds` (prev < t <= cur);
      * cur_bid reached `approach` x (`value_fraction` x est_value), i.e. nearing 50% of resale value.
    """
    if prev_bid is None or cur_bid <= prev_bid:
        return None
    for t in thresholds:
        if prev_bid < t <= cur_bid:
            return f"bid crossed ${t:g} (${prev_bid:,.2f} -> ${cur_bid:,.2f})"
    if est_value and est_value > 0:
        line = est_value * value_fraction * approach
        if prev_bid < line <= cur_bid:
            return (f"bid ${cur_bid:,.2f} is nearing {value_fraction:.0%} of est. value "
                    f"${est_value:,.2f}")
    return None
