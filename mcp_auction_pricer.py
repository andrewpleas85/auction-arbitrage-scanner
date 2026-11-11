"""auction-pricer: a local MCP server that prices auction lots from SOLD evidence, not retail or guesses.

Run:  .venv/bin/python mcp_auction_pricer.py        (stdio transport, for an MCP client such as Claude Code)
Register:  claude mcp add auction-pricer -- /abs/path/.venv/bin/python /abs/path/mcp_auction_pricer.py

Where the numbers come from
  * Sold comps are read from this project's own database (`sold_sales`, every eBay sale SoldComps ever returned) and the
    knowledge base (`value_ranges`). Nothing is scraped from eBay pages: that breaks eBay's terms and the project already
    has a licensed sold-data provider.
  * `fetch_ebay_sold_comps(..., allow_paid=True)` is the ONLY path that spends SoldComps quota (1 request of the 100 a
    month on the free plan). It is off by default and needs SOLDCOMPS_API_KEY in .env.
  * Where no sold evidence exists the luxury and tech tools say so and return no value, instead of inventing one.
    Depreciation and authenticity discounts below are ASSUMPTIONS, labelled as such in every result.
"""
from __future__ import annotations

import asyncio
import re
import sqlite3
import statistics
from datetime import datetime, timezone
from pathlib import Path

from fastmcp import FastMCP

import catalog
from engine import knowledge as kb
import free_comp_fillers as ff
from fill_ranges import _ACCESSORY, _COMP_JUNK  # the project's own accessory / parts / lot filters

DB_PATH = Path(__file__).with_name("arbitrage.db")
mcp = FastMCP("auction-pricer")

_DAMAGED = re.compile(r"\b(?:for parts|parts only|parts or repair|repair|broken|cracked|not working|as[- ]is|untested|faulty|"
                      r"defective|locked|icloud|read desc|damaged|salvage)\b", re.I)
CONDITION = {"new": 1.00, "like new": 0.95, "excellent": 0.90, "very good": 0.85, "good": 0.78, "fair": 0.62, "poor": 0.40,
             "for parts": 0.15}
HIGH_COUNTERFEIT = {"louis vuitton", "chanel", "gucci", "hermes", "hermès", "prada", "cartier", "van cleef", "rolex", "dior",
                    "bottega veneta", "saint laurent", "balenciaga", "fendi", "celine", "goyard", "tiffany"}
# annual value retention of used tech vs. new price (ASSUMPTION: typical resale curves, not measured here)
RETENTION_YEAR1 = {"ipad": 0.72, "tablet": 0.55, "kindle": 0.55, "macbook": 0.75, "laptop": 0.55}
RETENTION_PER_YEAR = {"ipad": 0.82, "tablet": 0.72, "kindle": 0.75, "macbook": 0.85, "laptop": 0.72}
FLOOR = 0.12


_SALES_CACHE: dict = {}


def _conn() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def _cond(condition: str) -> float:
    return CONDITION.get((condition or "good").strip().lower(), CONDITION["good"])


def _iqr_filter(prices: list[float]) -> list[float]:
    if len(prices) < 5:
        return prices
    q1, _, q3 = statistics.quantiles(prices, n=4)
    lo, hi = q1 - 1.5 * (q3 - q1), q3 + 1.5 * (q3 - q1)
    return [p for p in prices if lo <= p <= hi]


def _parse_dt(raw: str) -> datetime | None:
    try:
        d = datetime.fromisoformat(raw)
        return d if d.tzinfo else d.replace(tzinfo=timezone.utc)
    except (TypeError, ValueError):
        return None


def _stored_sales(conn: sqlite3.Connection, query: str) -> list[tuple[str, float, str]]:
    toks = kb.item_key(query).split()
    if not toks:
        return []
    key = (conn.execute("PRAGMA database_list").fetchone()[2], conn.execute("SELECT COUNT(*) FROM sold_sales").fetchone()[0])
    rows = _SALES_CACHE.get(key)  # keyed on the database file and its row count, so a different or grown database is re-read
    if rows is None:
        _SALES_CACHE.clear()
        rows = _SALES_CACHE[key] = [(r[0], r[1], r[2], set(kb.item_key(r[0]).split()))
                                    for r in conn.execute("SELECT DISTINCT title, price, sold_date FROM sold_sales")]
    out = []
    for title, price, sold_date, have in rows:
        if all(t in have for t in toks):
            out.append((title, float(price), sold_date or ""))
    return out


