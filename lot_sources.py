"""Read-side intake for additional auction sources, with the buyer's premium (and tax) read PER SALE, never stored per source.

Sources (priority order from Muse): CWS Marketing / Treasury TEOAF, HiBid, Illinois iBid, CTBids; EstateSales.net and AuctionZip are
DISCOVERY feeds only (they find estate sales; they price nothing).

WHY AN INTAKE FILE AND NOT A CLIENT: I checked each site's robots.txt and front page on 2026-11-11. HiBid and AuctionZip answer a script with a
Cloudflare/anti-bot 403 (HiBid's robots.txt too) and bid.cwsmarketing.com answered 403 to a plain request (its robots.txt allows /auctions/
with a 5 second crawl delay); CTBids redirected. Getting around a block would break the project rule against evading access controls, so Muse
(which can open these sites through Andrew's connected browser) exports JSON and this module loads it. Illinois iBid's robots.txt only blocks
admin paths and its front page loads, so a polite client is possible once its lot-page structure is reviewed; none is built yet.

THE PER-SALE PREMIUM RULE: HiBid, CTBids and Illinois iBid sales set their own buyer's premium (typically 10-18%, 15-20% per CTBids franchise).
A lot whose sale carries no premium cannot be costed and fails with "buyer's premium". CWS/Treasury terms state no premium and no sales tax
(relayed by Muse), so that source has a stated profile; a sale record still overrides it. TXAuction already stores premium per auction.

Nothing is registered, bid on or paid. The CWS $2,500 card authorization is Andrew's decision and is not touched here.
"""
from __future__ import annotations

import csv
import json
import re
import sqlite3
import time
from datetime import datetime, timezone
from pathlib import Path

import catalog
import evidence

MAX_PICKUP_MILES = 60.0       # decisions note: pickup-only lots only in/near the Chicago metro; the owner will not drive further
CLIMB_DEFAULT = 0.40          # GUESS: share of resale value these auctions close at (no measured data for these sources)
MELT_CLIMB = 0.95             # melt-priced lots close near their payout value (measured on TXAuction jewelry/scrap)
REFINER_SHIP = 12.0           # the existing scrap model's refiner shipping assumption
INBOUND_BUYER_ARRANGED = {"bag": 35.0, "jewelry": 25.0, "watch": 25.0, "phone": 15.0, "tablet": 16.0, "laptop": 30.0}  # ESTIMATES: insured parcel / buyer's shipper
INBOUND_DEFAULT = 25.0

PROFILES = {
    "cws_treasury": {"label": "CWS Marketing (Treasury TEOAF)", "priced": True, "premium_required": False, "premium": 0.0, "tax": 0.0, "stated_by": "Treasury terms: no buyer's premium and no sales tax (relayed by Muse)",
                     "ships": True, "note": "buyer-arranged shipping; registration needs a $2,500 card authorization (Andrew's decision: not done here)"},
    "hibid": {"label": "HiBid", "priced": True, "premium_required": True, "note": "local auction houses; premium differs per sale"},
    "illinois_ibid": {"label": "Illinois iBid", "priced": True, "premium_required": True, "note": "state surplus + Treasurer unclaimed-property jewelry; Springfield pickup or buyer's shipper"},
    "ctbids": {"label": "CTBids (Caring Transitions)", "priced": True, "premium_required": True, "note": "estate auctions, $1 starts; premium per franchise"},
    "estatesales": {"label": "EstateSales.net", "priced": False, "note": "discovery feed only"},
    "auctionzip": {"label": "AuctionZip", "priced": False, "note": "discovery feed only"},
}

