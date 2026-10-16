"""Will it SELL? Liquidity estimates: how long a correctly-priced item is likely to sit.

Pricing an item is only half the question: a $200 estimate on something that sits for six months is not a
$200 flip. This module turns market data we can fetch into an expected days-to-sell range and a grade.

  sold velocity   r = sold in the last 30 days / 30                       (SoldComps / eBay sold data, with dates)
  competition     L = active listings, with STALE ones (old, probably overpriced) discounted
  months of supply   L / sold_30d        -> days ~ 30 x months of supply   (Little's law: time in system = L / throughput)
  price position  expected sale price / median sold price: below the median sells faster, above slows it sharply
  blend           with a per-category PRIOR when sold data is thin, and with YOUR realized days-to-sell when you have them

HONESTY NOTES: the category priors come from reseller-tool vendor blogs (weak), the position factors are a reasoned
heuristic, and none of it has been validated on our own sales yet. The most reliable input is `trades` (listed_at ->
sold_at): `calibration()` pulls it in as soon as it exists. Treat grades as ranking aids, not promises.
"""
import sqlite3
import statistics
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone

# Weak priors (days for a correctly priced listing to sell), from vendor blogs: electronics 10-21d, sneakers ~9d,
# tablets 41d, phones+accessories 60d, Coach bags 54d, mixed fashion mostly 40-90d.
PRIOR_DAYS = {
    "phone": 30, "tablet": 41, "laptop": 28, "desktop": 35, "console": 14, "game": 14, "gamepad": 21, "headphones": 21,
    "speaker": 28, "tv": 35, "monitor": 35, "camera": 35, "lens": 35, "watch": 30, "ipod": 28, "audio": 35, "printer": 45,
    "drone": 28, "vr": 28, "accessory": 45, "bag": 54, "wallet": 60, "shoes": 30, "clothing": 50, "guitar": 40, "amp": 35,
    "pedal": 25, "keys": 45, "drill": 21, "saw": 25, "toy": 21, "cards": 21, "figure": 30,
}
DEFAULT_PRIOR_DAYS = 35
STALE_DAYS = 60
MARKDOWN_SCHEDULE = ((22, 0.15), (43, 0.30), (61, 0.50), (91, 0.70))  # (days listed >=, % off); vendor illustration


@dataclass
class MarketStats:
    item_key: str
    source: str
    sold_30d: int | None = None
    sold_90d: int | None = None
    active: int | None = None
    stale_active: int | None = None  # active listings older than STALE_DAYS (None when the source can't tell)
    median_sold: float | None = None
    median_ask: float | None = None
    avg_watchers: float | None = None
    observed_at: int = field(default_factory=lambda: int(time.time()))

    def str_reseller(self) -> float | None:
        """sold / active over 30 days (can exceed 100%)."""
        return self.sold_30d / self.active if self.active and self.sold_30d is not None else None

    def str_retail(self) -> float | None:
        """sold / (sold + active) over 30 days."""
        if self.sold_30d is None or self.active is None or (self.sold_30d + self.active) == 0:
            return None
        return self.sold_30d / (self.sold_30d + self.active)

    def ask_premium(self) -> float | None:
        """How far above the median sale price sellers are asking (1.3 = 30% over): high means a stale, overpriced market."""
        return self.median_ask / self.median_sold if self.median_ask and self.median_sold else None


@dataclass
class DaysEstimate:
    days: float
    low: float
    high: float
    basis: str  # prior | market | blended | calibrated
    reliable: bool  # enough real sales behind it to act on
    notes: list[str] = field(default_factory=list)

    @property
    def grade(self) -> str:
        return grade_for(self.days)

    def text(self) -> str:
        return f"grade {self.grade}: ~{self.days:.0f} days ({self.low:.0f}-{self.high:.0f}), {self.basis}" + (
            "" if self.reliable else ", LOW CONFIDENCE")


def grade_for(days: float | None) -> str:
    if days is None:
        return "?"
    return "A" if days <= 14 else "B" if days <= 30 else "C" if days <= 60 else "D"


def position_factor(price_position: float | None) -> float:
    """Selling below the median sells faster; above it slows sharply (sold prices cluster at market, asks run higher)."""
    if price_position is None:
        return 1.0
    return 0.5 if price_position <= 0.85 else 0.8 if price_position <= 1.0 else 1.0 if price_position <= 1.1 else \
        1.6 if price_position <= 1.25 else 2.5


def prior_days(kind: str | None) -> float:
    return float(PRIOR_DAYS.get(kind or "", DEFAULT_PRIOR_DAYS))


