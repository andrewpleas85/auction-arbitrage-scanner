"""Turn real eBay SOLD prices into price ranges for the exact models you could bid on soon.

A range is stored under the model's identity ('canon eos rebel xti', 'bose companion 2'; see `title_cleaner.model_identity`) and is
only ever applied to listings with that identity, never to a whole brand or style group. Every sale SoldComps returns is kept
permanently in `sold_sales`, so:
  * `fill` spends at most `budget` requests, and a query already fetched inside the cache window costs nothing;
  * `rebuild` recomputes every stored range from the saved sales with NO API call (use it after changing the matching rules).
The free SoldComps plan is 100 requests a month.
"""
import logging
import re
import sqlite3
import statistics
from datetime import datetime, timedelta, timezone

import catalog
from clients.ebay import Comp, Comps, CompCache, SoldCompsClient, SoldDataUnavailable
from engine import knowledge as kb
from engine.title_cleaner import build_search_query, comp_matches, model_identity, variant_of

log = logging.getLogger("arbitrage.fill_ranges")
SOURCE = "soldcomps-fill"
JUNK_STYLE = {"sold", "pieces", "piece", "icloud", "lock", "locked", "charging", "parts", "lot", "bundle", "untested", "tested", "cell",
              "phone", "phones", "unlocked", "working", "used", "new", "only", "set", "mixed", "assorted"}
_SPEC = re.compile(r"\bi[3579]-\d{3,5}[a-z]{0,2}\b|\bf/[\d.]+|\bef-s?\b|\b\d+(?:gb|tb|mhz|ghz)\b", re.I)
_CODE = re.compile(r"[a-z]{1,4}\d{2,}[a-z0-9]*-\d{2,4}|\b\d{5,}\b", re.I)  # a style code like bq6472-115
# A sold title that is an accessory/part/lot or not a working unit must not set the price of the WORKING product.
_COMP_JUNK = re.compile(r"^\s*for\s+(?:apple|samsung|amazon|sony|nintendo|ipad|iphone)\b|\b(?:cases?|covers?|stands?|stylus|pencils?|chargers?|"
                        r"cables?|protectors?|skins?|sleeves?|keyboards?|straps?|mounts?|holders?|adapters?|batter(?:y|ies)|screens?|"
                        r"replacement|compatible|lot of|lots?|bundle of|parts?\b|repair|broken|cracked|not working|read|locked|icloud|"
                        r"as[- ]is|untested|faulty|defective|bad)\b", re.I)
_ACCESSORY = re.compile(r"\b(?:cases?|covers?|chargers?|protectors?|skins?|sleeves?|straps?|cables?|adapters?|stands?|mounts?|holders?|bags?|"
                        r"cords?|batter(?:y|ies)|remotes?|docks?|lenses|filters?)\b", re.I)
MIN_MATCHES = 4  # fewer matching sales than this says too little about a price


def is_junk_style(style: str) -> bool:
    """A 'style' containing a condition word ('touch icloud', 'pieces sold') or only generic words names no product to price."""
    words = set(style.lower().split())
    return bool(words & JUNK_STYLE) or words <= catalog.GENERIC_STYLE


def candidates(conn: sqlite3.Connection, max_days: float = 2.0, now: datetime | None = None, domain: str | None = None,
               kinds: set[str] | None = None) -> list[dict]:
    """Models worth a sold-price lookup, best first: running auctions closing within `max_days` whose exact model has no sold range yet."""
    now = now or datetime.now(timezone.utc)
    horizon = now + timedelta(days=max_days)
    by: dict[str, dict] = {}
    for g in catalog.groups(conn, now=now, domain=domain):
        if g["kind"] in ("accessory", "other") or not g["style"] or is_junk_style(g["style"]):
            continue
        if kinds and g["kind"] not in kinds:
            continue  # only spend lookups on the item types in focus
        end = catalog._end(g["sample"])
        if end is not None and end > horizon:
            continue
        import webpage  # late: webpage imports catalog
        if g["sample"]["platform"] in webpage.LOCAL_PICKUP_PLATFORMS and not webpage._gsa_near_chicago(g["sample"]):
            continue  # a pickup-only lot far from Chicago is never shown, so do not spend a lookup on it
        if _ACCESSORY.search(g["sample"]["title"]):
            continue  # the cheapest listing of a group is often a case or cable, which would be priced as the product
        ident = model_identity(g["sample"]["title"])
        if not ident or is_junk_style(ident.split(" ", 1)[-1]) or _SPEC.search(ident):
            continue  # a processor, lens or storage spec is not a model
        if g["kind"] in ("shoes", "bag", "clothing") and not _CODE.search(ident):
            continue  # a sneaker/bag price only means something for one style code, not 'jordan air 1' in general
        have = catalog.model_range(conn, g["sample"]["title"])
        if have is not None and have.basis in ("sold", "realized"):
            continue  # this model already has a sold range
        cur = by.get(ident)
        if cur is None or catalog.priority(g) > catalog.priority(cur):
            n = (cur or {}).get("n", 0)
            by[ident] = {**g, "ident": ident, "query": ident, "n": n}
        by[ident]["n"] += g["n"]
    out = list(by.values())
    out.sort(key=catalog.priority, reverse=True)
    return out


