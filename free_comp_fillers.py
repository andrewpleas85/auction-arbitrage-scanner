"""Free, deterministic fallback prices for lots that have no stored eBay sold comps. No paid API, no quota.

Three sources, each labelled so nothing is mistaken for measured eBay data:
  swappa_free   Swappa's public price pages (robots.txt allows /prices/). Used values are Swappa's *average sale price*
                for the matching chip/storage row when the page has one, else its headline "Average price". The page
                does not state the averaging window, so it is NOT a verified 30-day figure.
  spec_matrix   A hardware floor from CPU generation, RAM, storage and age. The dollar figures are the owner's rule of
                thumb (an assumption, not market data) and are meant as a floor for old enterprise laptops.
  metal_spot    Intrinsic melt value from the live spot price (gold-api.com, free, no key; the same source the scanner's
                scrap section uses). exchangerate-api.com has no gold feed, so it is not used.
"""
from __future__ import annotations

import json
import re
import time
from pathlib import Path

import httpx

SWAPPA = "https://swappa.com/prices/{}"
UA = "arbitrage-scanner/1.0 (personal price lookup; contact: owner)"
CACHE_PATH = Path(__file__).with_name("swappa_cache.json")
CACHE_TTL = 24 * 3600
OZT = 31.1035
SPOT_URL = "https://api.gold-api.com/price/{}"
SYMBOL = {"gold": "XAU", "silver": "XAG", "platinum": "XPT"}
# per gram of the PURE metal when the live feed is unreachable (gold/silver: the owner's benchmark; platinum: rough 2026 level)
FALLBACK_GRAM = {"gold": 133.0, "silver": 1.00, "platinum": 52.0}
_SPOT_CACHE: dict[str, tuple[float, float]] = {}


# ---------------------------------------------------------------- Swappa
_MONEY = r"\$\s?([\d,]+(?:\.\d+)?)"


def _text(html: str) -> str:
    t = re.sub(r"<script.*?</script>|<style.*?</style>", " ", html, flags=re.S | re.I)
    t = re.sub(r"<[^>]+>", " ", t)
    t = t.replace("&quot;", '"').replace("&amp;", "&")
    return re.sub(r"\s+", " ", t)


def parse_swappa(html: str, storage_gb: int | None = None, chip: str | None = None) -> float | None:
    """Average sale price from a Swappa price page. Prefers the table row for this chip + storage, then storage alone,
    then the page's headline average. None if the page has no price at all."""
    t = _text(html)
    rows = []  # (label, list price or None, sale price or None)
    for m in re.finditer(r"((?:Apple |Intel |AMD )?[A-Za-z0-9][A-Za-z0-9 .\-]{0,40}?/\s*)?(\d+)\s*(GB|TB)\s+(\$[\d,]+|-)\s+(\$[\d,]+|-)", t):
        gb = int(m.group(2)) * (1000 if m.group(3) == "TB" else 1)
        num = lambda s: None if s == "-" else float(s.replace("$", "").replace(",", ""))
        rows.append(((m.group(1) or "").lower(), gb, num(m.group(4)), num(m.group(5))))
    if storage_gb:
        want_chip = (chip or "").lower().strip()
        cands = [r for r in rows if r[1] == storage_gb and r[3]]
        if want_chip:
            tight = [r for r in cands if want_chip.replace("apple ", "") in r[0]]
            if tight:
                return sum(r[3] for r in tight) / len(tight)
        elif cands:
            return sum(r[3] for r in cands) / len(cands)
    m = re.search(r"Average price:\s*" + _MONEY, t)
    return float(m.group(1).replace(",", "")) if m else None


def _cache() -> dict:
    try:
        return json.loads(CACHE_PATH.read_text())
    except (OSError, ValueError):
        return {}