def expected_days(stats: MarketStats | None, kind: str | None = None, expected_price: float | None = None,
                  calibrated: tuple[float, int] | None = None) -> DaysEstimate:
    """Expected days to sell at `expected_price`, from market data when it exists, else the category prior.

    calibrated = (median realized days, number of your own trades), blended in as it grows.
    """
    prior = prior_days(kind)
    notes: list[str] = []
    pos = (expected_price / stats.median_sold) if stats and stats.median_sold and expected_price else None
    market = None
    if stats is not None and stats.active:
        sold30 = stats.sold_30d
        rate = (sold30 / 30) if sold30 else ((stats.sold_90d or 0) / 90)
        if rate > 0:
            effective = max(stats.active - 0.7 * (stats.stale_active or 0), 1.0)
            market = effective / rate * position_factor(pos)
            if stats.stale_active:
                notes.append(f"{stats.stale_active} of {stats.active} active listings are stale and were discounted")
        else:
            market = 365.0
            notes.append("no sales found in the last 90 days")
        n_sales = max(sold30 or 0, (stats.sold_90d or 0) // 3)
        if rate == 0 and stats.median_sold is not None:
            n_sales = 5  # sales exist but none recently: a dead market is strong, reliable evidence, not an absence of data
    else:
        n_sales = 0
    if pos is not None and pos > 1.1:
        notes.append(f"expected price is {pos:.0%} of the median sale price: expect a slower sale")
    if market is None:
        days, basis, reliable = prior, "prior", False
        notes.append("no market data: category prior from vendor blogs (weak)")
    else:
        w = n_sales / (n_sales + 3)  # a few sales mean little: lean on the prior until there are more
        days = w * market + (1 - w) * prior
        basis, reliable = ("market" if w > 0.7 else "blended"), n_sales >= 5
    if calibrated and calibrated[1] > 0:
        cw = calibrated[1] / (calibrated[1] + 5)
        days = cw * calibrated[0] + (1 - cw) * days
        basis, reliable = "calibrated", reliable or calibrated[1] >= 5
        notes.append(f"blended with your {calibrated[1]} realized sale(s), median {calibrated[0]:.0f} days")
    days = max(1.0, min(days, 365.0))
    return DaysEstimate(days, max(1.0, days * 0.6), min(365.0, days * 1.8), basis, reliable, notes)


def carry_cost(buy_cost: float, days: float, annual_rate: float = 0.25) -> float:
    """Cost of money (and space, and risk) tied up while it sits. 25%/yr is a vendor-quoted 20-30%; set your own."""
    return round(buy_cost * annual_rate * days / 365, 2)


def suggested_markdown(days_listed: float, schedule=MARKDOWN_SCHEDULE) -> tuple[float, str]:
    """(fraction off the list price, label) for an item that has been listed `days_listed` days."""
    pct = 0.0
    for after, p in schedule:
        if days_listed >= after:
            pct = p
    label = ("hold price" if pct == 0 else f"take {pct:.0%} off" if days_listed < schedule[-1][0] else
             f"take {pct:.0%} off or bundle/liquidate")
    return pct, label


# ---------------------------------------------------------------- building stats from raw data
def _median(xs: list[float]) -> float | None:
    return statistics.median(xs) if xs else None


def build_stats(item_key: str, source: str, sold: list[tuple[float, datetime | None]], active: list[dict] | None,
                now: datetime | None = None) -> MarketStats:
    """sold = [(price, sold_date)]; active = [{'price', 'watchers', 'created': datetime|None}] or None if unavailable."""
    now = now or datetime.now(timezone.utc)

    def within(days: int) -> int:
        return sum(1 for _, d in sold if d is not None and (now - (d if d.tzinfo else d.replace(tzinfo=timezone.utc))).days <= days)

    stats = MarketStats(item_key, source, sold_30d=within(30), sold_90d=within(90),
                        median_sold=_median([p for p, _ in sold]))
    if active is not None:
        stats.active = len(active)
        stats.median_ask = _median([a["price"] for a in active if a.get("price")])
        watchers = [a["watchers"] for a in active if a.get("watchers") is not None]
        stats.avg_watchers = round(statistics.fmean(watchers), 1) if watchers else None
        created = [a["created"] for a in active if a.get("created")]
        if created:  # only meaningful when the source reports listing age
            stats.stale_active = sum(1 for c in created if (now - (c if c.tzinfo else c.replace(tzinfo=timezone.utc))).days > STALE_DAYS)
    return stats


# ---------------------------------------------------------------- persistence + calibration from YOUR trades
def save_stats(conn: sqlite3.Connection, s: MarketStats, replace_within_hours: float = 24) -> None:
    row = conn.execute("SELECT id FROM market_stats WHERE item_key=? AND source=? AND observed_at>=? ORDER BY observed_at DESC LIMIT 1",
                       (s.item_key, s.source, s.observed_at - replace_within_hours * 3600)).fetchone()
    vals = (s.sold_30d, s.sold_90d, s.active, s.stale_active, s.median_sold, s.median_ask, s.avg_watchers, s.observed_at)
    if row:
        conn.execute("UPDATE market_stats SET sold_30d=?, sold_90d=?, active=?, stale_active=?, median_sold=?, median_ask=?, "
                     "avg_watchers=?, observed_at=? WHERE id=?", (*vals, row[0]))
    else:
        conn.execute("INSERT INTO market_stats (item_key, source, sold_30d, sold_90d, active, stale_active, median_sold, "
                     "median_ask, avg_watchers, observed_at) VALUES (?,?,?,?,?,?,?,?,?,?)", (s.item_key, s.source, *vals))
    conn.commit()


def latest_stats(conn: sqlite3.Connection, item_key: str) -> MarketStats | None:
    r = conn.execute("SELECT * FROM market_stats WHERE item_key=? ORDER BY observed_at DESC LIMIT 1", (item_key,)).fetchone()
    if not r:
        return None
    return MarketStats(r["item_key"], r["source"], r["sold_30d"], r["sold_90d"], r["active"], r["stale_active"],
                       r["median_sold"], r["median_ask"], r["avg_watchers"], r["observed_at"])


def calibration(conn: sqlite3.Connection, item_key: str | None = None) -> tuple[float, int] | None:
    """(median realized days listed->sold, count) from YOUR trades; for one item if given (needs >=1), else everything (>=3)."""
    sql = "SELECT sold_at - listed_at AS secs FROM trades WHERE sold_at IS NOT NULL AND listed_at IS NOT NULL AND sold_at >= listed_at"
    args: list = []
    if item_key:
        sql += " AND item_key = ?"
        args.append(item_key)
    days = [r["secs"] / 86400 for r in conn.execute(sql, args).fetchall()]
    if not days or (item_key is None and len(days) < 3):
        return None
    return statistics.median(days), len(days)