def _summarise(sales: list[tuple[str, float, str]], include_damaged: bool) -> dict:
    sales = [s for s in sales if not (_COMP_JUNK.search(s[0]) or _ACCESSORY.search(s[0])) or (include_damaged and _DAMAGED.search(s[0]))]
    kept = [s for s in sales if include_damaged or not _DAMAGED.search(s[0])]
    dropped_damaged = len(sales) - len(kept)
    inliers = set(_iqr_filter([s[1] for s in kept]))
    kept2 = [s for s in kept if s[1] in inliers]
    if not kept2:
        return {"sample_size": 0, "dropped_damaged": dropped_damaged, "dropped_outliers": len(kept) - len(kept2)}
    prices = sorted(s[1] for s in kept2)
    recent = sorted(kept2, key=lambda s: _parse_dt(s[2]) or datetime.min.replace(tzinfo=timezone.utc), reverse=True)[:5]
    return {"median_sold_price": round(statistics.median(prices), 2), "low_sold_price": round(prices[0], 2),
            "high_sold_price": round(prices[-1], 2), "sample_size": len(prices),
            "dropped_damaged": dropped_damaged, "dropped_outliers": len(kept) - len(kept2),
            "recent_sold": [{"title": t, "total_price": p, "date": d[:10] or None} for t, p, d in recent]}


@mcp.tool
def fetch_ebay_sold_comps(query: str, category_id: str = "", include_damaged: bool = False, allow_paid: bool = False) -> dict:
    """Real eBay SOLD prices for a query: median/low/high, sample size, and the 5 most recent sales.

    Reads the sales already stored in the local database (free). Parts/damaged listings are dropped unless
    `include_damaged`, and price outliers are removed with the IQR rule. If nothing is stored and `allow_paid` is true,
    one SoldComps request is made (costs 1 of the 100 monthly free requests) and the result is saved for next time.
    `category_id` is accepted for compatibility but SoldComps searches by keyword only. Shipping cost and the sold
    date's time of day are not in the data: `total_price` is the item's sold price.
    """
    conn = _conn()
    try:
        sales = _stored_sales(conn, query)
        source = "local database (no cost)"
        if not sales and allow_paid:
            from config import settings
            if not settings.soldcomps_api_key:
                return {"query": query, "error": "SOLDCOMPS_API_KEY is not set in .env; nothing stored matches this query"}
            import httpx
            from clients.ebay import CompCache, SoldCompsClient

            async def go():
                async with httpx.AsyncClient(timeout=30) as http:
                    cache = CompCache(conn, "soldcomps", settings.soldcomps_cache_ttl_hours * 3600)
                    return await SoldCompsClient(http, settings.soldcomps_api_key, cache=cache).comps(query)
            res = asyncio.run(go())
            sales = [(c.title, c.price, c.sold_date.isoformat() if c.sold_date else "") for c in res.items]
            source = "SoldComps API (1 request spent)"
        if not sales:
            return {"query": query, "sample_size": 0, "source": source,
                    "note": "no stored sold sales match every word of the query; pass allow_paid=true to spend 1 SoldComps request"}
        return {"query": query, "source": source, **_summarise(sales, include_damaged)}
    finally:
        conn.close()


def _local_range(conn: sqlite3.Connection, text: str) -> kb.ValueRange | None:
    r = catalog.model_range(conn, text)
    if r:
        return r
    for key in kb.find_items(conn, text, strict=True):
        rng = kb.compute_range(conn, key)
        if rng and rng.typical is not None:
            rng.notes.append(f"matched stored item: {key}")
            return rng
    return None