SCHEMA = """
CREATE TABLE IF NOT EXISTS auction_sales (
    source TEXT NOT NULL, sale_id TEXT NOT NULL, title TEXT, url TEXT, buyer_premium REAL, premium_source TEXT, sales_tax REAL, tax_source TEXT,
    pickup_only INTEGER, pickup_location TEXT, distance_miles REAL, ships INTEGER, shipping_note TEXT, ends TEXT, imported_at INTEGER NOT NULL,
    PRIMARY KEY (source, sale_id));
CREATE TABLE IF NOT EXISTS auction_lots (
    source TEXT NOT NULL, sale_id TEXT NOT NULL, lot_id TEXT NOT NULL, title TEXT NOT NULL, description TEXT, current_bid REAL, bids INTEGER, ends TEXT, url TEXT,
    category TEXT, weight_g REAL, karat INTEGER, imported_at INTEGER NOT NULL, PRIMARY KEY (source, sale_id, lot_id));
CREATE TABLE IF NOT EXISTS discovery_leads (
    source TEXT NOT NULL, lead_id TEXT NOT NULL, title TEXT, url TEXT, location TEXT, distance_miles REAL, dates TEXT, notes TEXT, imported_at INTEGER NOT NULL,
    PRIMARY KEY (source, lead_id));
"""
SALE_ALIASES = {"id": "sale_id", "sale": "sale_id", "auction_id": "sale_id", "name": "title", "sale_title": "title", "premium": "buyer_premium",
                "buyers_premium": "buyer_premium", "buyer_premium_pct": "buyer_premium", "tax": "sales_tax", "sales_tax_pct": "sales_tax", "end": "ends", "end_time": "ends",
                "location": "pickup_location", "distance": "distance_miles"}
LOT_ALIASES = {"id": "lot_id", "lot": "lot_id", "lot_number": "lot_id", "name": "title", "bid": "current_bid", "price": "current_bid", "current_price": "current_bid",
               "end": "ends", "end_time": "ends", "source_url": "url", "weight": "weight_g", "grams": "weight_g", "karats": "karat"}


def ensure_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA)
    conn.commit()


def parse_rate(raw) -> float | None:
    """'15%' -> 0.15; 15 -> 0.15; 0.15 -> 0.15; blank -> None. (Above 1 is read as a percent, up to 1 as a fraction.)"""
    if raw is None or str(raw).strip() == "":
        return None
    s = str(raw).strip()
    m = re.search(r"\d+(?:\.\d+)?", s)
    if not m:
        return None
    v = float(m.group(0))
    return v / 100 if "%" in s or v > 1 else v


def _norm(rec: dict, aliases: dict) -> dict:
    out = {}
    for k, v in rec.items():
        key = re.sub(r"[\s\-]+", "_", str(k).strip().lower())
        out[aliases.get(key, key)] = v
    return out


def _bool(v) -> int | None:
    if v is None or v == "":
        return None
    return 1 if str(v).strip().lower() in ("1", "true", "yes", "y") else 0


def read_file(path: str | Path) -> dict:
    """JSON: {'sales': [{...sale terms..., 'lots': [...]}], 'leads': [...]} or a bare list of sale objects."""
    p = Path(path)
    data = json.loads(p.read_text(encoding="utf-8-sig"))
    if isinstance(data, list):
        data = {"sales": data}
    return data


