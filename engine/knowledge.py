"""Price knowledge base: what things are worth, as a RANGE, kept current as new evidence arrives.

Observations are append-only history (one row = "source X saw prices LOW..HIGH for item Y on date D"). The current
value range of an item is recomputed from the newest observation per (kind, source) inside a recency window, so
old evidence ages out and new evidence shifts the range. Nothing is ever overwritten, so "what did we think in
March" (`as_of`) still works. `value_ranges` stores the latest computed range for fast lookups.

Kinds, strongest first:
  realized    what YOU actually sold it for           (best evidence)
  sold        completed sales on a marketplace         (SoldComps, eBay sold, Poshmark sold, Swappa averages...)
  ask         what sellers are asking                   (discounted before use: asks overstate what items sell for)
  new_retail  price new, from a store                   (a CEILING on a used value, never a value itself)
  melt        precious-metal melt value                 (objective, per listing; shown but not item-keyed)

Confidence: A = sold/realized data confirmed by a second independent source; B = sold/realized from one source;
C = asking prices only; D = only a retail ceiling (no value evidence). Marked "stale" when the newest evidence
is old.
"""
import re
import sqlite3
import statistics
import time
from dataclasses import dataclass, field

from engine.matching import clean_title

KINDS = ("realized", "sold", "ask", "new_retail", "melt")
_STOP = {"the", "a", "an", "and", "with", "for", "of", "in", "new", "used", "authentic", "genuine", "vintage",
         "pre", "owned", "preowned", "size", "bag", "purse", "handbag", "shoes", "shoe", "women", "womens",
         "mens", "men", "womans", "ladies", "lot", "set"}


def item_key(text: str) -> str:
    """Stable lowercase key from a title or a typed name: 'Louis Vuitton Keepall 55 Damier Azur' -> same words, no fluff."""
    words = re.findall(r"[a-z0-9]+(?:[-'][a-z0-9]+)*", (text or "").lower())
    return " ".join(w for w in words if w not in _STOP)[:160]


def _tokens(key: str) -> set[str]:
    return set(key.split())


@dataclass
class ValueRange:
    item_key: str
    low: float | None
    typical: float | None
    high: float | None
    confidence: str
    basis: str  # which kind of evidence the range rests on
    n_obs: int
    newest_days: float | None  # age of the newest evidence
    notes: list[str] = field(default_factory=list)
    retail: float | None = None  # cheapest known NEW price, if any
    stale: bool = False

    def text(self) -> str:
        if self.typical is None:
            return f"no value evidence yet" + (f" (new retail ${self.retail:,.0f})" if self.retail else "")
        return (f"${self.low:,.0f} to ${self.high:,.0f}, typically ${self.typical:,.0f} "
                f"[{self.confidence}{' STALE' if self.stale else ''}, {self.basis}, {self.n_obs} source(s)]")