def fetch_swappa_free_comp(device_slug: str, storage_gb: int | None = None, chip: str | None = None,
                           http: httpx.Client | None = None) -> float | None:
    """Swappa average sale price (USD) for a device slug such as 'apple-ipad-air-5th-gen' or 'macbook-pro-2021-14'.

    Returns None when the page is missing (404), has no price, or Swappa is unreachable. Responses are cached on disk for
    24 hours and requests are made one at a time with a 1 second pause, so a batch is polite to the site.
    """
    key = f"{device_slug}|{storage_gb}|{chip}"
    cache = _cache()
    hit = cache.get(key)
    if hit and time.time() - hit["at"] < CACHE_TTL:
        return hit["price"]
    own = http is None
    http = http or httpx.Client(timeout=15, headers={"User-Agent": UA}, follow_redirects=True)
    try:
        time.sleep(1.0)
        r = http.get(SWAPPA.format(device_slug))
        price = parse_swappa(r.text, storage_gb, chip) if r.status_code == 200 else None
    except httpx.HTTPError:
        return None  # unreachable: do not cache a failure
    finally:
        if own:
            http.close()
    cache[key] = {"at": time.time(), "price": price}
    try:
        CACHE_PATH.write_text(json.dumps(cache))
    except OSError:
        pass
    return price


def swappa_slug_candidates(title: str, family: str = "") -> list[str]:
    """Plausible Swappa slugs for a lot title. Swappa's naming is irregular, so several are tried; a 404 just means 'no'."""
    t = title.lower()
    year = re.search(r"\b(20(?:1[5-9]|2[0-6]))\b", t)
    y = year.group(1) if year else None
    out: list[str] = []
    chip = re.search(r"\bm([1-4])\b", t)
    size = re.search(r"\b(13|14|15|16)(?:\.\d)?\s?(?:\"|”|inch|in\b|-inch)", t) or re.search(r"\b(?:macbook\s*(?:pro|air))\s*(13|14|15|16)\b", t)
    sz = size.group(1) if size else None
    if "macbook pro" in t or family == "macbook pro":
        years = [y] if y else ([] if not chip else {"1": ["2021", "2020"], "2": ["2023", "2022"], "3": ["late-2023", "2024"], "4": ["2024"]}[chip.group(1)])
        if not chip and not y and re.search(r"a2141", t):
            years = ["2019"]
        for yy in years:
            for s_ in ([sz] if sz else ["14", "16"]):
                out.append(f"macbook-pro-{yy}-{s_}")
    elif "macbook air" in t or family == "macbook air":
        years = [y] if y else ([] if not chip else {"1": ["2020"], "2": ["2022"], "3": ["2024", "2023"]}[chip.group(1)])
        for yy in years:
            out += [f"macbook-air-{yy}-{sz or '13'}"]
    elif "ipad air" in t or family == "ipad air":
        if re.search(r"5th|m1\b|2022", t):
            out.append("apple-ipad-air-5th-gen")
    elif "ipad pro" in t or family == "ipad pro":
        size_ = "13" if re.search(r"12\.9|13", t) else "11"
        gen = {"m1": "2021", "m2": "2022", "m4": "2024"}.get(chip and f"m{chip.group(1)}" or "", y or "")
        for s_ in (size_,):
            out += [f"apple-ipad-pro-{s_}-{n}" for n in (f"{gen}", "4th-gen-2022", "2024")] if gen else []
    elif "galaxy tab" in t:
        m = re.search(r"tab\s*s(\d+)\s*(ultra|\+|plus)?", t)
        if m:
            out.append(f"samsung-galaxy-tab-s{m.group(1)}" + ("-ultra" if m.group(2) == "ultra" else "-plus" if m.group(2) else ""))
    return list(dict.fromkeys(out))


# ---------------------------------------------------------------- spec matrix
_CPU_FLOOR = {"intel10": 120.0, "intel11": 180.0, "intel12": 260.0, "ryzen5000": 200.0, "m1": 420.0, "m2": 580.0}