def import_file(conn: sqlite3.Connection, path: str | Path) -> dict:
    """Load sale terms + lots (+ discovery leads). Returns counts and rejected rows with reasons. The premium is stored on the SALE row."""
    ensure_schema(conn)
    now = int(time.time())
    res = {"sales": 0, "lots": 0, "leads": 0, "rejected": []}
    data = read_file(path)
    for si, raw in enumerate(data.get("sales", []), 1):
        sale = _norm({k: v for k, v in raw.items() if k != "lots"}, SALE_ALIASES)
        src, sid = str(sale.get("source") or "").strip().lower(), str(sale.get("sale_id") or "").strip()
        if src not in PROFILES or not PROFILES[src]["priced"] or not sid:
            res["rejected"].append((f"sale {si}", f"unknown or discovery-only source '{src}', or no sale_id"))
            continue
        prem = parse_rate(sale.get("buyer_premium"))
        tax = parse_rate(sale.get("sales_tax"))
        conn.execute("INSERT OR REPLACE INTO auction_sales VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                     (src, sid, sale.get("title"), sale.get("url"), prem, sale.get("premium_source"), tax, sale.get("tax_source"), _bool(sale.get("pickup_only")),
                      sale.get("pickup_location"), parse_rate_num(sale.get("distance_miles")), _bool(sale.get("ships")), sale.get("shipping_note"), sale.get("ends"), now))
        res["sales"] += 1
        for li, lraw in enumerate(raw.get("lots", []), 1):
            lot = _norm(lraw, LOT_ALIASES)
            if not str(lot.get("lot_id") or "").strip() or not str(lot.get("title") or "").strip():
                res["rejected"].append((f"sale {sid} lot {li}", "missing lot_id or title"))
                continue
            conn.execute("INSERT OR REPLACE INTO auction_lots VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                         (src, sid, str(lot["lot_id"]).strip(), str(lot["title"]).strip(), str(lot.get("description") or "").strip(), parse_rate_num(lot.get("current_bid")) or 0.0,
                          int(parse_rate_num(lot.get("bids")) or 0), lot.get("ends") or sale.get("ends"), lot.get("url"), lot.get("category"),
                          parse_rate_num(lot.get("weight_g")), int(parse_rate_num(lot.get("karat"))) if parse_rate_num(lot.get("karat")) else None, now))
            res["lots"] += 1
    for li, raw in enumerate(data.get("leads", []), 1):
        lead = _norm(raw, {"id": "lead_id", "name": "title", "link": "url"})
        src = str(lead.get("source") or "").strip().lower()
        if src not in PROFILES or str(lead.get("lead_id") or "").strip() == "":
            res["rejected"].append((f"lead {li}", "unknown source or no lead_id"))
            continue
        conn.execute("INSERT OR REPLACE INTO discovery_leads VALUES (?,?,?,?,?,?,?,?,?)",
                     (src, str(lead["lead_id"]), lead.get("title"), lead.get("url"), lead.get("location"), parse_rate_num(lead.get("distance_miles")), lead.get("dates"), lead.get("notes"), now))
        res["leads"] += 1
    conn.commit()
    return res


def parse_rate_num(raw) -> float | None:
    if raw is None or str(raw).strip() == "":
        return None
    m = re.search(r"-?\d+(?:\.\d+)?", str(raw).replace(",", ""))
    return float(m.group(0)) if m else None


# ---------------------------------------------------------------- terms -> cost profile
def sale_terms(profile: dict, sale: sqlite3.Row) -> dict:
    """The premium, tax, pickup and shipping terms for ONE sale. The premium comes from the sale; only a source whose terms STATE it (CWS) has a default."""
    prem, prem_src = sale["buyer_premium"], sale["premium_source"] or "read from this sale's terms"
    if prem is None and not profile.get("premium_required") and profile.get("premium") is not None:
        prem, prem_src = profile["premium"], profile["stated_by"]
    tax, tax_src = sale["sales_tax"], sale["tax_source"] or "read from this sale's terms"
    if tax is None:
        if profile.get("tax") is not None:
            tax, tax_src = profile["tax"], profile["stated_by"]
        else:
            tax, tax_src = catalog.SALES_TAX, "ASSUMPTION: the configured sales tax (the sale's terms did not state one)"
    pickup_only = bool(sale["pickup_only"]) if sale["pickup_only"] is not None else (not profile.get("ships", False))
    return {"premium": prem, "premium_source": prem_src, "tax": tax, "tax_source": tax_src, "pickup_only": pickup_only and not sale["ships"],
            "ships": bool(sale["ships"]) or bool(profile.get("ships")), "distance_miles": sale["distance_miles"], "pickup_location": sale["pickup_location"]}


def lot_rows(conn: sqlite3.Connection, now: datetime) -> list[tuple[sqlite3.Row, sqlite3.Row]]:
    ensure_schema(conn)
    conn.row_factory = sqlite3.Row
    out = []
    for lot in conn.execute("SELECT * FROM auction_lots"):
        sale = conn.execute("SELECT * FROM auction_sales WHERE source=? AND sale_id=?", (lot["source"], lot["sale_id"])).fetchone()
        end = lot["ends"] or (sale["ends"] if sale else None)
        try:
            if end and datetime.fromisoformat(end.replace("Z", "+00:00")) <= now:
                continue
        except ValueError:
            pass
        if sale:
            out.append((lot, sale))
    return out