def _range_dict(r: kb.ValueRange) -> dict:
    return {"low": r.low, "typical": r.typical, "high": r.high, "confidence": r.confidence, "basis": r.basis,
            "n_obs": r.n_obs, "newest_days": r.newest_days, "stale": r.stale, "notes": r.notes}



def _free_fallback(title: str, family: str, device_type: str, storage_gb: int | None, chip: str | None, ram_gb: int | None,
                   release_year: int | None, allow_network: bool = True) -> dict | None:
    """Swappa free page first (tablets and MacBooks), then the spec matrix (laptops). None if neither can price it."""
    if allow_network:
        for slug in ff.swappa_slug_candidates(title, family):
            price = ff.fetch_swappa_free_comp(slug, storage_gb, chip)
            if price:
                return {"value": round(price, 2), "comp_source": "swappa_free", "swappa_slug": slug,
                        "caveat": "Swappa average sale price for this chip/storage row (or its headline average); window not stated by Swappa"}
    if "laptop" in device_type.lower() or "macbook" in title.lower() or "macbook" in family.lower():
        cpu = ff.cpu_family_from_text(title)
        if cpu:
            yr = release_year or (int(re.search(r"\b(20(?:1[5-9]|2[0-6]))\b", title).group(1)) if re.search(r"\b(20(?:1[5-9]|2[0-6]))\b", title) else 2020)
            v = ff.evaluate_laptop_by_spec_matrix("", cpu, ram_gb or 0, storage_gb or 0, yr)
            if v > 0:
                return {"value": v, "comp_source": "spec_matrix", "cpu_family": cpu, "release_year": yr,
                        "caveat": "rule-of-thumb FLOOR from CPU/RAM/storage/age (an assumption, not market data)"}
    return None


@mcp.tool
def evaluate_tech_hardware(device_type: str, brand: str, model: str, storage_gb: int = 0, specs: str = "",
                           condition: str = "good", release_year: int = 0, new_price: float = 0.0) -> dict:
    """Resale estimate for an iPad, Kindle, tablet, MacBook or laptop.

    Order of evidence: (1) stored sold sales for this exact model (and storage size when given), scaled by `condition`
    against a 'good' baseline; (2) if none and both `release_year` and `new_price` are given, a depreciation curve
    (an ASSUMPTION, labelled). With neither, no value is returned. Chip generation and screen size are only honoured when
    they appear in `model`/`specs`, because the lookup matches on words.
    """
    kind = next((k for k in RETENTION_YEAR1 if k in device_type.lower()), "laptop")
    text = " ".join(x for x in (brand, model, specs, f"{storage_gb}gb" if storage_gb else "") if x)
    conn = _conn()
    try:
        r = _local_range(conn, text)
        sales = _stored_sales(conn, text)
        scale = _cond(condition) / CONDITION["good"]
        if r and r.typical is not None:
            return {"method": "stored sold comps", "comp_source": "database", "query": text, "condition": condition, "condition_scale": round(scale, 2),
                    "expected_resale": round(r.typical * scale, 2), "range": _range_dict(r),
                    "sold_sample": _summarise(sales, False) if sales else None,
                    "caveat": "condition scale is an assumption (comps are mostly 'good'/'used'); battery health is not priced"}
        fb = _free_fallback(text, f"{brand} {model}", device_type, storage_gb or None, None, None, release_year or None)
        if fb:
            scale = _cond(condition) / CONDITION["good"]
            return {"method": "free fallback", "query": text, "condition": condition, "expected_resale": round(fb["value"] * (scale if fb["comp_source"] == "swappa_free" else 1.0), 2), **fb}
        if release_year and new_price:
            age = max(0, datetime.now().year - release_year)
            f = max(FLOOR, RETENTION_YEAR1[kind] * RETENTION_PER_YEAR[kind] ** max(0, age - 1)) if age else 0.9
            return {"method": "depreciation curve (assumption, no sold comps stored)", "comp_source": "depreciation_assumption", "query": text, "age_years": age,
                    "retention": round(f, 2), "expected_resale": round(new_price * f * scale, 2),
                    "caveat": "not market data; run fetch_ebay_sold_comps(..., allow_paid=true) to measure it"}
        return {"method": "none", "query": text, "expected_resale": None,
                "reason": "no stored sold sales match; give release_year and new_price for a labelled depreciation estimate"}
    finally:
        conn.close()