def cpu_family_from_text(text: str) -> str | None:
    """'intel10' ... 'intel12' (12th gen or newer), 'ryzen5000', 'm1', 'm2' from a title/spec string; None if unsupported
    (Intel 9th gen and older, M3+, unknown)."""
    t = text.lower()
    m = re.search(r"\bm([1-2])\b(?:\s*(?:pro|max|ultra))?", t)
    if m:
        return f"m{m.group(1)}"
    m = re.search(r"\bi[3579]-(\d{4,5})[a-z0-9]{0,3}\b", t)
    if m:
        d = m.group(1)
        gen = int(d[:2]) if len(d) == 5 else int(d[0])  # i7-1185G7 -> 11, i7-10510U -> 10, i7-8650U -> 8
        if len(d) == 4 and d[:2] in ("10", "11", "12", "13", "14"):
            gen = int(d[:2])
        return None if gen < 10 else f"intel{min(gen, 12)}"
    m = re.search(r"\b(\d{1,2})(?:th|st|nd|rd)\s*gen", t)
    if m and "intel" in t or (m and re.search(r"\bcore\b", t)):
        gen = int(m.group(1))
        return None if gen < 10 else f"intel{min(gen, 12)}"
    m = re.search(r"ryzen\s*[3579]\s*(\d)\d{3}", t)
    if m and int(m.group(1)) >= 5:
        return "ryzen5000"
    return None


def evaluate_laptop_by_spec_matrix(brand: str, cpu_family: str, ram_gb: int, storage_gb: int, release_year: int = 2020) -> float:
    """Rule-of-thumb floor value (USD) for an enterprise laptop. Returns 0.0 when the CPU family is not in the matrix.

    floor(cpu) + 30 for 16GB+ RAM (+70 for 32GB+) + 25 for a 512GB SSD (+60 for 1TB+), then x 0.88 per year the laptop is
    older than 2022 (read as: years before 2022). `brand` is accepted for the record; the matrix is brand-agnostic.
    The figures are an assumption, not market data.
    """
    base = _CPU_FLOOR.get((cpu_family or "").lower())
    if base is None:
        return 0.0
    v = base + (70.0 if ram_gb >= 32 else 30.0 if ram_gb >= 16 else 0.0) + (60.0 if storage_gb >= 1000 else 25.0 if storage_gb >= 512 else 0.0)
    v *= 0.88 ** max(0, 2022 - int(release_year or 2020))
    return round(v, 2)


# ---------------------------------------------------------------- metal spot
def spot_price_gram_24k(metal: str = "gold", http: httpx.Client | None = None) -> tuple[float, str]:
    """(USD per gram of pure metal, source). Live from gold-api.com with a 15 minute cache; else the labelled fallback."""
    metal = metal.lower()
    hit = _SPOT_CACHE.get(metal)
    if hit and time.time() - hit[0] < 900:
        return hit[1], "live (cached)"
    own = http is None
    http = http or httpx.Client(timeout=10)
    try:
        r = http.get(SPOT_URL.format(SYMBOL[metal]))
        r.raise_for_status()
        per_gram = float(r.json()["price"]) / OZT
        if per_gram > 0:
            _SPOT_CACHE[metal] = (time.time(), per_gram)
            return per_gram, "live"
    except (httpx.HTTPError, KeyError, ValueError):
        pass
    finally:
        if own:
            http.close()
    return FALLBACK_GRAM[metal], "fallback benchmark"


def calculate_precious_metal_melt(weight_grams: float, metal: str = "gold", purity_karat: int = 18,
                                  purity_fraction: float | None = None, http: httpx.Client | None = None) -> dict:
    """melt_value = weight_grams x purity x spot price per gram of the pure metal.

    Gold purity is `purity_karat / 24`. For silver/platinum pass `purity_fraction` (sterling 0.925, Pt950 0.95).
    Melt is the metal's intrinsic value; a refiner or dealer pays only a share of it.
    """
    spot, source = spot_price_gram_24k(metal, http)
    purity = purity_fraction if purity_fraction is not None else (purity_karat / 24.0 if metal.lower() == "gold" else 1.0)
    return {"melt_value": round(weight_grams * purity * spot, 2), "spot_price_gram": round(spot, 4),
            "purity_pct": round(purity * 100, 2), "metal": metal.lower(), "spot_source": source}