def working_comps(sample_title: str, comps: Comps) -> list[Comp]:
    """Sold items that are plausibly the same product in working order (no accessories, parts, lots or locked units)."""
    return [c for c in comps.items if comp_matches(sample_title, c.title) and not _COMP_JUNK.search(c.title)]


def _range(prices: list[float]) -> tuple[float, float, float, int] | None:
    prices = sorted(prices)
    if len(prices) < MIN_MATCHES:
        return None
    cut = max(1, len(prices) // 10)  # drop the extremes: one lot or one overpay should not set the range
    core = prices[cut:-cut] or prices
    return core[0], statistics.median(prices), core[-1], len(prices)


def summarize(sample_title: str, comps: Comps) -> tuple[float, float, float, int] | None:
    """(low, typical, high, n) over every variant pooled (used for the model-level range), or None if too few clean matches."""
    return _range([c.price for c in working_comps(sample_title, comps)])


def variant_ranges(sample_title: str, comps: Comps) -> dict[str, tuple[float, float, float, int]]:
    """{variant suffix: range} for each storage size, and each storage + connectivity, that has enough clean sales.
    Suffixes look like '128gb' and '128gb wifi'; the model-level (pooled) range is stored separately without a suffix."""
    groups: dict[str, list[float]] = {}
    for c in working_comps(sample_title, comps):
        storage, conn = variant_of(c.title)
        if storage:
            groups.setdefault(storage, []).append(c.price)
            if conn:
                groups.setdefault(f"{storage} {conn}", []).append(c.price)
    return {k: r for k, v in groups.items() if (r := _range(v))}


def _store(conn: sqlite3.Connection, ident: str, query: str, s: tuple, variants: dict | None = None) -> None:
    low, typ, high, n = s
    kb.add_observation(conn, ident, "sold", SOURCE, low=low, high=high, typical=typ, n=n,
                       notes=f"SoldComps query {query!r}; all storage sizes and connectivity pooled", replace_within_hours=24 * 7)
    for suffix, (vlo, vtyp, vhi, vn) in (variants or {}).items():
        kb.add_observation(conn, f"{ident} {suffix}", "sold", SOURCE, low=vlo, high=vhi, typical=vtyp, n=vn,
                           notes=f"SoldComps query {query!r}; {suffix} only", replace_within_hours=24 * 7)


async def fill(conn: sqlite3.Connection, client: SoldCompsClient | None, budget: int = 20, max_days: float = 2.0,
               dry_run: bool = False, domain: str | None = None, kinds: set[str] | None = None) -> list[dict]:
    """Look up the top `budget` models. Returns one result dict per model (stored / too few matches / dry-run)."""
    results = []
    for g in candidates(conn, max_days, domain=domain, kinds=kinds)[:budget]:
        res = {"name": g["ident"], "query": g["query"], "listings": g["n"], "status": "dry-run"}
        if not dry_run:
            if client is None:
                raise SoldDataUnavailable("SOLDCOMPS_API_KEY is not set")
            comps = await client.comps(g["query"])
            s = summarize(g["ident"], comps)
            if s is None:
                res["status"] = f"only {sum(1 for c in comps.items if comp_matches(g['ident'], c.title))} matching sales: skipped"
            else:
                _store(conn, g["ident"], g["query"], s, variant_ranges(g["ident"], comps))
                res.update(status="stored", low=s[0], typical=s[1], high=s[2], n=s[3])
        results.append(res)
    return results


def rebuild(conn: sqlite3.Connection) -> list[dict]:
    """Recompute every sold range from the saved sales, no API calls. Old group-level fills are replaced by model-level ones."""
    conn.execute("DELETE FROM price_observations WHERE source=?", (SOURCE,))
    conn.commit()
    cache = CompCache(conn, "soldcomps", 0)
    results = []
    for (query,) in conn.execute("SELECT DISTINCT query FROM sold_sales WHERE provider='soldcomps'").fetchall():
        comps = Comps("sold", cache.stored(query))
        ident = model_identity(query)
        s = summarize(query, comps) if ident else None
        if s is None:
            results.append({"name": ident or query, "status": "too few matching sales", "n": len(comps.items)})
            continue
        vr = variant_ranges(query, comps)
        _store(conn, ident, query, s, vr)
        results.append({"name": ident, "status": "stored", "low": s[0], "typical": s[1], "high": s[2], "n": s[3], "variants": len(vr)})
    return results


async def fill_named(conn: sqlite3.Connection, client: SoldCompsClient | None, pairs: list[tuple[str, str]],
                     dry_run: bool = False) -> list[dict]:
    """Look up explicit models: pairs of (identity to store under, text to search eBay sold for).
    The identity should be what `model_identity()` gives for the listings you want it applied to."""
    results = []
    for ident, query in pairs:
        res = {"name": ident, "query": query, "listings": 0, "status": "dry-run"}
        if not dry_run:
            if client is None:
                raise SoldDataUnavailable("SOLDCOMPS_API_KEY is not set")
            comps = await client.comps(query)
            s = summarize(ident, comps)
            if s is None:
                n = sum(1 for c in comps.items if comp_matches(ident, c.title) and not _COMP_JUNK.search(c.title))
                res["status"] = f"only {n} clean matching sales: skipped"
            else:
                _store(conn, ident, query, s, variant_ranges(ident, comps))
                res.update(status="stored", low=s[0], typical=s[1], high=s[2], n=s[3])
        results.append(res)
    return results


_BAG_WORDS = re.compile(r"\b(?:bag|purse|handbag|tote|satchel|crossbody|cross\s*body|shoulder|clutch|hobo|backpack|wristlet|bucket|messenger)\b", re.I)
_BAG_JUNK = re.compile(r"\b(?:wallet|keychain|key\s*chain|charm|strap\s+only|replacement|dust\s*bag\s+only|box\s+only|lot\s+of|lots?|"
                       r"parts?|repair|read|damaged|stained|broken|cosmetic|makeup)\b", re.I)


async def fill_baseline(conn: sqlite3.Connection, client: SoldCompsClient | None, brand: str, query: str, kind: str = "bag",
                        dry_run: bool = False) -> dict:
    """A brand-level SOLD baseline ('michael kors baseline-bags') from real sales: used when a listing's exact style has no range.

    Keeps sales whose title names the brand and a bag word, drops wallets/lots/damaged pieces, and stores the median under the
    key `catalog.baseline_key(brand, kind)` as a 'sold' observation, which outranks the hand-researched asking prices."""
    res = {"name": catalog.baseline_key(brand, kind), "query": query, "status": "dry-run"}
    if dry_run:
        return res
    if client is None:
        raise SoldDataUnavailable("SOLDCOMPS_API_KEY is not set")
    comps = await client.comps(query)
    b = brand.lower()
    prices = [c.price for c in comps.items if b in c.title.lower() and _BAG_WORDS.search(c.title) and not _BAG_JUNK.search(c.title)]
    s = _range(prices)
    if s is None:
        res["status"] = f"only {len(prices)} clean sales: skipped"
        return res
    kb.add_observation(conn, res["name"], "sold", "soldcomps-baseline", low=s[0], high=s[2], typical=s[1], n=s[3],
                       notes=f"SoldComps query {query!r}; {brand} {kind} brand-level sold baseline", replace_within_hours=24 * 7)
    res.update(status="stored", low=s[0], typical=s[1], high=s[2], n=s[3])
    return res