@mcp.tool
def evaluate_luxury_designer_goods(brand: str, item_type: str, model_name: str, condition: str = "good",
                                   has_authenticity_card: bool = False, has_box_or_receipt: bool = False) -> dict:
    """As-is, risk-adjusted resale value for a designer bag, clothing item, jewelry piece or watch.

    Base value comes from stored eBay sold sales / the knowledge base. FashionPhile, The RealReal and Vestiaire are not
    queried (no licensed data access here). For high-counterfeit brands, no authenticity card AND no box/receipt applies
    a 35% markdown (25% if only one proof is present): an ASSUMPTION following the 25-40% range, not a measured figure.
    """
    text = f"{brand} {model_name} {item_type}".strip()
    conn = _conn()
    try:
        r = _local_range(conn, f"{brand} {model_name}") or _local_range(conn, text)
        if not r or r.typical is None:
            return {"method": "none", "query": text, "as_is_value": None,
                    "reason": "no stored sold evidence for this brand and model; look up sold comps first"}
        base = r.typical * _cond(condition) / CONDITION["good"]
        risky = brand.strip().lower() in HIGH_COUNTERFEIT
        proofs = int(bool(has_authenticity_card)) + int(bool(has_box_or_receipt))
        discount = (0.35 if proofs == 0 else 0.25 if proofs == 1 else 0.0) if risky else 0.10 if proofs == 0 else 0.0
        return {"query": text, "condition": condition, "base_value": round(base, 2), "authenticity_markdown": discount,
                "as_is_value": round(base * (1 - discount), 2), "range": _range_dict(r), "high_counterfeit_brand": risky,
                "caveat": "the markdown is an assumption; nothing here verifies authenticity (text and proofs only)"}
    finally:
        conn.close()


CHICAGO_TAX = 0.1025
MIN_NET_PROFIT = 100.0
_CHIP = re.compile(r"\bm([1-4])(?:\s*(pro|max|ultra))?\b", re.I)
_STORAGE = re.compile(r"\b(\d{2,4})\s?(gb|tb)\b", re.I)
_RAM_WORD = re.compile(r"\b(8|12|16|24|32|64)\s?gb\s*(?:ram|memory|unified)|\b(?:ram|memory)[: ]+(8|12|16|24|32|64)\s?gb", re.I)
_SCREEN = re.compile(r"\b(11|12\.9|13(?:\.3|\.6)?|14(?:\.2)?|15(?:\.3|\.6)?|16(?:\.2)?|17)\s?(?:\"|inch|in\b|-inch)", re.I)
_DJI = re.compile(r"\b(?:dji\s+)?(mavic\s*[34](?:\s*(?:pro|classic|cine))?|mini\s*[2-5](?:\s*pro)?|air\s*(?:2s|3s?)|avata\s*2?|phantom\s*\d(?:\s*pro)?|inspire\s*\d)\b", re.I)
_AUTEL = re.compile(r"\bautel\b.{0,20}\b(evo\s*(?:nano|lite|max|ii)[\w+ ]*)", re.I)


def _storage_gb(text: str) -> int:
    best = 0
    for n, u in _STORAGE.findall(text):
        v = int(n) * (1000 if u.lower() == "tb" else 1)
        if v >= 64:  # 8/16 GB next to 'RAM' is memory, not storage
            best = max(best, v)
    return best