# ------------------------------------------------------------------ writes
def add_observation(conn: sqlite3.Connection, name: str, kind: str, source: str, *, low: float | None = None,
                    high: float | None = None, typical: float | None = None, n: int = 1,
                    condition: str | None = None, url: str | None = None, notes: str | None = None,
                    observed_at: int | None = None, replace_within_hours: float = 0,
                    refresh: bool = True) -> int:
    """Record what a source saw. With `replace_within_hours`, a repeat from the same (item, kind, source) inside that
    window updates the existing row instead of adding a near-duplicate (used for automatic scanner feeds)."""
    if kind not in KINDS:
        raise ValueError(f"kind must be one of {KINDS}")
    vals = [v for v in (low, typical, high) if v is not None]
    if not vals:
        raise ValueError("give at least one of low / typical / high")
    low = low if low is not None else min(vals)
    high = high if high is not None else max(vals)
    if typical is None:
        typical = statistics.median([low, high]) if low != high else low
    if low > high:
        low, high = high, low
    key, now = item_key(name), int(observed_at or time.time())
    cur = None
    if replace_within_hours:
        cur = conn.execute(
            "SELECT id FROM price_observations WHERE item_key=? AND kind=? AND source=? AND observed_at>=? "
            "ORDER BY observed_at DESC LIMIT 1", (key, kind, source, now - replace_within_hours * 3600)).fetchone()
    if cur:
        conn.execute("UPDATE price_observations SET low=?, high=?, typical=?, n=?, condition=?, url=?, notes=?, "
                     "observed_at=? WHERE id=?", (low, high, typical, n, condition, url, notes, now, cur[0]))
        row_id = cur[0]
    else:
        row_id = conn.execute(
            "INSERT INTO price_observations (item_key, item_name, kind, source, low, high, typical, n, condition, url, "
            "notes, observed_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (key, name.strip()[:200], kind, source, low, high, typical, n, condition, url, notes, now)).lastrowid
    conn.commit()
    if refresh:
        refresh_range(conn, key)
    return row_id


def add_trade(conn: sqlite3.Connection, name: str, buy_price: float, sell_price: float | None = None, *,
              channel: str | None = None, buy_costs: float = 0.0, sell_fees: float = 0.0, notes: str | None = None,
              bought_at: int | None = None, sold_at: int | None = None, listed_at: int | None = None,
              list_price: float | None = None) -> int:
    """Log a real purchase (and sale). A sale also becomes a 'realized' observation, the strongest evidence we have.

    listed_at (when it went up for sale) is what makes days-to-sell measurable: sold_at - listed_at.
    """
    now = int(time.time())
    tid = conn.execute(
        "INSERT INTO trades (item_key, item_name, bought_at, buy_price, buy_costs, sold_at, sell_price, sell_fees, "
        "channel, notes, listed_at, list_price) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
        (item_key(name), name.strip()[:200], bought_at or now, buy_price, buy_costs,
         (sold_at or now) if sell_price is not None else None, sell_price, sell_fees, channel, notes, listed_at,
         list_price)).lastrowid
    conn.commit()
    if sell_price is not None:
        add_observation(conn, name, "realized", f"my-sale-{channel or 'unknown'}", typical=sell_price, n=1,
                        notes=f"trade #{tid}", observed_at=sold_at or now)
    return tid


def close_trade(conn: sqlite3.Connection, trade_id: int, sell_price: float, *, sold_at: int | None = None,
                sell_fees: float = 0.0, channel: str | None = None) -> None:
    """Record the sale of an open trade (bought earlier, listed, now sold)."""
    row = conn.execute("SELECT item_name, channel FROM trades WHERE id=?", (trade_id,)).fetchone()
    if row is None:
        raise ValueError(f"no trade #{trade_id}")
    now = int(time.time())
    conn.execute("UPDATE trades SET sold_at=?, sell_price=?, sell_fees=?, channel=COALESCE(?, channel) WHERE id=?",
                 (sold_at or now, sell_price, sell_fees, channel, trade_id))
    conn.commit()
    add_observation(conn, row["item_name"], "realized", f"my-sale-{channel or row['channel'] or 'unknown'}",
                    typical=sell_price, n=1, notes=f"trade #{trade_id}", observed_at=sold_at or now)


# ------------------------------------------------------------------ reads
def observations(conn: sqlite3.Connection, key: str, as_of: int | None = None) -> list[sqlite3.Row]:
    return conn.execute("SELECT * FROM price_observations WHERE item_key=? AND observed_at<=? ORDER BY observed_at DESC",
                        (key, as_of or int(time.time()) + 1)).fetchall()


def compute_range(conn: sqlite3.Connection, key: str, *, as_of: int | None = None, window_days: float = 180,
                  ask_discount: float = 0.2, retail_cap: float = 0.85, min_sold: int = 3) -> ValueRange | None:
    """The item's value range from the newest evidence per (kind, source) inside the window; None if there is none."""
    now = as_of or int(time.time())
    rows = [r for r in observations(conn, key, now) if now - r["observed_at"] <= window_days * 86400]
    if not rows:
        return None
    latest: dict[tuple[str, str], sqlite3.Row] = {}
    for r in rows:  # newest first
        latest.setdefault((r["kind"], r["source"]), r)
    by_kind: dict[str, list[sqlite3.Row]] = {}
    for (kind, _), r in latest.items():
        by_kind.setdefault(kind, []).append(r)
    newest_days = (now - rows[0]["observed_at"]) / 86400
    notes: list[str] = []
    retail_rows = by_kind.get("new_retail", [])
    retail = min((r["typical"] for r in retail_rows), default=None)
    basis = next((k for k in ("realized", "sold", "ask") if by_kind.get(k)), None)
    if basis is None:
        return ValueRange(key, None, None, None, "D", "new_retail" if retail else "melt", len(latest), newest_days,
                          ["only a retail ceiling / melt value, nothing about resale"], retail)
    thin = None
    if basis == "sold" and by_kind.get("ask") and sum(r["n"] or 1 for r in by_kind["sold"]) < min_sold:
        # a couple of sales shouldn't outrank a broad set of listings: use the asks, widened to include the sales
        thin, basis = by_kind["sold"], "ask"
        notes.append(f"only {sum(r['n'] or 1 for r in thin)} sold sample(s) so far; range rests on asking prices "
                     f"and is widened to include the sales")
    use = by_kind[basis]
    low = min(r["low"] for r in use)
    high = max(r["high"] for r in use)
    typical = statistics.median([r["typical"] for r in use])
    if basis == "ask":
        low, typical, high = (v * (1 - ask_discount) for v in (low, typical, high))
        notes.append(f"asking prices discounted {ask_discount:.0%}: sellers' asks overstate what items sell for")
        if thin:
            low, high = min([low] + [r["low"] for r in thin]), max([high] + [r["high"] for r in thin])
    if retail:
        cap = retail * retail_cap
        if typical > cap:
            low, typical, high = min(low, cap), cap, min(high, cap)
            notes.append(f"capped at {retail_cap:.0%} of new retail ${retail:,.0f}")
    sources = {r["source"] for r in use}
    if basis in ("realized", "sold"):
        asks = by_kind.get("ask", [])
        corroborated = len(sources) >= 2 or any(
            abs(statistics.median([a["typical"] for a in asks]) * (1 - ask_discount) - typical) <= 0.3 * typical
            for _ in [0] if asks)
        confidence = "A" if corroborated else "B"
    else:
        confidence = "C"
    stale = newest_days > window_days / 2
    if stale:
        notes.append(f"newest evidence is {newest_days:.0f} days old")
    return ValueRange(key, round(low, 2), round(typical, 2), round(high, 2), confidence, basis, len(use), newest_days,
                      notes, retail, stale)


def refresh_range(conn: sqlite3.Connection, key: str, **kw) -> ValueRange | None:
    """Recompute and store the item's current range (called whenever evidence is added)."""
    rng = compute_range(conn, key, **kw)
    if rng is None:
        conn.execute("DELETE FROM value_ranges WHERE item_key=?", (key,))
    else:
        name = conn.execute("SELECT item_name FROM price_observations WHERE item_key=? ORDER BY observed_at DESC LIMIT 1",
                            (key,)).fetchone()
        conn.execute(
            "INSERT OR REPLACE INTO value_ranges (item_key, item_name, low, typical, high, confidence, basis, n_obs, "
            "retail, stale, updated_at, notes) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (key, name[0] if name else key, rng.low, rng.typical, rng.high, rng.confidence, rng.basis, rng.n_obs,
             rng.retail, int(rng.stale), int(time.time()), "; ".join(rng.notes)))
    conn.commit()
    return rng


