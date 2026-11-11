"""A local web page of the best current auctions to bid on, each with a link to bid and an estimate of how long it takes to SELL.

  python main.py --web                 build site/index.html from the catalog (instant; bids are as of the last crawl)
  python main.py --web --web-refresh   re-read every listing page first (~2 minutes): live bids, end times, test results
  python main.py --web --serve         also serve it at http://127.0.0.1:8765 (bound to this machine only)

The page is one self-contained HTML file (no external scripts, works offline). Titles come from third-party sites, so they are
only ever inserted as text, and only https links are allowed. Every number is an estimate: see the assumptions on the page itself.
"""
import os
import asyncio
import collections
import functools
import html
import http.server
import json
import re
import sqlite3
import threading
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import httpx

import catalog
import db
from engine import knowledge as kb
from engine import liquidity as lq
from clients.imei import cached_report
from engine.luxury_authenticator import LuxuryAuthenticator, detect_brand_and_tier
from engine.title_cleaner import build_search_query, extract_imei, extract_labelled_model, extract_models, is_mobile_device

ET = ZoneInfo("America/New_York")
PLATFORM_LABEL = {"txauction": "TXAuction", "shopgoodwill": "ShopGoodwill", "propertyroom": "PropertyRoom", "gsa": "GSA Auctions", "municibid": "Municibid", "publicsurplus": "Public Surplus", "govdeals": "GovDeals", "ebay": "eBay"}
HARD = re.compile(r"cracked|broken|not working|non[- ]?working|doesn'?t (work|turn|power)|does not (work|turn|power)|for parts|"
                  r"parts only|no power|activation lock|icloud|water damage|dead|bad (battery|screen)|needs? repair|shattered", re.I)
SOFT = re.compile(r"untested|as[- ]is|scratch|scuff|missing|worn|dent|no charger|no cord|no box", re.I)


def safe_url(url: str | None) -> str | None:
    """Only real https links: titles and URLs come from third-party pages and go into an HTML page."""
    return url if url and re.match(r"https://[^\s<>\"']+$", url) else None


def clean_text(page: str) -> str:
    page = re.sub(r"(?s)<(script|style)[^>]*>.*?</\1>", "", page or "")
    return re.sub(r"\s+", " ", html.unescape(re.sub(r"<[^>]+>", " ", page))).strip()


# ---------------------------------------------------------------- rows
# Surplus sites where most lots are local pickup: any shipping figure is an assumption, so the page asks for yours
PICKUP_PLATFORMS = {"municibid", "publicsurplus", "govdeals", "gsa", "propertyroom"}


# What it costs YOU to ship the sold item to its buyer (carrier label + packing), by item type. Rough US ground/priority figures for a
# 1-5 lb parcel, my estimates not quotes; heavy kinds (tv, speaker, printer...) keep the catalog's doubled rate. Edit per row on the page.
OUTBOUND_BY_KIND = {"phone": 7.0, "tablet": 12.0, "laptop": 20.0, "camera": 14.0, "lens": 12.0, "headphones": 9.0, "console": 16.0,
                    "game": 5.0, "gamepad": 8.0, "watch": 7.0, "ipod": 7.0, "vr": 16.0, "drone": 16.0, "shoes": 15.0, "bag": 14.0,
                    "wallet": 7.0, "clothing": 10.0, "accessory": 6.0, "calculator": 8.0, "microphone": 10.0, "storage": 8.0, "network": 12.0}
# Narrow the page to the item types you are working on (env FOCUS_KINDS="tablet,laptop" or --focus). Empty = everything.
FOCUS_KINDS = {k.strip() for k in os.getenv("FOCUS_KINDS", "").split(",") if k.strip()}
PER_BRAND = int(os.getenv("PAGE_MAX_PER_BRAND", "4"))  # at most this many rows per brand+item type, so one product line cannot fill the page
# ShopGoodwill sells DONATED, used soft goods ("general wear, may show discoloration, soles may show wear") and the sold comps behind our ranges
# lean toward cleaner pairs. Until a photo check exists, shoes/clothing/bags there are valued at this share of the typical sold price
# (an assumption, tune with env USED_SOFT_GOODS_FACTOR) unless the title says new / deadstock / unworn.
USED_SOFT_GOODS_FACTOR = float(os.getenv("USED_SOFT_GOODS_FACTOR", "0.85"))  # sold used/worn pairs ran ~70-120% of the pooled median (n=3-6 each): a mild haircut, not a measured one
_NEW_ITEM = re.compile(r"\b(nwt|nwb|nib|bnib|new with tags|new with box|new in box|new without tags|new without box|deadstock|brand new|"
                      r"sealed|unworn|never worn|mint condition)\b", re.I)
MAX_DAYS_LEFT = float(os.getenv("PAGE_MAX_DAYS", "2"))  # only auctions closing within this many days are listed
MIN_ROI = float(os.getenv("PAGE_MIN_ROI_PCT", "30")) / 100  # net profit / everything paid (bid, buyer fee, tax, shipping, repairs)
MIN_PROFIT = float(os.getenv("PAGE_MIN_PROFIT", "15"))  # ...and at least this many dollars, so a 30% return on a $5 item is not listed


def max_bid_roi(e: dict, value: float, factor: float, fix: float) -> float:
    """The highest bid that still leaves MIN_ROI on the all-in cost AND MIN_PROFIT dollars."""
    fee, order = catalog.fees_for(e, value * factor)
    resale = value * factor * (1 - fee - e.get("returns", 0.0)) - order - e["ship_out"]
    k = (1 + e["prem"]) * (1 + e.get("tax", 0.0))
    return (min(resale / (1 + MIN_ROI), resale - MIN_PROFIT) - e["inbound"] - fix) / k


def roi(e: dict, profit: float, bid: float, fix: float) -> float:
    cost = bid * (1 + e["prem"]) * (1 + e.get("tax", 0.0)) + e["inbound"] + fix
    return profit / cost if cost > 0 else 0.0


def recompute(row: dict) -> dict:
    """(Re)calculate every profit figure from the row's current bid, value range, condition factor and fix cost."""
    e, v = row["econ"], row["value"]
    f, fix, bid = row["factor"], row["fix"], row["bid"]
    ends_at = max(bid, e["climb"] * v["typ"])
    row["profit_now"] = round(catalog.net_profit(e, v["typ"], bid, f, fix), 2)
    row["profit_low"] = round(catalog.net_profit(e, v["low"], bid, f, fix), 2)
    cost_now = bid * (1 + e["prem"]) * (1 + e.get("tax", 0.0)) + e["inbound"] + fix
    row["roi_now"] = round(row["profit_now"] / cost_now, 3) if cost_now > 0 else 0.0
    row["ends_at"] = round(ends_at, 2)
    row["profit_end"] = round(catalog.net_profit(e, v["typ"], ends_at, f, fix) - row["liquidity"]["carry"], 2)
    row["max_bid"] = round(max_bid_roi(e, v["typ"], f, fix), 2)
    row["roi_end"] = round(roi(e, row["profit_end"], ends_at, fix), 3)
    # A lot nobody has bid on can still be won at today's price: it only "passes" if nobody else shows up
    ends_ok = row["profit_end"] >= MIN_PROFIT and row["roi_end"] >= MIN_ROI
    pn = row.get("p_nobid")  # measured share of comparable auctions that close with no bids; unknown elsewhere (PropertyRoom $1 lots climb)
    row["only_if_unbid"] = bool(row.get("bids") == 0 and pn is not None and pn >= 0.5 and row["profit_now"] >= MIN_PROFIT
                                and row["roi_now"] >= MIN_ROI and not ends_ok)
    if row.get("mobile"):  # what the phone would net if its IMEI turns out clean (100% of a clean comp)
        row["profit_now_clean"] = round(catalog.net_profit(e, v["typ"], bid, row.get("base_factor", 1.0), fix), 2)
        row["profit_if_clean"] = round(catalog.net_profit(e, v["typ"], ends_at, row.get("base_factor", 1.0), fix)
                                       - row["liquidity"]["carry"], 2)
    if row.get("luxury"):  # what it would net if the piece turned out to be genuine and verified
        base = row.get("lux_base", 1.0)
        row["profit_real_now"] = round(catalog.net_profit(e, v["typ"], bid, base, fix), 2)
        row["profit_real_end"] = round(catalog.net_profit(e, v["typ"], ends_at, base, fix) - row["liquidity"]["carry"], 2)
    return row


def designer_rows(conn: sqlite3.Connection, now: datetime | None = None, min_left_minutes: float = 30) -> list[dict]:
    """Every running priced designer piece (any tier but low-margin), shown whether or not the authenticity haircut leaves a profit."""
    now = now or datetime.now(timezone.utc)
    out = []
    for o in catalog.opportunities(conn, now=now):
        r = o["row"]
        if r["kind"] in ("accessory", "other") or not safe_url(r["url"]):
            continue
        if o["end"] is not None and (o["end"] - now).total_seconds() < min_left_minutes * 60:
            continue
        row = to_row(conn, o, now)
        if row.get("luxury") and row["luxury"]["tier"] != "low_margin":
            out.append(row)
    return out


# Measured on the 2026 Marshals designer auctions (TXAuction 31161/31313): similar lots closed at roughly 0.6-1.0 of the eBay sold
# typical (0.8 on average), so a bag is expected to climb to about 80% of its resale value. Melt-priced scrap closes near its payout value.
TX_CLIMB = {"bag": 0.80, "watch": 0.80, "jewelry": 0.95, "scrap": 1.00}
TX_SHIP_IN = {"bag": 35.0, "watch": 25.0, "jewelry": 25.0, "scrap": 15.0}
TX_KIND = {"handbag": "bag", "watch": "watch", "jewelry": "jewelry", "art": "art", "coins": "coin"}  # insured parcel from Texas: my estimate until the site quotes it


def _tx_row(auc: dict, lot: dict, kind: str, title: str, value: dict, bid: float, end: datetime, notes: list[str],
            econ_over: dict | None = None) -> dict:
    fee = catalog.ebay_fee_terms({"kind": "bag" if kind in ("bag", "jewelry") else kind, "title": ""})
    econ = {"prem": float(auc.get("premium") or 0.0), "inbound": TX_SHIP_IN.get(kind, 30.0), "tax": catalog.SALES_TAX, **fee,
            "returns": catalog.RETURN_RESERVE, "ship_out": OUTBOUND_BY_KIND.get(kind, 15.0), "climb": TX_CLIMB.get(kind, 0.90), **(econ_over or {})}
    row = {"title": title, "url": safe_url(lot.get("url") or auc.get("url")), "platform": "txauction", "platform_label": "TXAuction",
           "domain": "scrap" if kind == "scrap" else "fashion" if kind in ("bag", "watch") else "jewelry" if kind == "jewelry" else "collectibles", "kind": kind,
           "brand": "scrap metal" if kind == "scrap" else (value.get("brand") or kind), "style": "", "listing_id": f"{auc['id']}-{lot['lot']}",
           "bid": float(bid), "bids": lot.get("bids"), "end": end.isoformat(), "value": {"low": value["low"], "typ": value["typ"], "high": value["high"]},
           "conf": "low", "coarse": True, "range_notes": value.get("basis") or "", "econ": econ, "factor": 1.0, "fix": 0.0,
           "condition": "as is, no authenticity guarantee",
           "notes": list(notes) + [f"{econ['prem']:.1%} buyer's premium (from the auction page)".replace(".0%", "%") if econ["prem"] else "no buyer's premium (the auction page says so)"], "lot": False, "mobile": False, "query": title,
           "verified": "page", "group": f"txauction|{kind}", "ship_kind": "estimate", "p_nobid": None,
           "liquidity": {"days": 30.0, "low": 14.0, "high": 90.0, "grade": "C", "basis": "none", "reliable": False,
                         "text": "no sell-through data for this item", "notes": [], "carry": 0.0, "sold_30d": None, "active": None}}
    return recompute(row)