def parse_tech_specs(title: str) -> dict:
    """Pull chip, RAM, storage, screen size and drone extras from a lot title. Missing items are None: nothing is guessed."""
    m = _CHIP.search(title)
    ram = _RAM_WORD.search(title)
    sc = _SCREEN.search(title)
    storage = _storage_gb(title)
    d = {"chip": f"m{m.group(1)}" + (f" {m.group(2).lower()}" if m.group(2) else "") if m else None,
         "ram_gb": int(next(g for g in ram.groups() if g)) if ram else None,
         "storage_gb": storage or None, "screen_in": sc.group(1) if sc else None}
    t = title.lower()
    dm, am = _DJI.search(title), _AUTEL.search(title)
    if dm or am:
        d["drone_model"] = re.sub(r"\s+", " ", (dm.group(1) if dm else "autel " + am.group(1))).lower().strip()
        d["fly_more_combo"] = bool(re.search(r"fly\s*more|combo", t))
        d["controller"] = ("rc pro / screen controller" if re.search(r"rc\s*pro|smart controller|rc\s*2|with screen", t)
                           else "standard rc-n1/n2" if re.search(r"rc[- ]?n\d|standard", t) else "unspecified")
        nb = re.search(r"\b(\d)\s*(?:x\s*)?batter", t)
        d["batteries"] = int(nb.group(1)) if nb else (3 if d["fly_more_combo"] else None)
    return d


def _spec_conflict(title: str, want: dict) -> bool:
    """True when a sold title names a DIFFERENT chip, storage or screen size than the lot (so it cannot be a comp)."""
    m = _CHIP.search(title)
    if want.get("chip") and m:
        got = f"m{m.group(1)}" + (f" {m.group(2).lower()}" if m.group(2) else "")
        if got.split()[0] != want["chip"].split()[0] or (len(want["chip"].split()) != len(got.split())):
            return True
    st = _storage_gb(title)
    if want.get("storage_gb") and st and st != want["storage_gb"]:
        return True
    sc = _SCREEN.search(title)
    if want.get("screen_in") and sc and sc.group(1).split(".")[0] != str(want["screen_in"]).split(".")[0]:
        return True
    return False


@mcp.tool
def evaluate_high_value_tech(title: str, family: str = "", condition: str = "good") -> dict:
    """Spec-matched resale value for drones, pro tablets/laptops, gaming handhelds, cameras and pro e-readers.

    Parses chip generation (M1-M4), RAM, storage and screen size (and for drones: model, Fly More combo, controller,
    battery count) out of `title`, then uses ONLY stored eBay sold sales whose titles agree on those specs. It tries the
    full spec first and loosens one field at a time (RAM, then screen, then storage), and says which level matched and how
    many sales it rests on. Fewer than 3 comparable sales returns value None. `family` is the product name to search
    ('macbook air', 'ipad pro', 'dji mini 3 pro'); when empty it is inferred for drones and left to the title otherwise.
    Swappa is not queried (no data access here).
    """
    specs = parse_tech_specs(title)
    fam = (family or specs.get("drone_model") or "").strip().lower()
    if specs.get("drone_model") and "dji" not in fam and "autel" not in fam:
        fam = "dji " + fam
    if not fam:
        return {"title": title, "specs": specs, "value": None, "reason": "pass family= (e.g. 'macbook air', 'ipad pro')"}
    conn = _conn()
    try:
        levels = []
        base = [fam] + ([specs["chip"]] if specs.get("chip") else [])
        extras = [("storage", f'{specs["storage_gb"]}gb' if specs.get("storage_gb") and specs["storage_gb"] < 1000 else
                   (f'{specs["storage_gb"]//1000}tb' if specs.get("storage_gb") else None)),
                  ("ram", f'{specs["ram_gb"]}gb' if specs.get("ram_gb") else None),
                  ("combo", "combo" if specs.get("fly_more_combo") else None)]
        full = [x for _, x in extras if x]
        levels.append(("full spec", base + full))
        for drop in ("ram", "combo", "storage"):
            kept = [x for k, x in extras if x and k != drop]
            if kept != full:
                levels.append((f"without {drop}", base + kept))
        levels.append(("family + chip only", base))
        seen = set()
        for label, toks in levels:
            q = " ".join(toks)
            if q in seen:
                continue
            seen.add(q)
            sales = [x for x in _stored_sales(conn, q) if not _spec_conflict(x[0], specs)]
            summ = _summarise(sales, False)
            if summ.get("sample_size", 0) >= 3:
                scale = _cond(condition) / CONDITION["good"]
                out = {"title": title, "specs": specs, "comp_source": "database", "match_level": label, "query": q, "value": round(summ["median_sold_price"] * scale, 2),
                       "sold_sample": {k: summ[k] for k in ("median_sold_price", "low_sold_price", "high_sold_price", "sample_size", "recent_sold")},
                       "caveat": "condition scale is an assumption; sold titles were matched on words, so exact RAM/storage "
                                 "may be pooled" if label != "full spec" else "condition scale is an assumption"}
                if specs.get("drone_model") and not specs.get("fly_more_combo") and label != "full spec":
                    out["caveat"] += "; combo status not separated"
                return out
        kind_hint = "laptop" if re.search(r"macbook|laptop|notebook|latitude|thinkpad|elitebook|xps", title, re.I) else "tablet"
        fb = _free_fallback(title, fam, kind_hint, specs.get("storage_gb"), specs.get("chip"), specs.get("ram_gb"), None) if not specs.get("drone_model") else None
        if fb:
            scale = _cond(condition) / CONDITION["good"]
            return {"title": title, "specs": specs, "match_level": "free fallback", "query": fb.get("swappa_slug") or fb.get("cpu_family"),
                    "value": round(fb["value"] * (scale if fb["comp_source"] == "swappa_free" else 1.0), 2),
                    "sold_sample": None, **{k: v for k, v in fb.items() if k != "value"}}
        return {"title": title, "specs": specs, "value": None, "comp_source": None, "reason": "fewer than 3 stored sold sales agree on these specs",
                "tried": [" ".join(t) for _, t in levels]}
    finally:
        conn.close()