def refresh_all(conn: sqlite3.Connection, **kw) -> int:
    keys = [r[0] for r in conn.execute("SELECT DISTINCT item_key FROM price_observations")]
    for k in keys:
        refresh_range(conn, k, **kw)
    return len(keys)


def find_items(conn: sqlite3.Connection, text: str, strict: bool = False) -> list[str]:
    """Item keys matching a search. Loose (for people typing): every query word is in the key, or the key's words are all
    in the query, or most words overlap. Strict (for automatic matching): every query word must be in the key."""
    q = _tokens(item_key(text))
    if not q:
        return []
    hits = []
    for (key,) in conn.execute("SELECT DISTINCT item_key FROM price_observations"):
        kt = _tokens(key)
        if q <= kt or (not strict and ((kt and kt <= q) or len(q & kt) >= max(2, int(0.75 * min(len(q), len(kt)))))):
            hits.append(key)
    return sorted(hits)


def cross_check(rng: ValueRange | None, estimate: float, tolerance: float = 0.3) -> str | None:
    """A warning when our estimate sits outside the known range by more than `tolerance`, else None."""
    if rng is None or rng.typical is None or estimate <= 0:
        return None
    if estimate > rng.high * (1 + tolerance):
        return (f"ESTIMATE ${estimate:,.0f} IS ABOVE the known range (${rng.low:,.0f} to ${rng.high:,.0f}, "
                f"confidence {rng.confidence}): treat with suspicion")
    if estimate < rng.low * (1 - tolerance):
        return f"estimate ${estimate:,.0f} is below the known range (${rng.low:,.0f} to ${rng.high:,.0f})"
    return None


def grade(comp_source: str | None, rng: ValueRange | None) -> str:
    """Confidence grade for an alert: the stored range's grade if we have one, else from the comp source."""
    if comp_source == "mock":
        return "TEST"
    if comp_source == "melt":
        return "melt (objective)"
    if rng is not None and rng.typical is not None:
        return rng.confidence + (" stale" if rng.stale else "")
    return {"sold": "B", "active": "C"}.get(comp_source or "", "?")