# ---------------------------------------------------------------- the gate for these lots
def assess_lot(conn: sqlite3.Connection, lot: sqlite3.Row, sale: sqlite3.Row, now: datetime | None = None) -> dict:
    import luxury_goods
    import page_gate
    from engine.melt import STONE_RE
    now = now or datetime.now(timezone.utc)
    profile = PROFILES[lot["source"]]
    t = sale_terms(profile, sale)
    title, desc = lot["title"], lot["description"] or ""
    res = {"platform": lot["source"], "platform_label": profile["label"], "listing_id": f"{lot['sale_id']}-{lot['lot_id']}", "url": lot["url"] or sale["url"], "title": title,
           "end": lot["ends"] or sale["ends"], "bid": lot["current_bid"] or 0.0, "failures": [], "evidence": {"terms": t}, "status": "unverified", "failed": None, "needs_check": None,
           "claimed": None, "source": lot["source"], "sale_id": lot["sale_id"], "pickup_only": t["pickup_only"], "distance_miles": t["distance_miles"]}
    fails = res["failures"]
    if t["premium"] is None:
        fails.append(("buyer's premium", f"not read from this sale's terms (the premium varies per sale: typically 10-18%); the lot cannot be costed. "
                      f"Re-export sale {lot['sale_id']} with its buyer's premium and the terms text it came from"))
        res["needs_check"] = "add buyer_premium and premium_source for this sale"
        return _finish(res)
    if t["pickup_only"]:
        d = t["distance_miles"]
        if d is None:
            fails.append(("pickup distance", "pickup only and the distance is unknown: the Chicago-metro pickup rule cannot be checked"))
        elif d > MAX_PICKUP_MILES:
            fails.append(("pickup distance", f"pickup only, {d:,.0f} miles away (limit {MAX_PICKUP_MILES:,.0f}): outside the Chicago-metro rule unless a buyer's shipper is available"))
    kind = _kind(title, desc)
    inbound = 0.0 if t["pickup_only"] else INBOUND_BUYER_ARRANGED.get(kind, INBOUND_DEFAULT)  # pickup: travel is not modelled
    res["evidence"]["inbound_basis"] = "pickup (travel not modelled)" if t["pickup_only"] else "ESTIMATE: buyer-arranged shipping"

    melt = _melt_inputs(lot, title, desc)
    if melt:
        return _assess_melt(res, lot, t, melt, inbound, STONE_RE)
    brand_item = luxury_goods.authenticity(title, desc)["brand"] is not None and kind in ("bag", "clothing")
    if brand_item:
        g = luxury_goods.assess(conn, {"title": title, "description": desc, "platform": lot["source"], "platform_label": profile["label"], "listing_id": res["listing_id"],
                                      "url": res["url"], "end": res["end"], "bid": res["bid"], "climb": CLIMB_DEFAULT, "prem": t["premium"], "tax": t["tax"], "inbound": inbound}, now)
    else:
        econ = {"prem": t["premium"], "tax": t["tax"], "inbound": inbound, "returns": catalog.RETURN_RESERVE, "climb": CLIMB_DEFAULT,
                "ship_out": __import__("webpage").OUTBOUND_BY_KIND.get(kind, 15.0), **catalog.ebay_fee_terms({"kind": kind, "title": title})}
        g = page_gate.assess_row(conn, {"title": title, "platform": lot["source"], "platform_label": profile["label"], "listing_id": res["listing_id"], "url": res["url"], "end": res["end"],
                                        "bid": res["bid"], "kind": kind, "econ": econ, "factor": 1.0, "fix": 0.0, "description": desc}, now)
    res["failures"] = [{"check": c, "why": w} for c, w in fails] + g["failures"]
    res["evidence"] = {**g["evidence"], "terms": t, "inbound_basis": res["evidence"]["inbound_basis"]}
    res["needs_check"] = g.get("needs_check")
    if res["failures"]:
        res["failed"] = res["failures"][0]["check"]
    else:
        res["status"] = "verified"
    return res


def _kind(title: str, desc: str) -> str:
    import luxury_goods
    text = f"{title} {desc}"
    if re.search(r"\b(ring|necklace|bracelet|earrings?|pendant|chain|brooch|gold|sterling)\b", text, re.I) and not luxury_goods.BAG_WORDS.search(text):
        return "jewelry"
    if luxury_goods.BAG_WORDS.search(title):
        return "bag"
    k = catalog.style_of(title, "electronics")[1]
    return k if k != "other" else ("clothing" if re.search(r"\b(jacket|coat|dress|suit|sweater|blazer)\b", text, re.I) else k)