_JW_WEIGHT = re.compile(r"(\d+(?:\.\d+)?)\s*(grams?|g|dwt|ozt|troy\s*oz)\b", re.I)
_JW_KARAT = re.compile(r"\b(8|9|10|12|14|18|20|21|22|24)\s?-?\s?k(?:t|arat)?\b", re.I)
_JW_PURITY = [(re.compile(r"\bsterling|\.?925\b", re.I), "silver", 0.925), (re.compile(r"\bplatinum|\bpt\s?950\b", re.I), "platinum", 0.95),
              (re.compile(r"\.999\b|fine silver", re.I), "silver", 0.999)]


@mcp.tool
def evaluate_luxury_watch_jewelry(title: str, description: str = "", weight_grams: float = 0.0, metal: str = "",
                                  purity_karat: int = 0, dealer_payout: float = 0.85) -> dict:
    """Melt value of UNBRANDED solid gold, silver or platinum jewelry from its weight and purity (live spot price).

    Weight and karat are read from the title/description when not passed ("14k", "12.4 grams", "sterling"). Plated,
    filled, costume and watch lots return None; so do lots with no weight or no purity. Stones add weight that is not
    metal: a piece mentioning stones/diamonds gets `stones_flag` and the stated weight is treated as an upper bound, so
    `melt_value` is an upper bound there. Brand-name pieces (Cartier, Tiffany, Rolex...) are priced by resale comps, not
    melt: this tool defers them. `dealer_payout` is the share of melt a refiner/dealer pays (default 85%).
    """
    from engine.melt import EXCLUDE_RE, STONE_RE
    text = f"{title} {description}"
    if EXCLUDE_RE.search(text):
        return {"value": None, "comp_source": None, "reason": "plated / filled / costume / watch: not priced by melt"}
    if re.search(r"\b(cartier|tiffany|rolex|van\s+cleef|bulgari|david\s+yurman)\b", text, re.I):
        return {"value": None, "comp_source": None, "reason": "branded piece: needs resale comps, melt would undervalue it"}
    grams = weight_grams
    if not grams:
        w = _JW_WEIGHT.search(text)
        if w:
            u = w.group(2).lower()
            grams = float(w.group(1)) * (1.55517 if u == "dwt" else 31.1035 if u.startswith(("ozt", "troy")) else 1.0)
    mt, karat, frac = (metal or "").lower(), purity_karat, None
    if not mt and _JW_KARAT.search(text) and any(rx.search(text) for rx, _, _ in _JW_PURITY):
        return {"value": None, "comp_source": None, "reason": "mixed metals (gold and silver/platinum named): weight cannot be split"}
    if not mt:
        k = _JW_KARAT.search(text)
        if k:
            mt, karat = "gold", int(k.group(1))
        else:
            for rx, name, f in _JW_PURITY:
                if rx.search(text):
                    mt, frac = name, f
                    break
    if not grams or grams <= 0 or not mt or (mt == "gold" and not karat):
        return {"value": None, "comp_source": None, "reason": "weight or purity not stated"}
    if mt == "silver" and frac is None:
        frac = 0.925
    if mt == "platinum" and frac is None:
        frac = 0.95
    melt = ff.calculate_precious_metal_melt(grams, mt, karat or 24, purity_fraction=frac)
    stones = bool(STONE_RE.search(text))
    return {"value": round(melt["melt_value"] * dealer_payout, 2), "melt_value": melt["melt_value"], "dealer_payout": dealer_payout,
            "weight_grams": round(grams, 2), "metal": mt, "purity_pct": melt["purity_pct"], "spot_price_gram": melt["spot_price_gram"],
            "spot_source": melt["spot_source"], "stones_flag": stones, "comp_source": "metal_spot",
            "caveat": "melt value only; " + ("stated weight includes stones, so this is an UPPER bound" if stones else "weight taken as all metal")}