def tx_rows(snap: dict, scrap_auctions: list[dict] | None = None, gold_spot: float = 4165.0, now: datetime | None = None,
            values_path: str | Path = "txauction_values.json", min_left_minutes: float = 30,
            hidden_path: str | Path = "txauction_hidden.json", tracked_path: str | Path = "txauction_tracked.json") -> list[dict]:
    """Main-table rows for TXAuction lots we can value: hand-priced bags and watches, jewelry (gold melt + diamonds), and melt-priced scrap."""
    import jewelry as jw
    import txauction
    now = now or datetime.now(timezone.utc)
    try:
        values = json.loads(Path(values_path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        values = {}
    scrap_ids = {f"{a['id'].rsplit('-', 1)[-1]}-{l['lot']}" for a in (scrap_auctions or []) for l in a["lots"]}
    hidden = txauction.hidden_keys(hidden_path)
    tracked = set(txauction.tracked_lots(tracked_path))
    scrap_auctions = [{**a, "lots": [l for l in a["lots"] if f"{a['id'].rsplit('-', 1)[-1]}-{l['lot']}" not in hidden]} for a in (scrap_auctions or [])]
    out = []
    for a in snap.get("auctions", []):
        auc = a["auction"]
        auc = {**auc, "premium": auc.get("premium") or 0.0}
        for l in a["lots"]:
            key = f"{auc['id']}-{l['lot']}"
            try:
                end = datetime.fromisoformat((l.get("end") or "").replace("Z", "+00:00"))
            except ValueError:
                continue
            left = (end - now).total_seconds()
            if left <= 0 or (left < min_left_minutes * 60 and key not in tracked) or key in scrap_ids or key in hidden:
                continue  # lots on your own list stay visible until they actually end
            bid = l["bid"] if l["bid"] is not None else (l.get("start") or 0)
            l = {**l, "url": txauction.lot_url(auc["id"], l)}
            topic = txauction.topic_of(l)
            title = f"Lot {l['lot']}: {l['desc'][:150]}"
            v = values.get(key)
            if isinstance(v, dict) and topic in ("handbag", "watch", "jewelry"):
                kind = {"handbag": "bag", "watch": "watch", "jewelry": "jewelry"}[topic]
                over = {k: float(v[f]) for f, k in (("ship_in", "inbound"), ("ship_out", "ship_out")) if v.get(f) is not None}
                out.append(_tx_row(auc, l, kind, title, {"low": v["low"], "typ": v["value"], "high": v["high"], "basis": v.get("basis"),
                                                         "brand": v.get("brand")}, bid, end, [f"resale estimate: {v.get('basis')}"], over))
            elif topic == "jewelry" and jw.estimate(l["desc"], gold_spot):
                e = jw.estimate(l["desc"], gold_spot)
                if e:
                    out.append(_tx_row(auc, l, "jewelry", title, {"low": e["low"], "typ": e["mid"], "high": e["high"],
                                                                  "basis": f"gold melt + diamonds at ${jw.DIAMOND_PER_CT['low']:.0f}/${jw.DIAMOND_PER_CT['mid']:.0f}/${jw.DIAMOND_PER_CT['high']:.0f} per carat"},
                                       bid, end, [f"{e['karat']}K, {e['grams']:g} g, {e['cttw']:g} cttw (appraiser's estimate)"]))
    valued = {r["listing_id"] for r in out}
    for a in snap.get("auctions", []):
        auc = {**a["auction"], "premium": a["auction"].get("premium") or 0.0}
        for l in a["lots"]:
            key = f"{auc['id']}-{l['lot']}"
            topic = txauction.topic_of(l)
            if key in valued or key in scrap_ids or key in hidden or topic not in TX_KIND:
                continue
            try:
                end = datetime.fromisoformat((l.get("end") or "").replace("Z", "+00:00"))
            except ValueError:
                continue
            if (end - now).total_seconds() <= 0 or ((end - now).total_seconds() < min_left_minutes * 60 and key not in tracked):
                continue
            l = {**l, "url": txauction.lot_url(auc["id"], l)}
            bid = l["bid"] if l["bid"] is not None else (l.get("start") or 0)
            row = _tx_row(auc, l, TX_KIND[topic], f"Lot {l['lot']}: {l['desc'][:150]}", {"low": 0.0, "typ": 0.0, "high": 0.0, "basis": "no resale estimate yet"},
                          bid, end, ["no resale estimate yet: look up sold prices before bidding"])
            row["noval"] = True
            out.append(row)
    import scrap as sc
    for a in scrap_auctions or []:
        auc = {"id": a["id"].rsplit("-", 1)[-1], "premium": a.get("premium", 0.0), "url": a.get("url")}
        end = None
        try:
            end = datetime.fromisoformat((a.get("end") or "").replace("Z", "+00:00"))
        except ValueError:
            pass
        if end is None or (end - now).total_seconds() < min_left_minutes * 60:
            continue
        spot = a.get("spot") or {}
        for l in a["lots"]:
            melt = (l["fine_gold"] * (spot.get("gold") or 0) + l["fine_silver"] * (spot.get("silver") or 0)) / 31.1035
            pay = sc.DEFAULT_BULLION_PAYOUT if l.get("bullion") else sc.DEFAULT_PAYOUT
            typ = round(melt * sc.DEFAULT_METAL_SHARE * pay, 2)
            if typ <= 0:
                continue
            out.append(_tx_row(auc, {"lot": l["lot"], "bids": l["bids"], "url": l.get("url")}, "scrap", f"Lot {l['lot']}: {l['title']}",
                               {"low": round(typ * 0.9, 2), "typ": typ, "high": round(typ * 1.1, 2),
                                "basis": f"melt at spot x {sc.DEFAULT_METAL_SHARE:.0%} metal share x {pay:.0%} refiner payout"},
                               l["bid"], end, ["scrap metal sold to a refiner: no eBay fee, stated weights are gross"],
                               {"fee": 0.0, "order_fee": 0.0, "returns": 0.0, "ship_out": sc.DEFAULT_REFINER_SHIP}))
    mine = txauction.tracked_lots(tracked_path)
    for r in out:
        t = mine.get(r["listing_id"])
        if t is not None:
            r["tracked"] = True
            r["my_max"] = t.get("max_bid")
            r["track_note"] = t.get("note")
    return out


LOCAL_PICKUP_PLATFORMS = {"gsa", "govdeals", "municibid"}


def _gsa_near_chicago(r) -> bool:
    """Chicago-metro pickup location from 'City, ST 60601' (ZIP 600-608, or northwest Indiana). Without a ZIP only Illinois qualifies."""
    from clients.publicsurplus import near_chicago
    loc = (r["location"] if "location" in r.keys() else None) or ""
    m = re.search(r"\b([A-Z]{2})\s+(\d{5})\b", loc)
    if m:
        return near_chicago(m.group(1), m.group(2))
    return bool(re.search(r",\s*IL\b", loc))


def _passes(row: dict) -> bool:
    ok = row["profit_end"] >= MIN_PROFIT and row["roi_end"] >= MIN_ROI and row["profit_low"] > -10
    return ok or bool(row.get("only_if_unbid") and row["profit_low"] > -10)


def _unchecked_imei(row: dict) -> bool:
    """A phone whose listing prints a valid IMEI that nobody has checked yet: worth showing if it pays when clean."""
    return row.get("imei_verdict") == "unchecked" and row.get("profit_if_clean", 0) > 0


def _score(row: dict) -> float:
    """Potential profit in dollars if it ends near the expected price, trusting low-confidence ranges less.

    An unchecked-IMEI phone is scored on what it nets if the IMEI is clean (its upside); the red risk-adjusted figure stays visible.
    """
    conf = {"A": 1.0, "B": 1.0, "C": 0.7, "D": 0.0}.get(row["conf"], 0.7)
    base = row["profit_if_clean"] if (not _passes(row) and _unchecked_imei(row)) else row["profit_end"]
    return base * conf


_LUX = LuxuryAuthenticator()


def _apply_luxury(row: dict, text: str = "") -> None:
    """Authenticity tier for designer apparel / leather goods: caps the value factor and adds a confidence chip."""
    la = _LUX.assess(row["title"], text)
    if "lux_base" not in row:
        row["lux_base"] = row["factor"]
    if la is None:
        row.pop("luxury", None)
        row["factor"] = row["lux_base"]
        return
    row["factor"] = min(row["lux_base"], la.factor)
    row["luxury"] = {"brand": la.brand, "tier": la.tier, "factor": la.factor, "markers": la.markers,
                     "red_flags": la.red_flags, "concerns": la.concerns, "certilogo": la.certilogo,
                     "brand_tier": la.brand_tier, "max_bid": la.max_bid, "why": la.why,
                     "evidence_level": la.evidence_level, "evidence": la.evidence}


def _p_nobid(conn: sqlite3.Connection, platform: str, kind: str) -> float | None:
    """Share of comparable ShopGoodwill auctions that CLOSED WITH NO BIDS (measured, closed_auctions.measure); None elsewhere."""
    if platform != "shopgoodwill":
        return None
    try:
        r = conn.execute("SELECT unsold_share FROM climb_stats WHERE platform='shopgoodwill' AND kind=? AND n>=4", (kind,)).fetchone()
        if r and r[0] is not None:
            return round(float(r[0]), 2)
        allr = conn.execute("SELECT SUM(n*unsold_share), SUM(n) FROM climb_stats WHERE platform='shopgoodwill'").fetchone()
        return round(allr[0] / allr[1], 2) if allr and allr[1] else None
    except sqlite3.Error:
        return None


def to_row(conn: sqlite3.Connection, o: dict, now: datetime) -> dict:
    r, rng = o["row"], o["range"]
    stats = lq.latest_stats(conn, kb.item_key(build_search_query(r["title"])))
    est = lq.expected_days(stats, r["kind"], rng.typical, lq.calibration(conn, kb.item_key(build_search_query(r["title"]))) or lq.calibration(conn))
    carry_basis = o["econ"]
    if r["kind"] in OUTBOUND_BY_KIND:
        carry_basis = {**carry_basis, "ship_out": OUTBOUND_BY_KIND[r["kind"]]}
    row = {
        "title": r["title"], "url": safe_url(r["url"]), "platform": r["platform"],
        "platform_label": PLATFORM_LABEL.get(r["platform"], r["platform"]), "domain": r["domain"] or "fashion",
        "kind": r["kind"], "brand": r["brand"], "style": r["style"], "listing_id": r["listing_id"],
        "bid": float(r["current_bid"]), "bids": r["bids"], "end": o["end"].isoformat() if o["end"] else None,
        "value": {"low": rng.low, "typ": rng.typical, "high": rng.high}, "conf": rng.confidence,
        "coarse": bool(o["coarse"]), "range_notes": rng.notes, "econ": carry_basis, "factor": o["factor"], "fix": o.get("fix", 0.0),
        "condition": o["condition"], "notes": ([f"lot of {o['lot']['qty']}: " + ", ".join(o["lot"]["models"])] if o.get("lot") else []), "lot": bool(o.get("lot")), "mobile": is_mobile_device(r["title"]), "query": build_search_query(r["title"]), "verified": "catalog", "group": f"{r['brand']}|{r['style']}",
        "liquidity": {"days": round(est.days, 1), "low": round(est.low, 1), "high": round(est.high, 1), "grade": est.grade,
                      "basis": est.basis, "reliable": est.reliable, "text": est.text(), "notes": est.notes,
                      "carry": 0.0, "sold_30d": stats.sold_30d if stats else None, "active": stats.active if stats else None},
    }
    row["liquidity"]["carry"] = lq.carry_cost(max(row["bid"], carry_basis["climb"] * rng.typical) * (1 + carry_basis["prem"])
                                              + carry_basis["inbound"], est.days)
    shipping = r["shipping"] if "shipping" in r.keys() else 0
    row["ship_kind"] = ("quoted" if ((shipping or 0) > 0 and r["platform"] != "gsa") or (r["platform"] == "ebay" and shipping is not None) else
                        "pickup" if r["platform"] in PICKUP_PLATFORMS else "estimate")
    row["p_nobid"] = _p_nobid(conn, r["platform"], r["kind"])
    if r["platform"] == "shopgoodwill" and r["kind"] in ("shoes", "clothing", "bag", "wallet") and not _NEW_ITEM.search(r["title"]):
        row["factor"] = round(row["factor"] * USED_SOFT_GOODS_FACTOR, 4)
        row["notes"] = list(row.get("notes") or []) + [f"donated, used goods: priced at {USED_SOFT_GOODS_FACTOR:.0%} of typical sold; check the photos for wear"]
    _apply_luxury(row)
    models = extract_models(r["title"])
    if models:
        row["model"] = models[0].upper()
    return recompute(row)


def _apply_model(row: dict, text: str) -> None:
    """A model / style number printed in the listing text makes the eBay comparison exact: search the brand + that number."""
    if row.get("model"):
        return
    model = extract_labelled_model(text)
    if model:
        row["model"] = model.upper()
        row["query"] = f"{row['brand']} {model}".lower()
        row["notes_model"] = True


def collect(conn: sqlite3.Connection, limit: int = 100, per_style: int = 5, now: datetime | None = None,
            min_left_minutes: float = 30, mobile_candidates: bool = False, min_per_kind: int = 8,
            max_days_left: float = MAX_DAYS_LEFT) -> list[dict]:
    """The best running auctions: model-specific price ranges first, then brand-level estimates (flagged), capped per style."""
    now = now or datetime.now(timezone.utc)
    pool = []
    for o in catalog.opportunities(conn, now=now):
        r = o["row"]
        if r["platform"] in LOCAL_PICKUP_PLATFORMS and not _gsa_near_chicago(r):
            continue  # these sites mostly sell pickup-only (GSA never ships; you collect within ~10 days or forfeit 20%): only Chicago-area lots are usable
        if FOCUS_KINDS and r["kind"] not in FOCUS_KINDS and not o.get("lot"):
            continue
        if not o.get("lot") and (r["kind"] in ("accessory", "other") or o["condition"] not in ("working", "refurbished")):
            continue
        if o["end"] is not None and (o["end"] - now).total_seconds() < min_left_minutes * 60:
            continue
        if o["end"] is not None and (o["end"] - now).total_seconds() > max_days_left * 86400:
            continue  # closes too far out to bid on yet
        if not safe_url(r["url"]):
            continue
        pool.append(to_row(conn, o, now))
    # mobile_candidates: also carry phones that only pay if their IMEI is clean, so the live refresh can see whether the listing prints one
    pool = [p for p in pool if _passes(p) or (mobile_candidates and p.get("profit_if_clean", 0) > 0)]
    pool.sort(key=lambda p: -_score(p))
    taken: collections.Counter = collections.Counter()
    by_brand: collections.Counter = collections.Counter()
    sizes = collections.Counter(p["group"] for p in pool)
    out = []
    for p in pool:
        bk = (p["brand"], p["kind"])
        if taken[p["group"]] < per_style and by_brand[bk] < PER_BRAND:
            taken[p["group"]] += 1
            by_brand[bk] += 1
            p["similar"] = sizes[p["group"]] - per_style if sizes[p["group"]] > per_style else 0
            out.append(p)
        if len(out) >= limit:
            break
    return _number(_with_category_floor(out, pool, per_style, min_per_kind))


# Categories the user wants to browse even when they would not make the overall top N (a tablets view must not be empty)
FLOOR_KINDS = ("phone", "tablet", "laptop", "camera", "console", "watch")


def _with_category_floor(out: list[dict], pool: list[dict], per_style: int, min_per_kind: int) -> list[dict]:
    """Top up each FLOOR_KINDS category, and every bidding site, to `min_per_kind` rows from the profitable pool.

    Without this the biggest site (ShopGoodwill, thousands of listings) crowds PropertyRoom and GSA out of the top N.
    The overall list may then exceed its limit.
    """
    have = collections.Counter(r["kind"] for r in out)
    site = collections.Counter(r["platform"] for r in out)
    seen = {r["listing_id"] for r in out}
    styles = collections.Counter(r["group"] for r in out)
    brands = collections.Counter((r["brand"], r["kind"]) for r in out)
    for p in pool:
        if brands[(p["brand"], p["kind"])] >= PER_BRAND:
            continue  # the floor guarantees coverage of sites and kinds, not more of the same brand
        need_kind = p["kind"] in FLOOR_KINDS and have[p["kind"]] < min_per_kind
        if (need_kind or site[p["platform"]] < min_per_kind) and p["listing_id"] not in seen and styles[p["group"]] < per_style:
            out.append(p)
            seen.add(p["listing_id"])
            have[p["kind"]] += 1
            site[p["platform"]] += 1
            styles[p["group"]] += 1
            brands[(p["brand"], p["kind"])] += 1
    return out


def _number(rows: list[dict]) -> list[dict]:
    """Final order = best buys first across ALL sites and categories (the floor rows are merged in by score, not tacked on)."""
    rows.sort(key=lambda p: -_score(p))
    for i, p in enumerate(rows, 1):
        p["rank"] = i
    return rows


# ---------------------------------------------------------------- live refresh
def parse_pr_live(text: str) -> dict:
    """Current price, end time (UTC) and bid count from the TEXT of a PropertyRoom listing page."""
    out: dict = {}
    m = re.search(r"Current Price US \$([\d,]+\.\d{2})", text)
    if m:
        out["price"] = float(m.group(1).replace(",", ""))
    m = re.search(r"Ends ([A-Z][a-z]{2} \d{2}, \d{4} at \d{2}:\d{2} [AP]M)", text)
    if m:
        out["end"] = datetime.strptime(m.group(1), "%b %d, %Y at %I:%M %p").replace(tzinfo=ET).astimezone(timezone.utc)
    m = re.search(r"(\d+) Total Bid", text)
    if m:
        out["bids"] = int(m.group(1))
    return out


def parse_sgw_live(detail: dict) -> dict:
    """Current price, bid count, closed flag and description risk flags from ShopGoodwill's item-detail JSON."""
    desc = clean_text(detail.get("description") or "")
    return {"text": desc, "price": float(detail["currentPrice"]) if detail.get("currentPrice") else None,
            "bids": detail.get("numberOfBids"), "closed": bool((detail.get("bidHistory") or {}).get("auctionClosed")),
            "hard": sorted({m.group(0).lower() for m in HARD.finditer(desc)}),
            "soft": sorted({m.group(0).lower() for m in SOFT.finditer(desc)})}


def _apply_phone(row: dict, engine, text: str, notes: list[str], conn, disclaimer: bool = False) -> float | None:
    """Telecom risk for a phone row from its listing text, using a stored/manual IMEI result when there is one.

    Returns an extra factor cap for the caller to apply (None when nothing more is needed).
    """
    imei = extract_imei(f"{row['title']} {text}")
    report = cached_report(conn, imei) if imei else None
    tf, ts = engine.telecom_factor(row["title"], text, report)
    if imei:  # any electronics (laptop, tablet, watch...) that prints one: shown so it can be checked by hand
        row["imei"] = imei
        row["imei_verdict"] = report.verdict if report is not None and report.verdict != "unknown" else "unchecked"
    if not ts.mobile:
        return None
    row["telecom"] = ts.status
    known = report is not None and report.verdict != "unknown"
    if known:
        row["factor"] = tf  # a checked IMEI replaces the catalog's 50% default: 100% if clean, 20% if not
        notes.append(f"IMEI {imei} {report.verdict.upper()}" + (" (checked by hand)" if report.manual else "") + f": {tf:.0%} of a clean comp")
    else:
        notes.append(f"phone: {ts.status} telecom status, {tf:.0%} of a clean comp"
                     + (" (government telecom disclaimer)" if ts.unverified_telecom_status else ""))
        if ts.status != "clean":
            row["factor"] = min(row["factor"], tf)
    if imei:
        if not known:
            notes.append(f"IMEI {imei} printed but unchecked: check it by hand, then python main.py --set-imei {imei} --imei-verdict clean|bad")
    return None if (known or ts.status == "clean") else tf


_SGW_TOTAL = re.compile(r"Total Shipping and Handling:\s*[^\d<]{0,3}([\d,]+\.\d{2})")


async def sgw_shipping(http: httpx.AsyncClient, item_id: str, zip_code: str, state: str = "") -> float | None:
    """ShopGoodwill's own shipping calculator (the one on the item page): shipping + handling to your ZIP, or None.

    It returns the single method the seller set (usually FedEx/UPS ground), not a menu, and nothing for pickup-only lots."""
    try:
        resp = await http.post("https://buyerapi.shopgoodwill.com/api/ItemDetail/CalculateShipping",
                               json={"itemId": int(item_id), "country": "US", "province": state, "zipCode": zip_code, "quantity": 1, "clientIP": ""})
        m = _SGW_TOTAL.search(resp.json() if resp.headers.get("content-type", "").startswith("application/json") else resp.text)
    except (httpx.HTTPError, ValueError, TypeError):
        return None
    return float(m.group(1).replace(",", "")) if m else None


async def refresh(rows: list[dict], cfg, http: httpx.AsyncClient, pr_client=None, min_left_minutes: float = 30,
                  sgw_delay: float = 0.35, keep_ending: bool = False) -> list[dict]:
    """Re-read each listing: live bid, bid count, end time, test results / red flags. Returns the rows still worth showing."""
    from clients.propertyroom import PropertyRoomClient
    from models import Listing
    from engine.valuation import ValuationEngine
    engine = ValuationEngine(cfg)
    own = pr_client is None
    pr = pr_client or PropertyRoomClient(None, [], delay=0.8)
    now = datetime.now(timezone.utc)
    kept = []
    conn = db.connect(cfg.db_path)
    try:
        for row in rows:
            drop, factor, notes, page_text = False, None, [], ""
            try:
                if row["platform"] == "propertyroom":
                    resp = await pr._http.get(row["url"])
                    live = parse_pr_live(clean_text(resp.text))
                    if "price" in live:
                        row["bid"] = live["price"]
                    row["bids"] = live.get("bids", row["bids"])
                    if live.get("end"):
                        row["end"] = live["end"].isoformat()
                    a = await pr.assess(Listing("propertyroom", row["listing_id"], row["title"], row["bid"], 0, row["url"], None, "c"),
                                        cfg.fix_costs)
                    if a is None or not a.found:
                        notes.append("page layout not readable: test results unverified")
                        row["verified"] = "unverified"
                    else:
                        row["verified"] = "page"
                        if a.dead or a.activation_lock:
                            drop = True
                        elif a.untested:
                            factor = cfg.risky_condition_factor
                            notes.append("untested")
                        elif a.tested_ok:
                            notes.append("tested: powers on")
                        if a.activation_lock is False:
                            notes.append("no activation lock")
                        page_text = a.text
                        factor = _apply_phone(row, engine, a.text, notes, conn, a.telecom_disclaimer)
                        if a.missing:
                            row["fix"] = a.fix_cost
                            notes.append("missing: " + ", ".join(a.missing))
                        notes += a.notes
                elif row["platform"] == "municibid":
                    from clients.municibid import parse_detail
                    resp = await http.get(row["url"], follow_redirects=True)
                    live = parse_detail(resp.text, row["listing_id"])
                    if not live or live.get("ended"):
                        drop = bool(live)
                    else:
                        row["bid"], row["bids"] = live["price"], live["bids"]
                        row["end"] = live["end"].isoformat() if live["end"] else row["end"]
                        row["verified"] = "page"
                        notes.append("Municibid: local pickup usually required; 9% buyer's fee included")
                    await asyncio.sleep(0.8)
                elif row["platform"] == "publicsurplus":
                    from clients.publicsurplus import near_chicago, parse_detail as ps_detail
                    resp = await http.get(row["url"], follow_redirects=True)
                    live = ps_detail(resp.text)
                    if not live or live.get("ended"):
                        drop = bool(live)
                    else:
                        if live["price"] is not None:
                            row["bid"] = live["price"]
                        row["bids"] = live["bids"]
                        row["end"] = live["end"].isoformat() if live["end"] else row["end"]
                        row["verified"] = "page"
                        notes.append("Public Surplus: ~10.5% buyer's premium included")
                        if live.get("premium_pct") is not None:
                            row["econ"] = {**row["econ"], "prem": live["premium_pct"] / 100}
                        if live.get("free_shipping"):  # the seller ships free to a continental US address
                            row["econ"] = {**row["econ"], "inbound": 0.0}
                            row["ship_kind"] = "quoted"
                            live["ship_mode"] = "ships"
                            notes.append("Seller ships FREE (continental US)")
                        if live.get("drives_removed") and row["kind"] in ("laptop", "desktop"):
                            row["fix"] = row.get("fix", 0.0) + 35.0
                            notes.append("drive removed: add ~$35 for an SSD plus an OS install")
                        if live.get("no_power_supply") and row["kind"] in ("laptop", "tablet", "phone", "camera"):
                            row["fix"] = row.get("fix", 0.0) + 20.0
                            notes.append("no charger included: add ~$20")
                        if live.get("untested"):
                            factor = min(factor if factor is not None else 1.0, cfg.risky_condition_factor)
                            notes.append("seller did not test it beyond powering on: priced at the untested factor")
                        if live.get("needs_30_day_account"):
                            notes.append("seller requires your Public Surplus account to be 30+ days old before bidding")
                        local = near_chicago(live.get("pickup_state"), live.get("pickup_zip"))
                        if live["ship_mode"] == "pickup" and not local:
                            drop = True  # pickup-only and not in the Chicago area: can't be had
                        elif live["ship_mode"] == "pickup":
                            row["econ"] = {**row["econ"], "inbound": 0.0}
                            row["ship_kind"] = "local"
                            notes.append(f"Chicago-area pickup ({live['pickup_zip']}): no shipping, bring your own transport")
                        elif live["ship_mode"] in ("ask", "ships"):
                            row["ship_kind"] = "ask"
                            notes.append(f"Shipping: {live['ship_text']} (cost not listed: ask the agency, then type it in the shipping box)")
                    await asyncio.sleep(0.8)
                elif row["platform"] == "ebay":
                    from clients.ebay import EbayClient
                    ebay = EbayClient(http, cfg.ebay_client_id, cfg.ebay_client_secret, False, False)
                    live = await ebay.item_live(row["listing_id"])
                    if not live or live.get("ended"):
                        drop = bool(live)
                    else:
                        row["bid"], row["bids"] = live["price"], live["bids"]
                        row["end"] = live["end"].isoformat() if live["end"] else row["end"]
                        row["verified"] = "page"
                        notes.append("eBay: no buyer fee; eBay adds sales tax; seller's shipping used when it is a fixed price")
                    await asyncio.sleep(0.3)
                elif row["platform"] == "shopgoodwill":
                    resp = await http.get(f"https://buyerapi.shopgoodwill.com/api/ItemDetail/GetItemDetailModelByItemId/{row['listing_id']}")
                    live = parse_sgw_live(resp.json())
                    if live["price"]:
                        row["bid"] = live["price"]
                    row["bids"] = live["bids"]
                    if live["closed"] or live["hard"]:
                        drop = True
                    row["verified"] = "page"
                    notes.append("description: " + (", ".join(live["soft"][:3]) if live["soft"] else "no red flags"))
                    page_text = live["text"]
                    factor = _apply_phone(row, engine, live["text"], notes, conn)
                    await asyncio.sleep(sgw_delay)
                    if cfg.ship_to_zip and not live["closed"]:
                        quote = await sgw_shipping(http, row["listing_id"], cfg.ship_to_zip, cfg.ship_to_state)
                        if quote is not None:
                            row["econ"] = {**row["econ"], "inbound": quote}
                            row["ship_kind"] = "quoted"
                            notes.append(f"ShopGoodwill shipping quote to {cfg.ship_to_zip}: ${quote:.2f} (shipping + handling)")
                        await asyncio.sleep(sgw_delay)
            except (httpx.HTTPError, ValueError, KeyError, TypeError):
                notes.append("could not re-read the listing: figures are from the last crawl")
                row["verified"] = "stale"
            if not keep_ending and row["end"] and (datetime.fromisoformat(row["end"]) - now).total_seconds() < min_left_minutes * 60:
                drop = True
            if drop:
                continue
            _apply_luxury(row, page_text)
            _apply_model(row, page_text)
            if factor is not None:
                row["factor"] = min(row["factor"], factor)
            row["notes"] = notes
            kept.append(recompute(row))
    finally:
        conn.close()
        if own:
            await pr.aclose()
    return kept


def rerank(rows: list[dict], limit: int = 100, per_style: int = 5, min_per_kind: int = 8) -> list[dict]:
    rows = [r for r in rows if _passes(r) or _unchecked_imei(r)]
    rows.sort(key=lambda p: -_score(p))
    taken: collections.Counter = collections.Counter()
    by_brand: collections.Counter = collections.Counter()
    sizes = collections.Counter(p["group"] for p in rows)
    out = []
    for p in rows:
        bk = (p["brand"], p["kind"])
        if taken[p["group"]] < per_style and by_brand[bk] < PER_BRAND:
            taken[p["group"]] += 1
            by_brand[bk] += 1
            p["similar"] = max(0, sizes[p["group"]] - per_style)
            out.append(p)
        if len(out) >= limit:
            break
    return _number(_with_category_floor(out, rows, per_style, min_per_kind))


# ---------------------------------------------------------------- html
_TEMPLATE = r"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>Arbitrage: top auctions</title>
<style>
:root{--bg:#f6f7f9;--card:#fff;--ink:#1b1f24;--mute:#5b6570;--line:#e3e6ea;--accent:#1763d4;--good:#13795b;--warn:#a15c00;--bad:#b3261e;--chip:#eef1f5}
@media (prefers-color-scheme:dark){:root{--bg:#0f1317;--card:#171c22;--ink:#e8ecf0;--mute:#9aa5b1;--line:#2a323b;--accent:#6aa5ff;--good:#4cc79b;--warn:#f0b24f;--bad:#ff8a80;--chip:#222a33}}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--ink);font:14px/1.45 system-ui,-apple-system,Segoe UI,Roboto,sans-serif}
header,main,footer{max-width:1400px;margin:0 auto;padding:16px}
h1{font-size:20px;margin:0 0 4px}.sub{color:var(--mute)}
.note{background:var(--card);border:1px solid var(--line);border-left:4px solid var(--warn);border-radius:8px;padding:10px 14px;margin:12px 0}
.bar{display:flex;flex-wrap:wrap;gap:8px;align-items:center;margin:12px 0}
input,select{background:var(--card);color:var(--ink);border:1px solid var(--line);border-radius:6px;padding:6px 8px;font:inherit}
label{display:inline-flex;gap:6px;align-items:center;color:var(--mute)}
.wrap{overflow:auto;max-height:92vh;border:1px solid var(--line);border-radius:8px;background:var(--card)}
table{border-collapse:collapse;width:100%;min-width:1080px}
td.item{min-width:270px;max-width:380px}td.sells{min-width:150px;text-align:left}
th,td{padding:8px 8px;border-bottom:1px solid var(--line);text-align:left;vertical-align:top}
th{position:sticky;top:0;z-index:2;box-shadow:0 1px 0 var(--line);background:var(--card);cursor:pointer;white-space:nowrap;user-select:none;font-size:12px;color:var(--mute);text-transform:uppercase;letter-spacing:.03em}
th.sorted::after{content:" \25BE"}th.sorted.asc::after{content:" \25B4"}
td.num{text-align:right;white-space:nowrap;font-variant-numeric:tabular-nums}
tr.ended{opacity:.45}
.title{font-weight:600}.meta{color:var(--mute);font-size:12px}
.chip{display:inline-block;background:var(--chip);border-radius:999px;padding:1px 8px;font-size:12px;margin:2px 4px 0 0}
.grade{display:inline-block;min-width:22px;text-align:center;border-radius:6px;padding:1px 6px;font-weight:700;color:#fff}
.gA{background:#13795b}.gB{background:#3b8a2e}.gC{background:#b7791f}.gD{background:#b3261e}.g\?{background:#6b7480}
tr.mine td{background:color-mix(in srgb,var(--accent) 9%,transparent)}
a{color:var(--accent)}a.bid{color:#fff}
.pos{color:var(--good);font-weight:600}.neg{color:var(--bad)}.warn{color:var(--warn)}
a.bid{display:inline-block;background:var(--accent);color:#fff;text-decoration:none;border-radius:6px;padding:5px 10px;font-weight:600;white-space:nowrap}
.dim{color:var(--mute)}details{margin-top:6px}summary{cursor:pointer;color:var(--mute)}
input.ship{width:3.4em;font:inherit;padding:0 2px;background:transparent;color:inherit;border:1px solid #8884;border-radius:3px;text-align:right}

@media (max-width:760px){
  header,main,footer{padding:12px}
  .wrap{overflow:visible;max-height:none;border:0;background:transparent}
  table{min-width:0}
  table thead{display:none}
  table,table tbody,table tr,table td{display:block;width:100%}
  table tr{background:var(--card);border:1px solid var(--line);border-radius:12px;margin:0 0 12px;padding:8px 12px}
  table td{border:0;padding:5px 0;display:grid;grid-template-columns:42% 1fr;column-gap:12px;align-items:baseline}
  table td::before{content:attr(data-label);color:var(--mute);font-size:12px;grid-column:1;grid-row:1;text-align:left}
  table td>*{grid-column:2;text-align:right;min-width:0}
  table td.item{display:block;min-width:0;max-width:none}table td.item::before{display:none}table td.item>*{text-align:left}
  table td.num,table td.sells{text-align:right;white-space:normal}
  table td:empty{display:none}
  .bar label{width:100%}
}

#chips{display:none;flex-direction:column;gap:6px;width:100%}
.fgroup{display:flex;align-items:center;gap:8px;min-width:0}
.flabel{flex:0 0 78px;color:var(--mute);font-size:12px}
.chips{display:flex;gap:6px;overflow-x:auto;padding:2px 0 4px;min-width:0;scrollbar-width:thin}
.fchip{flex:0 0 auto;border:1px solid var(--line);background:var(--card);color:var(--ink);border-radius:999px;padding:5px 12px;font:inherit;cursor:pointer;white-space:nowrap}
.fchip.zero{opacity:.45}
.fchip[aria-pressed="true"]{background:var(--accent);border-color:var(--accent);color:#fff}
details.more{width:100%}details.more>summary{cursor:pointer;color:var(--ink);font-weight:600;padding:6px 0}
.morebar{display:flex;flex-wrap:wrap;gap:8px 16px;align-items:center;padding:4px 0 8px}
@media (max-width:760px){
  .flabel{flex-basis:64px}
  .fchip{padding:8px 14px}
  input,select{min-height:40px}
  .morebar label{width:100%}
}
</style></head><body>
<header><h1>Top auctions to bid on</h1>
<div class="sub" id="meta"></div>
<div class="note"><b>Read this first.</b> Every figure is an estimate. <b>Worth</b> comes from price ranges in the local database (<i>model-specific</i> when we researched that model, otherwise a <i>brand-level</i> median that can be far off). Auctions keep climbing: <b>Max bid</b> is the most you can pay and still keep ~$15. <b>Profit if it ends near</b> assumes the auction rises to a typical share of resale value (a rough rule from a few observations). <b>Sells in</b> is an expected time for a correctly priced listing: it is a category default unless it says it used market data or your own sales, and has not been validated yet. Nothing here checks authenticity or condition beyond the listing text.</div>
<div class="bar">
<input id="q" type="search" placeholder="Search items" aria-label="Search">
<select id="tier" aria-label="Fashion"><option value="">All items</option><option value="designer">Designer fashion</option><option value="everyday">Nike and everyday brands</option></select>
<select id="plat" aria-label="Platform"><option value="">All sites</option></select>
<select id="kind" aria-label="Item type"><option value="">All item types</option></select>
<select id="dom" aria-label="Category"><option value="">All categories</option></select>
<select id="grade" aria-label="Sells in"><option value="">Any sell time</option><option value="B">Sells within ~30 days</option><option value="C">Within ~60 days</option></select>
<div id="chips" aria-label="Filters"></div>
<details id="more" class="more"><summary>More filters</summary><div class="morebar">
<label><input type="checkbox" id="spec"> Model-specific prices only</label>
<label><input type="checkbox" id="hideEnded" checked> Hide ended</label>
<label><input type="checkbox" id="mine"> My list only</label>
<label title="Lots with no bids yet can often be won at today's price: 64-87% of comparable ShopGoodwill auctions closed with no bids at all. Show lots that clear your rules only if nobody else bids."><input type="checkbox" id="nobid"> Include no-bid lots (pay only if nobody else bids)</label>
<label title="Hide auctions that close later than this. Your tracked rows always show."><input type="checkbox" id="soon" checked> Ends within <span id="soonv"></span> days</label>
<label title="Hide rows whose profit is less than this share of everything you would pay (bid, buyer fee, tax, shipping) if the auction ends near its expected price, or under $15. Your tracked rows always show."><input type="checkbox" id="minp" checked> Return at least <span id="minpv"></span>%</label>
<label title="Applied to the bid plus the buyer fee. Max bid, all-in cost and every profit include it.">Sales tax % <input id="tax" type="number" min="0" max="15" step="0.25" style="width:4.2em"></label>
<label title="Texas auction lots that have no resale estimate yet are listed with question marks; tick to hide them"><input type="checkbox" id="noest"> Hide lots with no estimate</label>
<label><input type="checkbox" id="lux"> Designer only</label>
</div></details>
<span class="dim" id="count"></span> <a href="#watchbox" id="gowatch" class="bid" style="margin-left:10px;display:none">Watch list: <b id="gowatchn">0</b> lots ↓</a> <a href="#" id="gotx" class="bid" style="margin-left:6px;display:none" title="Shows every valued TXAuction lot in the main table, ignoring the return and closing-date filters">TXAuction in the table: <b id="gotxn">0</b> lots</a> <a href="#extbox" id="goext" class="bid" style="margin-left:6px;display:none">Other sources: <b id="goextn">0</b> verified ↓</a> <a href="#repbox" id="gorep" class="bid" style="margin-left:6px;display:none">Repair plays: <b id="gorepn">0</b> ↓</a> <a href="#unvbox" id="gounv" class="bid" style="margin-left:6px;display:none">Unverified claims: <b id="gounvn">0</b> ↓</a> <a href="#luxbox" id="golux" class="bid" style="margin-left:6px;display:none">Designer: <b id="goluxn">0</b> verified ↓</a> <a href="#fbbox" id="gofb" class="bid" style="margin-left:6px;display:none">Facebook: <b id="gofbn">0</b> verified ↓</a> <a href="#picksbox" id="gopicks" class="bid" style="margin-left:6px;display:none">Pricer picks: <b id="gopicksn">0</b> ↓</a> <a href="#scrapbox" id="goscrap" class="bid" style="margin-left:6px;display:none">Scrap lots: <b id="goscrapn">0</b> ↓</a></div></header>
<main><div class="wrap"><table id="t"><thead><tr id="head"></tr></thead><tbody id="body"></tbody></table></div>
<div class="wrap" id="watchbox" style="margin-top:28px" hidden>
<h2 style="font-size:1.05rem;margin:0 0 4px">Watch list: lots without an eBay sold price</h2>
<p class="sub" style="margin:0 0 10px">Two groups: lots near Chicago from BidProwl (GovDeals, Public Surplus, Municibid, GSA and others), and TXAuction lots that ship from Texas (US Marshals designer goods, jewelry, watches, coins, art). Not scored against eBay sold prices. A resale estimate shows where one exists: a hand-set value from sold lookups, or gold melt plus diamonds for single-piece jewelry. Pick Source = TXAuction to see the new auctions.</p>
<label>Source <select id="wsrc"><option value="">all</option></select></label> <label>Type <select id="wtopic"><option value="">all</option></select></label> <label>Distance <select id="wband"><option value="">all</option><option value="near">near only</option><option value="far">worth a drive only</option><option value="ships">ships from Texas</option></select></label> <label><input type="checkbox" id="wval"> Only lots with a resale estimate</label> <span class="dim" id="wcount"></span>
<table id="wt"><thead><tr><th>Item</th><th>Type</th><th>Source</th><th>Bid now</th><th>Bids</th><th>Est. resale (low-high)</th><th>Ends in</th><th>Where</th></tr></thead><tbody id="wbody"></tbody></table>
</div>
<div class="wrap" id="extbox" style="margin-top:28px" hidden>
<h2 style="font-size:1.05rem;margin:0 0 4px">Other auction sources: Treasury (CWS), HiBid, Illinois iBid, CTBids (buyer's premium read per sale)</h2>
<p class="sub" style="margin:0 0 10px" id="extnote"></p>
<h3 style="font-size:.95rem;margin:6px 0 4px">Where each buy source's data comes from</h3>
<table id="extst"><thead><tr><th>Source</th><th>Status</th><th>How it is fetched / what blocks automation</th></tr></thead><tbody id="extsbody"></tbody></table>
<h3 style="font-size:.95rem;margin:14px 0 4px">Verified</h3>
<table id="extvt"><thead><tr><th>Item</th><th>Source and sale terms</th><th>Expected price (all-in)</th><th>Value basis</th><th>Net</th><th>Max bid</th></tr></thead><tbody id="extvbody"></tbody></table>
<h3 style="font-size:.95rem;margin:18px 0 4px">Unverified</h3>
<table id="extut"><thead><tr><th>Item</th><th>Source and sale terms</th><th>Bid</th><th>Failed check</th><th>Why</th></tr></thead><tbody id="extubody"></tbody></table>
<h3 style="font-size:.95rem;margin:18px 0 4px">Estate sale leads (discovery only: not priced)</h3>
<table id="extlt"><thead><tr><th>Lead</th><th>Source</th><th>Where</th><th>When</th><th>Company</th><th>Photos</th><th>Notes</th></tr></thead><tbody id="extlbody"></tbody></table>
</div>
<div class="wrap" id="repbox" style="margin-top:28px" hidden>
<h2 style="font-size:1.05rem;margin:0 0 4px">Repair plays: auction lots that state a repairable defect</h2>
<p class="sub" style="margin:0 0 10px" id="repnote"></p>
<table id="rept"><thead><tr><th>Item</th><th>Bid / expected close (all-in)</th><th>As-is (parts) value</th><th>Repaired value and repair estimate</th><th>Net</th><th>Max bid</th><th>Demand</th><th>Verdict</th></tr></thead><tbody id="repbody"></tbody></table>
</div>
<div class="wrap" id="unvbox" style="margin-top:28px" hidden>
<h2 style="font-size:1.05rem;margin:0 0 4px">Unverified claims: old page numbers that failed the verified gate</h2>
<p class="sub" style="margin:0 0 10px" id="unvnote"></p>
<div class="bar" style="margin-bottom:8px"><input id="unvq" type="search" placeholder="Search" aria-label="Search unverified"> <label>Failed check <select id="unvfail"><option value="">any</option></select></label> <span class="dim" id="unvcount"></span></div>
<table id="unvt"><thead><tr><th>Item</th><th>Site</th><th>Bid</th><th>Old claim (UNVERIFIED)</th><th>Failed check</th><th>Why</th></tr></thead><tbody id="unvbody"></tbody></table>
</div>
<div class="wrap" id="luxbox" style="margin-top:28px" hidden>
<h2 style="font-size:1.05rem;margin:0 0 4px">Designer bags and clothing: authenticity first, then damage, condition-matched comps and the right channel</h2>
<p class="sub" style="margin:0 0 10px" id="luxnote"></p>
<h3 style="font-size:.95rem;margin:10px 0 4px">Verified (authenticity supported, no value-killing damage, every check passed)</h3>
<table id="luxvt"><thead><tr><th>Item</th><th>Price at expected close (all-in)</th><th>Authenticity</th><th>Damage and repair</th><th>Value basis (condition-matched comps)</th><th>Channel and demand</th><th>Expected net</th><th>Max price</th></tr></thead><tbody id="luxvbody"></tbody></table>
<h3 style="font-size:.95rem;margin:18px 0 4px">Unverified: failed a check (not profitable until resolved)</h3>
<table id="luxut"><thead><tr><th>Item</th><th>Bid</th><th>Failed check</th><th>Why</th><th>Next step</th></tr></thead><tbody id="luxubody"></tbody></table>
</div>
<div class="wrap" id="fbbox" style="margin-top:28px" hidden>
<h2 style="font-size:1.05rem;margin:0 0 4px">Facebook Marketplace: local pickup, strict confidence bar</h2>
<p class="sub" style="margin:0 0 10px" id="fbnote"></p>
<div class="bar" style="margin-bottom:8px"><input id="fbq" type="search" placeholder="Search Marketplace items" aria-label="Search Marketplace">
<label>Channel <select id="fbch"><option value="">all</option><option value="eBay shipped">eBay shipped</option><option value="local cash">local cash</option></select></label>
<label>Failed check <select id="fbfail"><option value="">any</option></select></label> <span class="dim" id="fbcount"></span></div>
<h3 style="font-size:.95rem;margin:10px 0 4px">Verified profitable (every check passed)</h3>
<table id="fbvt"><thead><tr><th>Item</th><th>Ask (buy)</th><th>Sold-comp value</th><th>Sold comps</th><th>Demand</th><th>Best channel</th><th>Net profit</th><th>Return</th><th>Max buy price</th><th>Identity and play</th></tr></thead><tbody id="fbvbody"></tbody></table>
<h3 style="font-size:.95rem;margin:18px 0 4px">Unverified: needs pricing check (not profitable until the failed check is resolved)</h3>
<table id="fbut"><thead><tr><th>Item</th><th>Ask</th><th>Failed check</th><th>Why</th><th>Next step</th></tr></thead><tbody id="fbubody"></tbody></table>
</div>
<div class="wrap" id="picksbox" style="margin-top:28px" hidden>
<h2 style="font-size:1.05rem;margin:0 0 4px">Spec-matched pricer: laptops, tablets, drones, cameras, luxury</h2>
<p class="sub" style="margin:0 0 10px">Value = median of stored eBay SOLD sales whose titles agree on chip, RAM, storage and screen. <b>Max bid</b> keeps 25% on cost; <b>$100 bid</b> is the most that still nets $100. Profit is at today's bid, which usually rises; trust the max bids. Condition and photos are not checked.</p>
<div class="bar" style="margin-bottom:8px"><label><input type="checkbox" id="pk100" checked> Net profit $100 or more only</label> <label><input type="checkbox" id="pkskip"> Also show lots with no stored comps (coverage gaps)</label> <span class="dim" id="pkcount"></span></div>
<table id="pkt"><thead><tr><th>Item</th><th>Source</th><th>Bid</th><th>Sold-price value</th><th>Max bid (25%)</th><th>Bid for $100</th><th>Net profit now</th><th>Sold comps</th><th>Ends</th></tr></thead><tbody id="pkbody"></tbody></table>
</div>
<div class="wrap" id="scrapbox" style="margin-top:28px" hidden>
<h2 style="font-size:1.05rem;margin:0 0 4px">Scrap gold and silver: valued by melt, not eBay</h2>
<p class="sub" style="margin:0 0 10px" id="scrapnote"></p>
<div class="bar" style="margin-bottom:8px">
<label>Gold $/oz <input id="sp_au" type="number" min="0" step="1" style="width:5.5em"></label>
<label>Silver $/oz <input id="sp_ag" type="number" min="0" step="0.1" style="width:5em"></label>
<label title="Share of melt a refiner or dealer pays. Bullion rounds use 95%.">Payout % <input id="sp_pay" type="number" min="50" max="100" step="1" style="width:4em"></label>
<label title="Stated weights are gross: clasps, solder and any stones count in the grams. 100% trusts the listing exactly.">Metal share % <input id="sp_share" type="number" min="20" max="100" step="1" style="width:4em"></label>
<label title="Insured shipping of the won lots from Texas, per lot. One parcel for several lots costs less.">Shipping in $ <input id="sp_ship" type="number" min="0" step="1" style="width:4em"></label>
<label title="Insured shipping to the refiner">To refiner $ <input id="sp_ref" type="number" min="0" step="1" style="width:4em"></label>
<label><input type="checkbox" id="sp_pass"> Only lots that clear 30% and $15</label>
<span class="dim" id="sp_count"></span></div>
<table id="st"><thead><tr><th>Lot</th><th>Item</th><th>Bid now</th><th>Bids</th><th>Melt</th><th>All-in cost</th><th>Net from refiner</th><th>Profit</th><th>Return</th><th>Max bid</th></tr></thead><tbody id="sbody"></tbody></table>
</div></main>
<footer class="sub">Fees assumed (eBay non-store, by category): 13.6% + $0.40 per order for most items, guitars 6.7%, handbags and watches 15%, trading cards/coins 13.25%, sneakers 8% with no order fee at $150+; plus a 3% returns reserve; $10 inbound shipping on ShopGoodwill ($20 heavy), $15 + 15% premium on PropertyRoom; $15 outbound ($30 heavy). Shipping is an assumption unless the listing quotes it: type the real figure in the box under 'Bid now' and the profits update ('all-in' = bid + buyer fee + shipping). Carrying cost 25%/yr. All sales on these sites are final. Generated locally; nothing on this page is financial advice.</footer>
<script id="data" type="application/json">__DATA__</script>
<script>
"use strict";
const D = JSON.parse(document.getElementById("data").textContent);
const rows = D.rows;
const COLS = [
 ["rank","#",r=>r.rank,"num"],["title","Item",null,""],["platform_label","Bid at",r=>r.platform_label,""],
 ["bid","Bid now",r=>r.bid,"num"],["end","Ends in",r=>r.end?Date.parse(r.end):Infinity,"num"],
 ["max_bid","Max bid (all-in)",r=>r.max_bid,"num"],["worth","Worth (typical)",r=>r.value.typ,"num"],
 ["profit_now","Profit at bid",r=>r.profit_now,"num"],["profit_end","Profit if it ends near",r=>r.profit_end,"num"],
 ["days","Sells in",r=>r.liquidity.days,"num"]];
let sortKey="end", asc=true;  // soonest-closing first, so nothing is missed
const $=id=>document.getElementById(id);
const money=n=>(n<0?"-":"")+"$"+Math.abs(Math.round(n)).toLocaleString();
function left(r){ if(!r.end) return {t:"?",s:Infinity}; const s=(Date.parse(r.end)-Date.now())/1000;
  if(s<=0) return {t:"ended",s}; const d=Math.floor(s/86400),h=Math.floor(s%86400/3600),m=Math.floor(s%3600/60);
  return {t:d?`${d}d ${h}h`:h?`${h}h ${m}m`:`${m}m`,s}; }
function labelTable(id){const t=document.getElementById(id); if(!t) return; const heads=[...t.querySelectorAll("thead th")].map(th=>th.textContent.trim());
  t.querySelectorAll("tbody tr").forEach(tr=>[...tr.children].forEach((td,i)=>{td.dataset.label=heads[i]||"";}));}
function el(tag,cls,text){const e=document.createElement(tag); if(cls) e.className=cls; if(text!==undefined) e.textContent=text; return e;}
function sellsCell(r){ const L=r.liquidity, td=el("td","num sells"); const g=el("span","grade g"+L.grade,L.grade); td.append(g," ",
  el("span","", `~${Math.round(L.days)}d`)); const sub=el("div","meta", `${Math.round(L.low)}-${Math.round(L.high)}d · `+
  (L.basis==="prior"?"default estimate":L.basis==="calibrated"?"your sales":"market data")+(L.reliable?"":" · unproven"));
  td.title=L.text+(L.sold_30d!=null?` | ${L.sold_30d} sold/30d, ${L.active} active`:""); td.append(sub); return td; }
const shipKey=r=>"ship:"+r.platform+":"+r.listing_id, outKey=r=>"out:"+r.platform+":"+r.listing_id;
let TAX=D.tax; try{const t=localStorage.getItem("tax"); if(t!==null&&t!==""&&!isNaN(+t)) TAX=+t/100;}catch(e){}
function feesAt(c,sale){return (c.hi_at&&sale>=c.hi_at)?[c.fee_hi,0]:[c.fee,c.order_fee];}
function net(r,v,bid,f){const c=r.cost, [fe,of]=feesAt(c,v*f); return v*f*(1-fe-c.returns)-of-r.out_in-(bid*(1+c.prem)*(1+TAX)+r.ship_in)-r.fix;}
function calc(r){  // every profit figure and the max bid from the row's raw numbers, YOUR shipping and the page's sales tax
  const c=r.cost, v=r.value, bid=r.bid; r.ends_at=Math.max(bid,c.climb*v.typ);
  r.profit_now=net(r,v.typ,bid,r.factor); r.profit_low=net(r,v.low,bid,r.factor);
  r.profit_end=net(r,v.typ,r.ends_at,r.factor)-r.carry;
  const cost0=bid*(1+c.prem)*(1+TAX)+r.ship_in+r.fix; r.roi_now=cost0>0?r.profit_now/cost0:0;
  const [fe0,of0]=feesAt(c,v.typ*r.factor), resale=v.typ*r.factor*(1-fe0-c.returns)-of0-r.out_in, k=(1+c.prem)*(1+TAX);
  r.max_bid=(Math.min(resale/(1+D.minRoi),resale-D.minProfit)-r.ship_in-r.fix)/k;
  r.roi_end=r.profit_end/(r.ends_at*k+r.ship_in+r.fix);
  r.only_if_unbid=(r.bids===0&&r.p_nobid!=null&&r.p_nobid>=0.5&&r.profit_now>=D.minProfit&&r.roi_now>=D.minRoi&&!(r.profit_end>=D.minProfit&&r.roi_end>=D.minRoi));
  if(r.mobile&&r.profit_if_clean!=null){const bf=r.base_factor!=null?r.base_factor:1; r.profit_now_clean=net(r,v.typ,bid,bf); r.profit_if_clean=net(r,v.typ,r.ends_at,bf)-r.carry;}
  if(r.luxury){const lb=r.lux_base!=null?r.lux_base:1; r.profit_real_now=net(r,v.typ,bid,lb); r.profit_real_end=net(r,v.typ,r.ends_at,lb)-r.carry;}}
function setOut(r,val){r.out_in=val==null?r.cost.ship_out:val; r.out_set=val!=null; calc(r);}
function setShip(r,val){r.ship_in=val==null?r.cost.inbound:val; r.ship_set=val!=null; calc(r);}
function recalcAll(){for(const r of rows) calc(r);}
for(const r of rows){let v=null,o=null; try{v=localStorage.getItem(shipKey(r)); o=localStorage.getItem(outKey(r));}catch(e){}
  r.out_in=o!==null&&o!==""&&!isNaN(+o)?+o:r.cost.ship_out; r.out_set=o!==null&&o!==""&&!isNaN(+o); setShip(r,v!==null&&v!==""&&!isNaN(+v)?+v:null);}
function row(r){
  const tr=el("tr"); const L=left(r); if(L.s<=0) tr.className="ended"; else if(r.tracked) tr.className="mine";
  tr.append(el("td","num",String(r.rank)));
  const t=el("td","item"); t.append(el("div","title",r.title));
  const meta=el("div","meta"); meta.append(`${r.brand} · ${r.kind}`);
  if(r.similar) meta.append(` · +${r.similar} similar listings`); t.append(meta);
  const chips=[]; if(r.tracked) chips.push(["★ on your list","good"]); chips.push(r.coarse?["brand-level price","warn"]:["model price","good"]);
  chips.push(["price confidence "+r.conf,""]); chips.push(r.verified==="page"?["listing page checked","good"]:r.verified==="catalog"?["not re-read yet","warn"]:r.verified==="stale"?["could not re-read","warn"]:["test results unverified","warn"]);
  if(r.bids===0){chips.push([r.only_if_unbid?"no bids yet: pays only if nobody bids":"no bids yet",r.only_if_unbid?"warn":"good"]);}
  if(r.imei) chips.push(["IMEI "+(r.imei_verdict==="unchecked"?"unchecked":r.imei_verdict),r.imei_verdict==="clean"?"good":r.imei_verdict==="bad"?"bad":"warn"]);
  if(r.luxury){const Lx=r.luxury; chips.push(["authenticity: "+Lx.tier+" · "+Math.round(Lx.factor*100)+"% confidence",Lx.tier==="verified"?"good":(Lx.tier==="counterfeit"||Lx.tier==="low_margin")?"bad":"warn"]);}
  const cw=el("div"); for(const [c,k] of chips){const s=el("span","chip "+k,c); cw.append(s);} t.append(cw);
  if(r.imei){const m=el("div","meta","IMEI "); m.append(el("code","",r.imei));
    if(r.imei_verdict==="unchecked"){m.append(r.profit_if_clean!=null?` · nets ${money(r.profit_if_clean)} if clean · check at `:" · check at ");
      const a=el("a","","CTIA Stolen Phone Checker ↗"); a.href="https://www.stolenphonechecker.org/"; a.target="_blank"; a.rel="noopener noreferrer"; m.append(a);
      m.append(el("div","meta","then: python main.py --set-imei "+r.imei+" --imei-verdict clean|bad"));}
    t.append(m);}
  if(r.luxury){const Lx=r.luxury, bits=[];
    if(Lx.red_flags.length) bits.push("RED FLAGS: "+Lx.red_flags.join(", "));
    bits.push(Lx.evidence_level==="third_party"?"EVIDENCE: independent ("+Lx.evidence.join(", ")+")":
              Lx.evidence_level==="documented"?"EVIDENCE: seller documents "+Lx.evidence.join(", ")+" (still not independent)":
              Lx.evidence_level==="claimed"?"EVIDENCE: only "+Lx.evidence.join(", "):"EVIDENCE: no proof in the listing");
    if(Lx.certilogo) bits.push("Certilogo "+Lx.certilogo);
    if(Lx.markers.length) bits.push("seller claims: "+Lx.markers.join(", "));
    if(Lx.concerns.length) bits.push("check: "+Lx.concerns.join(", "));
    if(Lx.max_bid) bits.push("unverified heavyweight luxury: only worth bidding up to about $"+Lx.max_bid);
    if(Lx.tier==="low_margin") bits.push("low-margin brand: heavy retail depreciation");
    bits.push("text-only screen, not proof; value counted at "+Math.round(Lx.factor*100)+"% of genuine");
    t.append(el("div","meta",bits.join(" · ")));
    if(Lx.tier!=="verified"&&Lx.why&&Lx.why.length) t.append(el("div","meta "+(Lx.tier==="counterfeit"?"neg":"warn"),"Might not be real: "+Lx.why.join("; ")));}
  if(r.tracked){const cap=r.my_max!=null?r.my_max:r.max_bid, over=r.bid>cap;
    const m=el("div","meta"+(over?" neg":""),(r.my_max!=null?"your max "+money(r.my_max):"break-even max "+money(r.max_bid))+(over?" · PRICED PAST IT, skip":" · room to bid "+money(cap-r.bid))
      +(r.track_note?" · "+r.track_note:"")); t.append(m);}
  if(r.notes&&r.notes.length){const d=el("div","meta",r.notes.join(" · ")); t.append(d);}
  if(r.model){t.append(el("div","meta","Model # "+r.model+(r.notes_model?" (from the description)":"")));}
  if(r.query){const q=encodeURIComponent(r.query), base="https://www.ebay.com/sch/i.html?_nkw="+q, lk=el("div","meta","Compare: ");
    for(const [label,suffix] of [["sold on eBay","&LH_Sold=1&LH_Complete=1"],["listed on eBay","&LH_BIN=1"]]){
      const a=el("a","",label+" ↗"); a.href=base+suffix; a.target="_blank"; a.rel="noopener noreferrer"; lk.append(a," · ");}
    lk.lastChild.remove(); lk.title="Opens an eBay search for: "+r.query; t.append(lk);}
  tr.append(t);
  const go=el("td","");
  if(r.url){const a=el("a","bid",r.platform_label+" ↗"); a.title="Open the listing to bid"; a.href=r.url; a.target="_blank"; a.rel="noopener noreferrer"; go.append(a);} else {go.textContent=r.platform_label;}
  tr.append(go);
  const b=el("td","num",money(r.bid)); if(r.bids!=null) b.append(el("div","meta",r.bids+" bids"));
  const allin=r.bid*(1+r.cost.prem)*(1+TAX)+r.ship_in;
  const ai=el("div","meta","all-in "+money(allin)); ai.title="bid + "+Math.round(r.cost.prem*1000)/10+"% buyer fee + "+Math.round(TAX*1000)/10+"% sales tax + $"+Math.round(r.ship_in)+" shipping in; selling costs another $"+Math.round(r.out_in)+" to ship out ("+(r.ship_set?"your figure":r.ship_kind==="quoted"?"quoted by the site":r.ship_kind==="local"?"Chicago-area pickup, no shipping":r.ship_kind==="ask"?"agency has not listed a price, assumed":r.ship_kind==="pickup"?"pickup/freight not stated, assumed":"assumed")+")"; b.append(ai);
  const sh=el("div","meta"); sh.append((r.ship_set||r.ship_kind==="quoted"?"ship ":r.ship_kind==="local"?"pickup ":r.ship_kind==="pickup"?"pickup? est ":"est ship ")+"$");
  const inp=el("input"); inp.type="number"; inp.min="0"; inp.step="1"; inp.className="ship"; inp.value=String(Math.round(r.ship_in)); inp.title="Type the real shipping/pickup cost; profit updates. Clear it to go back to the assumed figure.";
  inp.onchange=()=>{const v=inp.value.trim(); try{ if(v==="") localStorage.removeItem(shipKey(r)); else localStorage.setItem(shipKey(r),v);}catch(e){} setShip(r,v===""||isNaN(+v)?null:+v); render();};
  sh.append(inp); b.append(sh);
  const so=el("div","meta"); so.append("sell ship $"); const oi=el("input"); oi.type="number"; oi.min="0"; oi.step="1"; oi.className="ship"; oi.value=String(Math.round(r.out_in));
  oi.title="What it costs you to ship the item to its buyer after you sell it (estimate by item type). Type your own; clear it to reset.";
  oi.onchange=()=>{const v=oi.value.trim(); try{ if(v==="") localStorage.removeItem(outKey(r)); else localStorage.setItem(outKey(r),v);}catch(e){} setOut(r,v===""||isNaN(+v)?null:+v); render();};
  so.append(oi); b.append(so); tr.append(b);
  const e=el("td","num left"); e.dataset.end=r.end||""; e.append(el("span","cd",L.t));
  if(r.end){e.title=new Date(r.end).toLocaleString(); e.append(el("div","meta",new Date(r.end).toLocaleString([],{weekday:"short",hour:"numeric",minute:"2-digit"})));}
  tr.append(e);
  tr.append(el("td","num pos",r.noval?"?":money(r.max_bid)));
  const w=el("td","num",r.noval?"no estimate":money(r.value.typ)); if(!r.noval) w.append(el("div","meta",`${money(r.value.low)}-${money(r.value.high)}`)); else w.append(el("div","meta","look up sold prices")); tr.append(w);
  const unc=r.imei_verdict==="unchecked"&&r.profit_if_clean!=null;  // negative here is the 20%-until-checked assumption, not the item
  if(r.noval){tr.append(el("td","num","?"), el("td","num","?")); tr.append(sellsCell(r)); return tr;}
  const pn=el("td","num "+(r.profit_now>=0?"pos":"neg"),money(r.profit_now)); if(unc) pn.append(el("div","meta pos","if IMEI clean: "+money(r.profit_now_clean))); tr.append(pn);
  const pe=el("td","num "+(r.profit_end>=0?"pos":"neg"),money(r.profit_end)); pe.append(el("div","meta","ends ~"+money(r.ends_at)));
  if(unc) pe.append(el("div","meta pos","if IMEI clean: "+money(r.profit_if_clean)));
  if(r.bids===0){const nb=el("div","meta "+(r.profit_now>=0?"pos":"neg"),"if nobody bids: "+money(r.profit_now)+" ("+Math.round(r.roi_now*100)+"%)");
    if(r.p_nobid!=null) nb.title=Math.round(r.p_nobid*100)+"% of comparable ShopGoodwill auctions closed with no bids"; pe.append(nb);}
  tr.append(pe);
  if(r.pricer) pe.append(el("div","meta","pricer: worth "+money(r.pricer.value)+" ("+(r.pricer.comps==null?"?":r.pricer.comps)+" sold comps"+(r.pricer.low!=null?", "+money(r.pricer.low)+"-"+money(r.pricer.high):"")+"), max bid "+money(r.pricer.max25)));
  if(r.luxury&&r.profit_real_end!=null){ pe.append(el("div","meta "+(r.profit_real_end>=0?"pos":"neg"),"if authentic: "+money(r.profit_real_end))); }
  tr.append(sellsCell(r));
  return tr;}
function visible(skip){
  skip=skip||"";
  const q=$("q").value.toLowerCase(), k=skip==="kind"?"":$("kind").value, p=skip==="plat"?"":$("plat").value, d=skip==="dom"?"":$("dom").value, gr=skip==="grade"?"":$("grade").value, tr_=skip==="tier"?"":$("tier").value, spec=$("spec").checked, he=$("hideEnded").checked, mn=$("mine").checked, lx=$("lux").checked, mp=$("minp").checked, sn=$("soon").checked, nb=$("nobid").checked;
  const order={A:1,B:2,C:3,D:4,"?":5};
  return rows.filter(r=>(!sn||r.tracked||!r.end||Date.parse(r.end)-Date.now()<=D.maxDays*864e5)&&(!mp||r.tracked||r.noval||(r.profit_end>=D.minProfit&&r.roi_end>=D.minRoi)||(nb&&r.only_if_unbid)||(lx&&r.luxury&&r.profit_real_end>=D.minProfit))&&(!mn||r.tracked)&&(!$("noest").checked||!r.noval)&&(!lx||r.luxury)&&(!q||(r.title+" "+r.brand+" "+r.kind).toLowerCase().includes(q))&&(!k||r.kind===k)&&(!p||r.platform===p)&&(!d||r.domain===d)&&(!tr_||r.tier===tr_)
    &&(!gr||order[r.liquidity.grade]<=order[gr])&&(!spec||!r.coarse)&&(!he||left(r).s>0));}
function render(){
  const col=COLS.find(c=>c[0]===sortKey); const list=visible();
  const dead=r=>!r.end||left(r).s<=0;  // unknown or already-ended auctions always sink to the bottom, whatever the direction
  if(col&&col[2]) list.sort((a,b)=>{if(sortKey==="end"&&dead(a)!==dead(b)) return dead(a)?1:-1;
    const x=col[2](a),y=col[2](b); return (x<y?-1:x>y?1:0)*(asc?1:-1);});
  const body=$("body"); body.replaceChildren(...list.map(row)); $("count").textContent=`${list.length} of ${rows.length} shown`; labelTable("t");
  if(!list.length){const tr=el("tr"), td=el("td","","Nothing matches these filters. Untick \u201cReturn at least\u201d, \u201cEnds within\u201d or \u201cMy list only\u201d under More filters, or pick All.");
    td.colSpan=COLS.length; td.style.textAlign="left"; tr.append(td); body.append(tr);}
  syncChips();
  document.querySelectorAll("th").forEach(th=>{th.classList.toggle("sorted",th.dataset.k===sortKey); th.classList.toggle("asc",th.dataset.k===sortKey&&asc);});}
if(D.focus&&D.focus.length===1&&[...$("kind").options].some(o=>o.value===D.focus[0])) $("kind").value=D.focus[0];
$("soonv").textContent=String(D.maxDays); $("soon").onchange=render; $("nobid").onchange=render; $("minpv").textContent=String(Math.round(D.minRoi*100)); $("minp").onchange=render;
const taxIn=$("tax"); taxIn.value=String(Math.round(TAX*10000)/100);
taxIn.onchange=()=>{const v=taxIn.value.trim(), n=v===""||isNaN(+v)?D.tax*100:+v; TAX=n/100; try{localStorage.setItem("tax",String(n));}catch(e){} recalcAll(); render();};
const head=$("head");
for(const c of COLS){const th=el("th","",c[1]); th.dataset.k=c[0]; if(c[2]) th.onclick=()=>{asc=(sortKey===c[0])?!asc:(c[0]==="rank"||c[0]==="end"||c[0]==="days"); sortKey=c[0]; render();}; head.append(th);}
for(const v of [...new Set(rows.map(r=>r.platform))]){const o=el("option","",rows.find(r=>r.platform===v).platform_label); o.value=v; $("plat").append(o);}
const kinds={}; rows.forEach(r=>kinds[r.kind]=(kinds[r.kind]||0)+1);
for(const v of Object.keys(kinds).sort((a,b)=>kinds[b]-kinds[a])){const o=el("option","",`${v} (${kinds[v]})`); o.value=v; $("kind").append(o);}
for(const v of [...new Set(rows.map(r=>r.domain))].sort()){const o=el("option","",v); o.value=v; $("dom").append(o);}
["q","kind","plat","dom","grade","tier","spec","hideEnded","mine","lux","noest"].forEach(id=>{$(id).addEventListener("input",render); $(id).addEventListener("change",render);});
["soon","nobid","minp"].forEach(id=>$(id).addEventListener("input",render));
const FIELD={plat:"platform",kind:"kind",dom:"domain",tier:"tier"};
function syncChips(){
  for(const id of ["tier","plat","kind","dom","grade"]){const sel=$(id), pool=visible(id), ord={A:1,B:2,C:3,D:4,"?":5};
    for(const o of sel.options){
      if(!o.dataset.label) o.dataset.label=o.textContent.replace(/ \(\d+\)$/,"");
      const n=!o.value?pool.length:id==="grade"?pool.filter(r=>ord[r.liquidity.grade]<=ord[o.value]).length:pool.filter(r=>r[FIELD[id]]===o.value).length;
      o.textContent=o.dataset.label+" ("+n+")";}}}
function onTier(){const v=$("tier").value; if(v){$("minp").checked=false; $("soon").checked=false; render();}}
$("tier").addEventListener("change",onTier);
syncChips();
$("more").open=window.matchMedia("(min-width:761px)").matches;
$("meta").textContent=`${rows.length} items · built ${new Date(D.generated).toLocaleString()} · ${D.refreshed?"bids, end times and test results re-read from each listing at build time":"bids are as of the last catalog crawl: re-run with --web-refresh for live numbers"}`;
setInterval(()=>{document.querySelectorAll("td.left").forEach(td=>{const e=td.dataset.end; if(!e) return; const s=(Date.parse(e)-Date.now())/1000;
  td.querySelector(".cd").textContent=s<=0?"ended":(()=>{const d=Math.floor(s/86400),h=Math.floor(s%86400/3600),m=Math.floor(s%3600/60); return d?`${d}d ${h}h`:h?`${h}h ${m}m`:`${m}m`;})();});},30000);
function renderWatch(){
  const W=D.watch||[]; $("watchbox").hidden=!W.length; $("gowatch").style.display=W.length?"inline-block":"none"; $("gowatchn").textContent=String(W.length);
  if(!W.length) return;
  const sel=$("wtopic"), srcSel=$("wsrc"), bandSel=$("wband"), val=$("wval");
  if(sel.options.length===1){for(const t of [...new Set(W.map(x=>x.topic))].sort()){const o=el("option","",t); o.value=t; sel.append(o);} sel.addEventListener("input",renderWatch); sel.addEventListener("change",renderWatch);}
  if(srcSel.options.length===1){for(const t of [...new Set(W.map(x=>x.source))].sort()){const o=el("option","",t+" ("+W.filter(x=>x.source===t).length+")"); o.value=t; srcSel.append(o);} srcSel.addEventListener("input",renderWatch); srcSel.addEventListener("change",renderWatch);}
  if(!bandSel.dataset.bound){bandSel.dataset.bound="1"; for(const e of [bandSel,val]) for(const ev of ["input","change"]) e.addEventListener(ev,renderWatch);}
  const list=W.filter(x=>(!sel.value||x.topic===sel.value)&&(!srcSel.value||x.source===srcSel.value)&&(!bandSel.value||x.band===bandSel.value)&&(!val.checked||x.value)); $("wcount").textContent=list.length+" of "+W.length+" lots";
  const body=$("wbody"); body.replaceChildren(...list.map(x=>{
    const tr=el("tr"), t=el("td","item"); const title=el("div","title",x.title); t.append(title);
    if(x.url){const a=el("a","bid","open listing ↗"); a.href=x.url; a.target="_blank"; a.rel="noopener noreferrer"; t.append(a);}
    if(x.quantity&&x.quantity>1) t.append(el("div","meta","quantity "+x.quantity));
    if(x.basis&&x.value) t.append(el("div","meta","value basis: "+x.basis));
    tr.append(t, el("td","",x.topic), el("td","",x.source), el("td","num",money(x.bid)), el("td","num",x.bids==null?"?":String(x.bids)));
    tr.append(el("td","num",x.value?(money(x.value)+(x.low?"  ("+money(x.low)+"-"+money(x.high)+")":"")):(x.band==="far"&&x.value?money(x.value):"")));
    const s=(Date.parse(x.end)-Date.now())/1000, d=Math.floor(s/86400),h=Math.floor(s%86400/3600),m=Math.floor(s%3600/60);
    tr.append(el("td","num",s<=0?"ended":d?`${d}d ${h}h`:h?`${h}h ${m}m`:`${m}m`), el("td","",[x.city,x.state].filter(Boolean).join(", ")+(x.band==="far"?"  (25-100 mi"+(x.value?", worth ~$"+Math.round(x.value):"")+")":x.band==="ships"?"  (ships)":"")));
    return tr;}));
  labelTable("wt");
}
function playCell(r){const e=r.evidence, td=el("td","meta"), id=e.identity||{};
  td.append(el("div","","identity: "+(id.confidence||"?")+" ("+((id.corroborated_by||[]).join("; ")||"no second source")+"); priced as "+(id.price_based_on||"?")));
  const P=e.plays||{}; const as=P["as-is"], rp=P["repair"];
  td.append(el("div","","play: "+(e.play||"none")+" ["+(e.condition?e.condition.class.replace("_"," "):"")+"]"));
  if(as) td.append(el("div","","as-is/parts value "+money(as.value)+" → net "+money(as.net)));
  if(rp) td.append(el("div","","repaired value "+money(rp.value)+" − repair "+money(rp.repair_cost)+" (estimate) → net "+money(rp.net)+", +"+rp.days_added+" days"));
  for(const k of (id.risks||[])) td.append(el("div","neg",k));
  return td;}
function renderExt(){
  const X=D.ext; const box=$("extbox"); box.hidden=!X; $("goext").style.display=box.hidden?"none":"inline-block";
  if(box.hidden) return; $("goextn").textContent=String(X.verified.length);
  $("extnote").textContent=`Each sale's buyer's premium and tax are read from that sale's own terms (premium varies per sale; a lot whose sale states none cannot be costed). Same gate as everywhere else: high identity, spec-matched sold comps, the ${money(X.bar.min_net)} / ${Math.round(X.bar.min_return*100)}% bar. Pickup-only lots beyond ${X.bar.max_pickup_miles} miles fail the Chicago-metro rule. Jewelry with karat and gram weights is valued by melt.`;
  $("extsbody").replaceChildren(...(X.sources||[]).map(s=>{const tr=el("tr"), st=el("td",s.status==="AUTOMATED"?"pos":s.status==="MANUAL-ONLY"?"neg":"",s.status); const d=el("td","meta");
    d.append(el("div","",s.status==="MANUAL-ONLY"?"NOT AUTOMATED: "+s.blocker:(s.how||""))); if(s.status==="AUTOMATED") d.append(el("div","",(s.lots||0)+" lot(s) stored; last fetched "+(s.last_fetched?new Date(s.last_fetched).toLocaleString():"never")));
    tr.append(el("td","",s.label), st, d); return tr;}));
  labelTable("extst");
  const terms=r=>{const t=r.evidence.terms||{}, d=el("div","meta"); d.append(el("div","",r.platform_label+(r.sale_id?" sale "+r.sale_id:""))); d.append(el("div","","premium "+(t.premium==null?"NOT READ":Math.round(t.premium*1000)/10+"%")+" ("+(t.premium_source||"")+"); tax "+Math.round((t.tax||0)*10000)/100+"%"));
    if(r.manual_only) d.append(el("div","neg","MANUAL-ONLY source: this lot was supplied by file, it is not fetched automatically"));
    d.append(el("div","",t.pickup_only?("pickup only"+(t.distance_miles!=null?", "+t.distance_miles+" mi":", distance unknown")):"buyer-arranged shipping")); return d;};
  const item=r=>{const t=el("td","item"); t.append(el("div","title",r.title)); if(r.url){const a=el("a","bid","open listing ↗"); a.href=r.url; a.target="_blank"; a.rel="noopener noreferrer"; t.append(a);} return t;};
  $("extvbody").replaceChildren(...X.verified.map(r=>{const e=r.evidence, tr=el("tr"), tdT=el("td"); tdT.append(terms(r));
    tr.append(item(r), tdT, el("td","num",money(e.price_assumed)+" ("+money(e.all_in)+" all-in)"), el("td","meta",e.value_basis||(e.value_tier?e.value_tier.tier+" of "+e.value_tier.n+" comps":"")), el("td","num pos",money(e.net)+" ("+Math.round((e.return||0)*100)+"%)"), el("td","num",money(e.max_bid))); return tr;}));
  $("extubody").replaceChildren(...X.unverified.map(r=>{const tr=el("tr"), tdT=el("td"), why=el("td"); tdT.append(terms(r)); for(const f of r.failures) why.append(el("div","meta",f.check+": "+f.why));
    tr.append(item(r), tdT, el("td","num",money(r.bid)), el("td","neg",r.failed), why); return tr;}));
  $("extlbody").replaceChildren(...X.leads.map(l=>{const tr=el("tr"), t=el("td","item"); t.append(el("div","title",l.title||l.lead_id)); if(l.url){const a=el("a","bid","open ↗"); a.href=l.url; a.target="_blank"; a.rel="noopener noreferrer"; t.append(a);}
    const ph=el("td"); if(l.photos_url){const a=el("a","bid","photos ↗"); a.href=l.photos_url; a.target="_blank"; a.rel="noopener noreferrer"; ph.append(a);}
    tr.append(t, el("td","",l.label), el("td","",[l.address||l.location,l.distance_miles!=null?l.distance_miles+" mi":""].filter(Boolean).join(" · ")), el("td","",l.dates||""), el("td","",l.company||""), ph, el("td","meta",l.notes||"")); return tr;}));
  labelTable("extvt"); labelTable("extut"); labelTable("extlt");
}
function renderGate(){
  const G=D.gate; const rb=$("repbox"), ub=$("unvbox");
  if(!G){rb.hidden=true; ub.hidden=true; return;}
  const B=G.bar, RP=G.repair, all=RP.verified.concat(RP.unverified);
  rb.hidden=!all.length; $("gorep").style.display=rb.hidden?"none":"inline-block"; $("gorepn").textContent=String(RP.verified.length);
  $("repnote").textContent=`These lots state damage with a known fix. Each shows the as-is (parts) value and the repaired-and-sold value side by side. A repair play must clear net ${money(B.min_net)} + a ${money(B.repair_cushion)} repair cushion and ${Math.round(B.min_return*100)}% of all-in cost (buy + repair), on spec-matched sold comps with high identity. Repair costs are labelled estimates, not quotes. The main table's filters are unchanged.`;
  $("repbody").replaceChildren(...all.map(r=>{const e=r.evidence, tr=el("tr"), P=e.plays||{}, as=P["as-is"], rp=P["repair"], ch=P[e.play||"repair"]||rp||as;
    const t=el("td","item"); t.append(el("div","title",r.title)); if(r.url){const a=el("a","bid","open listing ↗"); a.href=r.url; a.target="_blank"; a.rel="noopener noreferrer"; t.append(a);} t.append(el("div","meta",r.platform_label||""));
    const tdR=el("td","meta"); if(rp){tdR.append(el("div","","repaired value "+money(rp.value))); tdR.append(el("div","","repair "+money(rp.repair_cost)+" (ESTIMATE) +"+rp.days_added+" days"));} else tdR.append(el("div","","no value yet"));
    const tdV=el("td","meta"); if(r.status==="verified") tdV.append(el("div","pos","VERIFIED repair play")); else {tdV.append(el("div","neg",r.failed||"unverified")); for(const f of r.failures) tdV.append(el("div","meta",f.check+": "+f.why));}
    const d=e.demand; tr.append(t, el("td","num",money(r.bid)+(ch?" → "+money(ch.price_assumed)+" ("+money(ch.all_in)+" all-in)":"")), el("td","num",as?money(as.value)+" (net "+money(as.net)+")":"?"), tdR,
      el("td","num "+(ch&&ch.net>=B.min_net?"pos":"neg"),ch?money(ch.net)+" ("+Math.round((ch.return||0)*100)+"%)":"?"), el("td","num",ch?money(ch.max_bid):"?"),
      el("td","meta",d?(d.sold_per_30d+" sold per 30 days; time to sell: "+d.time_to_sell):"no comps"), tdV); return tr;}));
  labelTable("rept");
  const U=G.unverified; ub.hidden=!U.length; $("gounv").style.display=ub.hidden?"none":"inline-block"; $("gounvn").textContent=String(U.length);
  if(ub.hidden) return;
  $("unvnote").textContent=`These rows used to be ranked as profitable on brand-level baselines, hand-set values or too few matching sold sales. They are NOT profitable until the failed check is resolved. The old numbers are shown only for comparison and are labelled unverified claims.`;
  const fs=$("unvfail"); if(!fs.dataset.set){fs.dataset.set="1"; for(const c of [...new Set(U.map(x=>x.failed))].sort()){const o=el("option","",c); o.value=c; fs.append(o);} for(const id of ["unvq","unvfail"]) for(const ev of ["input","change"]) $(id).addEventListener(ev,renderGate);}
  const q=$("unvq").value.toLowerCase(), fl=$("unvfail").value;
  const L=U.filter(x=>(!q||x.title.toLowerCase().includes(q))&&(!fl||x.failed===fl)).sort((a,b)=>((b.claimed.profit_at_expected_close||-1e9)-(a.claimed.profit_at_expected_close||-1e9)));
  $("unvcount").textContent=L.length+" of "+U.length+" shown";
  $("unvbody").replaceChildren(...L.slice(0,400).map(x=>{const tr=el("tr"), t=el("td","item"); t.append(el("div","title",x.title)); if(x.url){const a=el("a","bid","open listing ↗"); a.href=x.url; a.target="_blank"; a.rel="noopener noreferrer"; t.append(a);}
    const c=x.claimed, tdC=el("td","meta"); tdC.append(el("div","neg","unverified claim: worth "+(c.worth_typ!=null?money(c.worth_typ):"?")+(c.profit_at_expected_close!=null?", profit "+money(c.profit_at_expected_close)+" ("+Math.round((c.return_at_expected_close||0)*100)+"%)":"")+(c.max_bid!=null?", max bid "+money(c.max_bid):""))); if(c.basis) tdC.append(el("div","",c.basis));
    const why=el("td"); for(const f of x.failures) why.append(el("div","meta",f.check+": "+f.why));
    tr.append(t, el("td","",x.platform_label||x.platform), el("td","num",money(x.bid)), tdC, el("td","neg",x.failed), why); return tr;}));
  labelTable("unvt");
}
function renderLux(){
  const L=D.lux; const box=$("luxbox"); box.hidden=!L||(!L.verified.length&&!L.unverified.length); $("golux").style.display=box.hidden?"none":"inline-block";
  if(box.hidden) return; const B=L.bar;
  $("goluxn").textContent=String(L.verified.length);
  $("luxnote").textContent=`No designer item is listed on price math alone. It needs supported or plausible authenticity (counterfeit flags or no proof hard-fail it), no value-killing damage (ink on light leather, mould or strong odour, cracked canvas...), at least ${B.min_comps} sold comps in the SAME condition grade as the item (no flat discount on clean comps), brand-tier demand evidence, and an expected net of at least ${money(B.min_net)} and ${Math.round(B.min_return*100)}% after authentication, fees, repair (plus a ${money(B.repair_cushion)} cushion) and the counterfeit risk. Repair costs, authentication prices, channel fees and the genuine-probability are labelled estimates, not quotes.`;
  const item=r=>{const t=el("td","item"); t.append(el("div","title",r.title)); if(r.url){const a=el("a","bid","open listing ↗"); a.href=r.url; a.target="_blank"; a.rel="noopener noreferrer"; t.append(a);} t.append(el("div","meta",r.platform_label||"")); return t;};
  $("luxvbody").replaceChildren(...L.verified.map(r=>{const e=r.evidence, p=e.plays[e.chosen], tr=el("tr"), au=e.authenticity, dm=e.damage;
    const tdA=el("td","meta"); tdA.append(el("div","",au.verdict+" ("+Math.round(e.auth.probability_genuine*100)+"% assumed genuine)")); tdA.append(el("div","",(au.evidence||[]).join(", ")||"no stated proofs")); tdA.append(el("div","",e.auth.third_party_cost?("third-party authentication "+money(e.auth.third_party_cost)+" priced in"):"already authenticated per listing (verify)"));
    const tdD=el("td","meta"); if(!dm.items.length) tdD.append(el("div","","no damage stated")); for(const i of dm.items){tdD.append(el("div","",i.label+": "+money(i.cost_low)+"-"+money(i.cost_high)+" ("+i.source.split(";")[0]+")"));} if(dm.items.length) tdD.append(el("div","","repair "+money(dm.repair_low)+"-"+money(dm.repair_high)+"; "+dm.days+" days"));
    const tdV=el("td","meta"); tdV.append(el("div","","play: "+e.chosen+"; grade "+p.grade+": "+money(p.value)+" ("+p.comps+" comps, "+money(p.low)+"-"+money(p.high)+")")); if(e.clean_median) tdV.append(el("div","","clean (excellent) median "+money(e.clean_median)+"; post-repair discount "+Math.round(e.post_repair_discount*100)+"%")); tdV.append(el("div","","if genuine net "+money(p.net_if_genuine)));
    const tdC=el("td","meta"); tdC.append(el("div","",p.channel+" (fee "+Math.round(p.channel_fee*100)+"%)")); tdC.append(el("div","","brand tier: "+e.brand_tier+"; "+e.demand.recent_sales_90d+" sold in 90 days (need "+e.demand.recent_needed+"); "+e.demand.sold_per_30d+" sold per 30 days; time to sell: "+e.demand.time_to_sell));
    tr.append(item(r), el("td","num",money(p.price_assumed)+" ("+money(p.buy_all_in)+" all-in)"), tdA, tdD, tdV, tdC, el("td","num pos",money(e.net)+" ("+Math.round(e.return*100)+"%)"), el("td","num",money(e.max_price))); return tr;}));
  if(!L.verified.length){const tr=el("tr"); tr.append(Object.assign(el("td","","Nothing has passed every check yet."),{colSpan:8})); $("luxvbody").append(tr);}
  $("luxubody").replaceChildren(...L.unverified.map(r=>{const tr=el("tr"), why=el("td"); for(const f of r.failures) why.append(el("div","meta",f.check+": "+f.why));
    tr.append(item(r), el("td","num",money(r.bid)), el("td","neg",r.failed), why, el("td","meta",r.needs_check||"resolve the failed check")); return tr;}));
  labelTable("luxvt"); labelTable("luxut");
}
function renderFb(){
  const F=D.fb; const box=$("fbbox"); box.hidden=!F||(!F.verified.length&&!F.unverified.length); $("gofb").style.display=box.hidden?"none":"inline-block";
  if(box.hidden) return;
  const B=F.bar;
  $("fbnote").textContent=`A listing is verified only if it has at least ${B.min_comps} matching SOLD comps (${B.min_recent}+ in the last 90 days), a working unit, and nets at least ${money(B.min_net)} and ${Math.round(B.min_return*100)}% on its asking price after every cost. Asking prices are never used as value. Local cash resale is an assumed ${Math.round(B.local_factor*100)}% of the sold median and `+(B.local_counts?"counts":"is shown for information but does not count")+" toward verification.";
  const fsel=$("fbfail"); if(!fsel.dataset.set){fsel.dataset.set="1"; for(const c of [...new Set(F.unverified.map(r=>r.failed))].sort()){const o=el("option","",c); o.value=c; fsel.append(o);}
    for(const id of ["fbq","fbch","fbfail"]) for(const ev of ["input","change"]) $(id).addEventListener(ev,renderFb);}
  const q=$("fbq").value.toLowerCase(), ch=$("fbch").value, fl=$("fbfail").value;
  const okq=r=>!q||(r.title+" "+(r.location||"")).toLowerCase().includes(q);
  const V=F.verified.filter(r=>okq(r)&&(!ch||r.evidence.best_channel===ch)), U=F.unverified.filter(r=>okq(r)&&(!fl||r.failed===fl));
  $("gofbn").textContent=String(F.verified.length); $("fbcount").textContent=`${V.length} verified, ${U.length} unverified shown`;
  const item=r=>{const t=el("td","item"); t.append(el("div","title",r.title)); const a=el("a","bid","open on Facebook ↗"); a.href=r.url; a.target="_blank"; a.rel="noopener noreferrer"; t.append(a);
    t.append(el("div","meta",[r.location,r.distance_miles!=null?r.distance_miles+" mi":"",r.condition_label].filter(Boolean).join(" · "))); return t;};
  $("fbvbody").replaceChildren(...V.map(r=>{const e=r.evidence, d=e.demand||{}, tr=el("tr");
    tr.append(item(r), el("td","num",money(r.buy)), el("td","num",money(e.sold_median)+" ("+money(e.sold_low)+"-"+money(e.sold_high)+")"), el("td","num",String(e.comps)),
      el("td","",`${d.sold_90d} sold in 90 days (~${d.sold_per_month}/mo); about ${d.est_days_to_sell} days to sell (${d.days_basis==="prior"?"category estimate, no active-listing data":d.days_basis})`), el("td","",e.best_channel),
      el("td","num pos",money(e.net)), el("td","num",Math.round(e.return*100)+"%"), el("td","num",money(e.max_buy)), playCell(r)); return tr;}));
  if(!V.length) $("fbvbody").append(Object.assign(el("tr"),{})), $("fbvbody").lastChild.append(Object.assign(el("td","","Nothing has passed every check yet."),{colSpan:10}));
  $("fbubody").replaceChildren(...U.map(r=>{const tr=el("tr"), why=el("td"); for(const f of r.failures) why.append(el("div","meta",f.check+": "+f.why));
    tr.append(item(r), el("td","num",money(r.buy)), el("td","neg",r.failed), why, el("td","meta",r.needs_check||"fix the failed check, then re-run the import")); return tr;}));
  labelTable("fbvt"); labelTable("fbut");
}
function renderPicks(){
  const P=D.picks||[]; $("picksbox").hidden=!P.length; $("gopicks").style.display=P.length?"inline-block":"none";
  if(!P.length) return;
  if(!$("pk100").dataset.bound){$("pk100").dataset.bound="1"; for(const id of ["pk100","pkskip"]) for(const ev of ["input","change"]) $(id).addEventListener(ev,renderPicks);}
  const only=$("pk100").checked, gaps=$("pkskip").checked, priced=P.filter(x=>x.value!=null);
  const list=priced.filter(x=>!only||x.profit>=100).sort((a,b)=>b.profit-a.profit);
  if(gaps) list.push(...P.filter(x=>x.value==null));
  $("gopicksn").textContent=String(priced.filter(x=>x.profit>=100).length); $("pkcount").textContent=list.length+" shown · "+priced.length+" priced, "+(P.length-priced.length)+" with no stored comps";
  $("pkbody").replaceChildren(...list.map(x=>{
    const tr=el("tr"), t=el("td","item"); t.append(el("div","title",x.title));
    if(x.url){const a=el("a","bid","open listing ↗"); a.href=x.url; a.target="_blank"; a.rel="noopener noreferrer"; t.append(a);}
    if(x.value==null){t.append(el("div","meta neg","no stored sold comps: "+(x.basis||"look this model up to price it")));
      tr.append(t, el("td","",x.source), el("td","num",money(x.bid)), el("td","num","?"), el("td","num","?"), el("td","num","?"), el("td","num","?"), el("td","num","0"), el("td","num",x.end?(Date.parse(x.end)-Date.now()<=0?"ended":x.end.slice(5,16).replace("T"," ")):""));
      return tr;}
    t.append(el("div","meta","matched on: "+(x.basis||"")));
    if(x.play){t.append(el("div","meta","condition: "+(x.condition_class||"").replace("_"," ")+"; play: "+x.play));
      if(x.as_is_value!=null) t.append(el("div","meta","as-is/parts value "+money(x.as_is_value)+" → net "+money(x.as_is_net)));
      if(x.repaired_value!=null) t.append(el("div","meta","repaired value "+money(x.repaired_value)+" − repair "+money(x.repair_cost)+" (estimate: "+(x.repair_source||"")+") → net "+money(x.repaired_net)+", +"+x.repair_days+" days"));
      if(x.prob_weighted_value!=null) t.append(el("div","meta","if it works "+money(x.working_value)+"; a 50% chance it works would weight to "+money(x.prob_weighted_value)+" (information only)"));}
    if(x.recent&&x.recent.length){const dt=el("details"); dt.append(el("summary","","recent sold: "+x.recent.map(z=>money(z.total_price)).join(", ")));
      for(const z of x.recent) dt.append(el("div","meta",money(z.total_price)+"  "+(z.date||"")+"  "+z.title)); t.append(dt);}
    const s=(Date.parse(x.end)-Date.now())/1000, d=Math.floor(s/86400),h=Math.floor(s%86400/3600),m=Math.floor(s%3600/60);
    tr.append(t, el("td","",x.source), el("td","num",money(x.bid)), el("td","num",money(x.value)+(x.comp_low!=null?"  ("+money(x.comp_low)+"-"+money(x.comp_high)+")":"")), el("td","num",money(x.max_bid_ceiling)), el("td","num",money(x.max_bid_100)),
      el("td","num "+(x.profit>=100?"pos":"neg"),money(x.profit)), el("td","num",x.comps==null?"?":String(x.comps)), el("td","num",s<=0?"ended":d?`${d}d ${h}h`:h?`${h}h ${m}m`:`${m}m`));
    return tr;}));
  labelTable("pkt");
}
function renderScrap(){
  const A=D.scrap||[]; $("scrapbox").hidden=!A.length; $("goscrap").style.display=A.length?"inline-block":"none";
  if(!A.length) return;
  const OZT=31.1035, MINROI=0.30, MINPROFIT=15, a0=A[0];
  $("goscrapn").textContent=String(A.reduce((n,a)=>n+a.lots.length,0));
  const f=(id,v)=>{const e=$(id); if(!e.dataset.set){e.value=v; e.dataset.set="1"; e.addEventListener("input",renderScrap);} return parseFloat(e.value)||0;};
  const au=f("sp_au",a0.spot.gold), ag=f("sp_ag",a0.spot.silver), pay=f("sp_pay",85)/100, share=f("sp_share",90)/100, ship=f("sp_ship",a0.ship_in), ref=f("sp_ref",12);
  if(!$("sp_pass").dataset.bound){$("sp_pass").dataset.bound="1"; $("sp_pass").addEventListener("input",renderScrap);}
  $("scrapnote").textContent=A.map(a=>`${a.name} (${a.source}, ${a.location||""}; buyer's premium ${Math.round(a.premium*100)}%; bids as of ${a.as_of||"?"}${a.end?"; ends "+new Date(a.end).toLocaleString():""})`).join(" | ")
    +". Spot: "+(a0.spot.live?"live":"stored "+(a0.spot.as_of||""))+". "+(a0.notes||"");
  const out=[];
  for(const a of A) for(const l of a.lots){
    const melt=share*(l.fine_gold*au+l.fine_silver*ag)/OZT, p=l.bullion?0.95:pay, net=melt*p-ref;
    const cost=l.bid*(1+a.premium)+ship, profit=net-cost, roi=cost?profit/cost:0;
    const room=Math.min(net/(1+MINROI), net-MINPROFIT), max=Math.max(0,(room-ship)/(1+a.premium));
    out.push({a,l,melt,net,cost,profit,roi,max,ok:profit>=MINPROFIT&&roi>=MINROI});
  }
  out.sort((x,y)=>y.profit-x.profit);
  const list=out.filter(x=>!$("sp_pass").checked||x.ok); $("sp_count").textContent=`${list.length} of ${out.length} lots; ${out.filter(x=>x.ok).length} clear the rules`;
  $("sbody").replaceChildren(...list.map(x=>{
    const tr=el("tr"); if(x.ok) tr.style.background="rgba(46,160,67,.14)";
    const t=el("td","item"); t.append(el("div","title",x.l.title));
    if(x.l.url||x.a.url){const lk=el("a","bid",x.l.url?"open lot ↗":"open auction ↗"); lk.href=x.l.url||x.a.url; lk.target="_blank"; lk.rel="noopener noreferrer"; t.append(lk);}
    tr.append(el("td","num",String(x.l.lot)), t, el("td","num",money(x.l.bid)), el("td","num",String(x.l.bids)), el("td","num",money(x.melt)),
      el("td","num",money(x.cost)), el("td","num",money(x.net)), el("td","num",(x.profit<0?"-":"")+money(Math.abs(x.profit))),
      el("td","num",Math.round(x.roi*100)+"%"), el("td","num",money(x.max)));
    return tr;}));
  labelTable("st");
}
(function(){const n=rows.filter(r=>r.platform==="txauction").length; if(!n) return; $("gotxn").textContent=String(n); $("gotx").style.display="inline-block";
  $("gotx").addEventListener("click",ev=>{ev.preventDefault(); $("plat").value="txauction"; $("minp").checked=false; $("soon").checked=false; $("noest").checked=false; render(); $("t").scrollIntoView();});})();
render();
renderWatch();
renderExt();
renderGate();
renderLux();
renderFb();
renderPicks();
renderScrap();
</script></body></html>
"""


def _json_for_html(obj) -> str:
    """JSON safe to embed in a <script> block: no '</script>', no HTML comment openers, no line-separator characters."""
    return (json.dumps(obj, default=str).replace("<", "\\u003c").replace(">", "\\u003e").replace("&", "\\u0026")
            .replace("\u2028", "\\u2028").replace("\u2029", "\\u2029"))


_FASHION_KINDS = {"bag", "wallet", "shoes", "clothing", "watch", "jewelry", "accessory"}
_HIGH_END_EXTRA = re.compile(r"\b(?:tiffany|cartier|rolex|omega|david\s+yurman|van\s+cleef|bulgari|bvlgari|tag\s+heuer|breitling|"
                             r"hublot|panerai|iwc|patek|audemars|longines|tudor|chopard|mikimoto|harry\s+winston)\b", re.I)


def fashion_tier(r: dict) -> str:
    """'designer' (luxury / high-end names), 'everyday' (Nike, Coach, Michael Kors, unbranded...) or '' for non-fashion rows."""
    if r.get("kind") not in _FASHION_KINDS and r.get("domain") != "fashion":
        return ""
    text = f"{r.get('title', '')} {r.get('brand', '')}"
    brand, tier = detect_brand_and_tier(text)
    if _HIGH_END_EXTRA.search(text) or (brand and tier != "LOW_RESALE_MARGIN") or r.get("luxury", {}).get("brand_tier") in ("TIER_1_LUXURY", "TIER_2_STREETWEAR"):
        return "designer"
    return "everyday"


def render(rows: list[dict], refreshed: bool = False, now: datetime | None = None, watch: list[dict] | None = None, scrap: list[dict] | None = None, picks: list[dict] | None = None, fb: dict | None = None, lux: dict | None = None, gate: dict | None = None, ext: dict | None = None) -> str:
    slim = []
    by_key = {(x.get("source"), str(x.get("lot"))): x for x in (picks or []) if x.get("value")}
    for r in rows:
        e = r.get("econ") or {}
        r = {k: v for k, v in r.items() if k not in ("econ",)}
        r["cost"] = {k: e.get(k, 0.0) for k in ("prem", "tax", "inbound", "fee", "ship_out", "climb", "order_fee", "returns", "fee_hi", "hi_at")}  # what the page needs to re-cost
        r["carry"] = r["liquidity"]["carry"]
        r["tier"] = fashion_tier(r)
        pk = by_key.get((r.get("platform"), str(r.get("listing_id"))))
        if pk:
            r["pricer"] = {"value": pk["value"], "comps": pk.get("comps"), "low": pk.get("comp_low"), "high": pk.get("comp_high"), "max25": pk["max_bid_ceiling"], "max100": pk["max_bid_100"],
                           "profit": pk["profit"], "basis": pk.get("basis", "")}
        r["bid"], r["max_bid"] = round(r["bid"], 2), round(r["max_bid"], 2)
        slim.append(r)
    payload = {"generated": (now or datetime.now(timezone.utc)).isoformat(), "refreshed": refreshed, "tax": catalog.SALES_TAX, "minProfit": MIN_PROFIT, "minRoi": MIN_ROI, "focus": sorted(FOCUS_KINDS), "maxDays": MAX_DAYS_LEFT, "rows": slim,
               "watch": [{**w_, "url": safe_url(w_.get("url") or "")} for w_ in (watch or [])],
               "scrap": [{**a_, "url": safe_url(a_.get("url") or "")} for a_ in (scrap or [])],
               "picks": [{k_: v_ for k_, v_ in x.items() if k_ != "specs"} | {"url": safe_url(x.get("url") or "")} for x in (picks or [])],
               "ext": ({"bar": ext["bar"], "leads": ext["leads"], "sources": ext.get("sources", []), **{g_: [{**r_, "url": safe_url(r_.get("url") or "")} for r_ in ext[g_]] for g_ in ("verified", "unverified")}} if ext else None),
               "gate": ({"bar": gate["bar"], "unverified": [{**g_, "url": safe_url(g_.get("url") or "")} for g_ in gate["unverified"]],
                        "repair": {k_: [{**g_, "url": safe_url(g_.get("url") or "")} for g_ in gate["repair"][k_]] for k_ in ("verified", "unverified")}} if gate else None),
               "lux": ({"bar": lux["bar"], **{g_: [{**r_, "url": safe_url(r_.get("url") or "")} for r_ in lux[g_]] for g_ in ("verified", "unverified")}} if lux else None),
               "fb": ({"bar": fb["bar"], **{g_: [{**r_, "url": safe_url(r_.get("url") or "")} for r_ in fb[g_]] for g_ in ("verified", "unverified")}} if fb else None)}
    return _TEMPLATE.replace("__DATA__", _json_for_html(payload))


def write_site(path: str | Path, rows: list[dict], refreshed: bool = False, watch: list[dict] | None = None, scrap: list[dict] | None = None, picks: list[dict] | None = None, fb: dict | None = None, lux: dict | None = None, gate: dict | None = None, ext: dict | None = None) -> Path:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(render(rows, refreshed, watch=watch, scrap=scrap, picks=picks, fb=fb, lux=lux, gate=gate, ext=ext), encoding="utf-8")
    return path


def make_server(directory: str | Path, port: int = 8765, host: str = "127.0.0.1") -> http.server.ThreadingHTTPServer:
    """A static file server for the page. Bound to localhost ONLY unless `host` is set (0.0.0.0 = the whole local network)."""
    handler = functools.partial(http.server.SimpleHTTPRequestHandler, directory=str(directory))
    handler.log_message = lambda *a, **k: None  # type: ignore[attr-defined]
    return http.server.ThreadingHTTPServer((host, port), handler)


def lan_address() -> str | None:
    """This machine's address on the local network (what another computer would type), or None if it cannot be found."""
    import socket
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect(("10.255.255.255", 1))  # no packet is sent; this just picks the outgoing interface
            return s.getsockname()[0]
    except OSError:
        return None


def serve(directory: str | Path, port: int = 8765, lan: bool = False) -> None:
    server = make_server(directory, port, "0.0.0.0" if lan else "127.0.0.1")
    shown = server.server_address[1]
    if lan:
        print(f"Serving {directory} on your local network: http://{lan_address() or '<this-computer-address>'}:{shown}  "
              f"(anyone on this network can open it; no password; Ctrl+C to stop)")
    else:
        print(f"Serving {directory} at http://127.0.0.1:{shown}  (this machine only; Ctrl+C to stop)")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