def _melt_inputs(lot: sqlite3.Row, title: str, desc: str) -> dict | None:
    """Jewelry lots that state karat and gram weight (fields or text) feed the melt model."""
    import mcp_auction_pricer as mp
    if _kind(title, desc) != "jewelry":
        return None
    kw = {}
    if lot["weight_g"]:
        kw["weight_grams"] = float(lot["weight_g"])
    if lot["karat"]:
        kw["purity_karat"] = int(lot["karat"])
        kw["metal"] = "gold"
    r = mp.evaluate_luxury_watch_jewelry(title, desc, **kw)
    return r if r.get("value") else {"value": None, "reason": r.get("reason")}


def _assess_melt(res: dict, lot, t: dict, melt: dict, inbound: float, stone_re) -> dict:
    fails = res["failures"]
    ev = res["evidence"]
    ev["melt"] = melt
    if not melt.get("value"):
        fails.append(("melt value", melt.get("reason") or "weight or purity not stated"))
        return _finish(res)
    value = melt["value"]
    price = max(res["bid"], MELT_CLIMB * value)
    all_in = price * (1 + t["premium"]) * (1 + t["tax"]) + inbound
    net = value - REFINER_SHIP - all_in
    ret = net / all_in if all_in else None
    ev.update({"value": round(value, 2), "price_assumed": round(price, 2), "all_in": round(all_in, 2), "net": round(net, 2), "return": round(ret, 3) if ret is not None else None,
               "value_basis": f"{melt['metal']} {melt['purity_pct']}% x {melt['weight_grams']} g at ${melt['spot_price_gram']}/g ({melt['spot_source']}), "
                              f"{int(melt['dealer_payout'] * 100)}% dealer payout, ${REFINER_SHIP:.0f} refiner shipping", "play": "melt"})
    a_max = max(0.0, min(value - REFINER_SHIP - evidence.MIN_NET, (value - REFINER_SHIP) / (1 + evidence.MIN_RETURN)))
    ev["max_bid"] = round(max(0.0, (a_max - inbound) / ((1 + t["premium"]) * (1 + t["tax"]))), 2)
    if melt.get("stones_flag"):
        fails.append(("stones in the weight", "the stated weight includes stones, so melt value is only an UPPER bound; weigh the metal alone before relying on it"))
    if net < evidence.MIN_NET:
        fails.append(("profit bar", f"net ${net:,.0f} at the expected close is under the ${evidence.MIN_NET:,.0f} minimum"))
    if (ret or 0) < evidence.MIN_RETURN:
        fails.append(("profit bar", f"return {(ret or 0):.0%} is under {evidence.MIN_RETURN:.0%}"))
    return _finish(res)


def _finish(res: dict) -> dict:
    fails = res["failures"]
    if fails and isinstance(fails[0], tuple):
        res["failures"] = [{"check": c, "why": w} for c, w in fails]
    if res["failures"]:
        res["failed"] = res["failures"][0]["check"]
    else:
        res["status"] = "verified"
    return res


def leads(conn: sqlite3.Connection) -> list[dict]:
    ensure_schema(conn)
    conn.row_factory = sqlite3.Row
    return [dict(r) | {"label": PROFILES.get(r["source"], {}).get("label", r["source"]), "priced": False} for r in conn.execute("SELECT * FROM discovery_leads ORDER BY dates, title")]


def page_payload(conn: sqlite3.Connection, now: datetime | None = None) -> dict:
    now = now or datetime.now(timezone.utc)
    results = [assess_lot(conn, lot, sale, now) for lot, sale in lot_rows(conn, now)]
    return {"verified": sorted([r for r in results if r["status"] == "verified"], key=lambda r: -r["evidence"].get("net", 0)),
            "unverified": [r for r in results if r["status"] != "verified"], "leads": leads(conn),
            "bar": {"min_net": evidence.MIN_NET, "min_return": evidence.MIN_RETURN, "max_pickup_miles": MAX_PICKUP_MILES}}