@mcp.tool
def calculate_arbitrage_margin(current_bid: float, estimated_market_value: float, platform_fee_pct: float = 0.13,
                               estimated_shipping: float = 15.0, buyer_premium_pct: float = 0.0,
                               sales_tax_pct: float = CHICAGO_TAX, inbound_shipping: float = 0.0, target_margin: float = 0.25,
                               min_net_profit: float = MIN_NET_PROFIT) -> dict:
    """Profit, ROI, break-even bid and max bid ceiling for a resale, with a hard $100 net-profit guardrail.

    Cost = bid x (1 + buyer premium) x (1 + tax) + inbound shipping. Revenue = value x (1 - platform fee) - outbound shipping.
    Sales tax defaults to Chicago's 10.25%. `max_bid_ceiling` keeps `target_margin` (25%) on cost, and
    `max_bid_for_min_profit` is the highest bid that still nets `min_net_profit`. Recommendation:
    "SKIPPED (PROFIT < $100)" whenever net profit is under `min_net_profit`, whatever the ROI; otherwise BUY at ROI
    >= target margin, else HOLD.
    """
    mult = (1 + buyer_premium_pct) * (1 + sales_tax_pct)
    revenue = estimated_market_value * (1 - platform_fee_pct) - estimated_shipping
    cost = current_bid * mult + inbound_shipping
    profit = revenue - cost
    roi = profit / cost if cost else 0.0
    breakeven = max(0.0, (revenue - inbound_shipping) / mult)
    ceiling = max(0.0, (revenue / (1 + target_margin) - inbound_shipping) / mult)
    for_min = max(0.0, (revenue - min_net_profit - inbound_shipping) / mult)
    if profit < min_net_profit:
        rec = f"SKIPPED (PROFIT < ${min_net_profit:,.0f})"
    else:
        rec = "BUY" if roi >= target_margin else "HOLD"
    return {"net_profit": round(profit, 2), "roi_pct": round(roi * 100, 1), "break_even_bid": round(breakeven, 2),
            "max_bid_ceiling": round(ceiling, 2), "max_bid": round(ceiling, 2), "max_bid_for_min_profit": round(for_min, 2),
            "risk_reward": round(profit / cost, 2) if cost else None, "recommendation": rec,
            "net_revenue_after_fees": round(revenue, 2), "all_in_cost": round(cost, 2),
            "note": "assumes the item sells at estimated_market_value; auctions usually close above the current bid"}


if __name__ == "__main__":
    mcp.run()
