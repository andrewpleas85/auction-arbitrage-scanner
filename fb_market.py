"""Facebook Marketplace intake, local-pickup cost model and the resale-confidence gate.

The app cannot scan Marketplace. Muse exports listings to a JSON or CSV file; `import_file` loads them into the parallel
`fb_listings` table (never the auction `catalog`, whose code assumes end times, bids and price climb).

A listing is VERIFIED profitable only when every check in `assess` passes; otherwise it is UNVERIFIED and the row says which
check failed first. Nothing here spends paid quota: value comes only from sold sales already stored in `sold_sales`.

  1. model identified      the title names a brand and model (engine.title_cleaner.model_identity)
  2. single item           a lot, bundle or set is not one model, so one sold price cannot value it
  3. sold comps            >= MIN_COMPS stored SOLD sales whose titles agree on the model words and on chip/RAM/storage/screen
                           where those are stated. Asking prices are never evidence.
  4. demand                >= MIN_RECENT_SALES sold in the last 90 days among those comps, plus an estimated time to sell
  5. identity             the title's claimed model is corroborated by the description/spec fields and nothing contradicts it
                           (identity.py); a contradiction, a mixed bundle or an unverifiable model goes to the unverified list
  6. condition / repair    working units price at comp value; cosmetic at a haircut; repairable (stated defect, known fix) must clear the
                           bar PLUS a $25 cushion after the repair estimate; untested/parts are priced at PARTS value, never as working
  7. channel + costs       best channel's real costs (eBay shipped: fee + returns reserve + order fee + shipping out;
                           local cash: none, at a discounted price that is an ASSUMPTION)
  8. profit bar            net >= MIN_NET dollars AND return on all-in cost >= MIN_RETURN

Cost model: the asking price is the buy price. No auction climb, no buyer's premium, no sales tax, no shipping in.
"""
from __future__ import annotations

import csv
import json
import re
import sqlite3
import statistics
import time
from datetime import datetime, timezone
from pathlib import Path

import catalog
import evidence
import identity as ident_mod
import repair
import sell_channels
from engine import knowledge as kb
from engine.title_cleaner import model_identity

SOURCE = "facebook_marketplace"
MIN_COMPS = evidence.VERIFY_MIN_COMPS  # stored sold sales that agree on model and specs (evidence.py has the valuation tiers)
MIN_RECENT_SALES = 3     # of those, sold within the last 90 days
MIN_NET = evidence.MIN_NET        # ruling: $100 net after every cost
MIN_RETURN = evidence.MIN_RETURN  # ruling: 40% of all-in cost
LOCAL_FACTOR = 0.85      # ASSUMPTION: local cash price as a share of the eBay sold median (no local sold data exists)
LOCAL_CHANNEL_COUNTS = False  # until local sold data exists, local cash is shown for information but cannot make a row verified
# ASSUMPTION: share of a comp's price a Marketplace condition label should fetch (comps are mostly "used, good")
CONDITION_FACTOR = {"new": 1.0, "like new": 1.0, "good": 0.95, "fair": 0.75}
_MULTI = re.compile(r"\b(lot|bundle|set of|collection|assorted|bulk|\d+\s*(?:pcs|pieces|items)|and more)\b|\s\+\s|\bwith (?:a|an|the)\b.{0,25}\b(?:receiver|amp|speaker|changer|player)\b", re.I)
HEAVY_WORDS = re.compile(r"\b(receiver|amplifier|amp|subwoofer|speakers?|turntable|tv|television|monitor|projector|stereo|cd changer)\b", re.I)
HEAVY_SHIP_OUT = 40.0    # ASSUMPTION: insured ground shipping of a 20+ lb component

SCHEMA = """
CREATE TABLE IF NOT EXISTS fb_listings (
    listing_id TEXT PRIMARY KEY, title TEXT NOT NULL, description TEXT, asking_price REAL NOT NULL, condition TEXT,
    location TEXT, distance_miles REAL, seller_id TEXT, product_url TEXT NOT NULL, creation_date TEXT,
    first_seen INTEGER NOT NULL, last_seen INTEGER NOT NULL, model_number TEXT, specs TEXT
)"""

REQUIRED = ("listing_id", "title", "asking_price", "product_url")
FIELDS = ("listing_id", "title", "description", "asking_price", "condition", "location", "distance_miles", "seller_id",
          "product_url", "creation_date", "model_number", "specs")
# header names Muse might use for the same thing; matched case-insensitively
ALIASES = {"id": "listing_id", "item_id": "listing_id", "name": "title", "price": "asking_price", "asking": "asking_price",
           "url": "product_url", "link": "product_url", "distance": "distance_miles", "created": "creation_date",
           "created_at": "creation_date", "date": "creation_date", "seller": "seller_id", "desc": "description", "model": "model_number",
           "model_no": "model_number", "spec": "specs", "specifications": "specs", "source_url": "product_url", "item_url": "product_url"}


def ensure_schema(conn: sqlite3.Connection) -> None:
    conn.execute(SCHEMA)
    have = {r[1] for r in conn.execute("PRAGMA table_info(fb_listings)")}
    for col in ("model_number", "specs"):  # added later: databases created before the identity check get the columns in place
        if col not in have:
            conn.execute(f"ALTER TABLE fb_listings ADD COLUMN {col} TEXT")
    conn.commit()


# ---------------------------------------------------------------- intake
def parse_price(raw) -> float | None:
    """'$1,250', '1250', 1250.0 -> 1250.0. 'Free' is 0.0. Anything unreadable is None."""
    if raw is None:
        return None
    if isinstance(raw, (int, float)):
        return float(raw)
    s = str(raw).strip().lower()
    if s in ("free", "$0"):
        return 0.0
    m = re.search(r"\d[\d,]*(?:\.\d+)?", s)
    return float(m.group(0).replace(",", "")) if m else None


def _normalise(rec: dict) -> dict:
    out: dict = {}
    for k, v in rec.items():
        key = re.sub(r"[\s\-]+", "_", str(k).strip().lower())
        out[ALIASES.get(key, key)] = v
    return out


def read_records(path: str | Path) -> list[dict]:
    p = Path(path)
    text = p.read_text(encoding="utf-8-sig")
    if p.suffix.lower() == ".csv":
        return list(csv.DictReader(text.splitlines()))
    data = json.loads(text)
    if isinstance(data, dict):  # accept {"listings": [...]} as well as a bare array
        data = data.get("listings") or data.get("items") or []
    return data


def import_file(conn: sqlite3.Connection, path: str | Path) -> dict:
    """Load a JSON/CSV export. Upserts by listing_id (re-importing refreshes price/description, keeps first_seen).
    Returns {'imported', 'updated', 'rejected': [(row number, reason)]}."""
    ensure_schema(conn)
    now = int(time.time())
    imported = updated = 0
    rejected: list[tuple[int, str]] = []
    for i, raw in enumerate(read_records(path), 1):
        rec = _normalise(raw)
        price = parse_price(rec.get("asking_price"))
        missing = [f for f in REQUIRED if f != "asking_price" and not str(rec.get(f) or "").strip()]
        if price is None:
            missing.append("asking_price")
        if missing:
            rejected.append((i, "missing or unreadable: " + ", ".join(missing)))
            continue
        url = str(rec["product_url"]).strip()
        if not url.startswith("https://www.facebook.com/"):
            rejected.append((i, "product_url is not a facebook.com link"))
            continue
        dist = parse_price(rec.get("distance_miles"))
        lid = str(rec["listing_id"]).strip()
        exists = conn.execute("SELECT 1 FROM fb_listings WHERE listing_id=?", (lid,)).fetchone()
        conn.execute(
            "INSERT INTO fb_listings (listing_id,title,description,asking_price,condition,location,distance_miles,seller_id,product_url,"
            "creation_date,first_seen,last_seen,model_number,specs) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?) "
            "ON CONFLICT(listing_id) DO UPDATE SET title=excluded.title, description=excluded.description, asking_price=excluded.asking_price,"
            " condition=excluded.condition, location=excluded.location, distance_miles=excluded.distance_miles, seller_id=excluded.seller_id,"
            " product_url=excluded.product_url, creation_date=excluded.creation_date, last_seen=excluded.last_seen,"
            " model_number=excluded.model_number, specs=excluded.specs",
            (lid, str(rec["title"]).strip(), str(rec.get("description") or "").strip(), price, str(rec.get("condition") or "").strip(),
             str(rec.get("location") or "").strip(), dist, str(rec.get("seller_id") or "").strip(), url,
             str(rec.get("creation_date") or "").strip(), now, now, str(rec.get("model_number") or "").strip(),
             json.dumps(rec["specs"]) if isinstance(rec.get("specs"), (dict, list)) else str(rec.get("specs") or "").strip()))
        updated += bool(exists)
        imported += not exists
    conn.commit()
    return {"imported": imported, "updated": updated, "rejected": rejected}


# ---------------------------------------------------------------- comps
def _comps(conn: sqlite3.Connection, title: str, ident: str) -> list[tuple[str, float, str]]:
    """Stored SOLD sales whose titles contain every model word of `ident` and agree on chip/RAM/storage/screen with the listing."""
    import mcp_auction_pricer as mp  # reuse its stored-sales matcher, junk filters and spec-conflict rule
    specs = mp.parse_tech_specs(title)
    claimed = ident_mod.claims(title)

    def other_variant(comp_title: str) -> bool:  # the comp names a different lens mount / focal length / RAM than the listing claims
        c = ident_mod.claims(comp_title)
        return any(claimed[k] is not None and c[k] is not None and claimed[k] != c[k] for k in ("mount", "focal_mm", "ram_gb"))

    sales = [s for s in mp._stored_sales(conn, ident) if not mp._spec_conflict(s[0], specs) and not other_variant(s[0])]
    return [s for s in sales if not (mp._COMP_JUNK.search(s[0]) or mp._ACCESSORY.search(s[0]) or mp._DAMAGED.search(s[0]))]


def _days_ago(raw: str, now: datetime) -> int | None:
    try:
        d = datetime.fromisoformat(raw)
    except (TypeError, ValueError):
        return None
    return (now - (d if d.tzinfo else d.replace(tzinfo=timezone.utc))).days


# ---------------------------------------------------------------- cost model
def ship_out_estimate(kind: str, title: str) -> float:
    from webpage import OUTBOUND_BY_KIND  # the same per-type table the auction model uses
    base = OUTBOUND_BY_KIND.get(kind, 15.0) * (2 if kind in catalog.HEAVY else 1)
    return max(base, HEAVY_SHIP_OUT) if HEAVY_WORDS.search(title) else base


def channels(value: float, kind: str, title: str, buy: float, net_bar: float | None = None) -> dict:
    """Net profit per resale channel for a Marketplace purchase. Buy cost = asking price only (local pickup, no tax or premium)."""
    fee_terms = catalog.ebay_fee_terms({"kind": kind, "title": title})
    fee, order = catalog.fees_for({**fee_terms}, value)
    ship = ship_out_estimate(kind, title)
    ebay_rev = value * (1 - fee - catalog.RETURN_RESERVE) - order - ship
    local_value = value * LOCAL_FACTOR
    out = {}
    for name, rev, sale, costs in (("eBay shipped", ebay_rev, value, {"fee_pct": fee, "returns_pct": catalog.RETURN_RESERVE, "order_fee": order, "ship_out": ship}),
                                   ("local cash", local_value, local_value, {"fee_pct": 0.0, "returns_pct": 0.0, "order_fee": 0.0, "ship_out": 0.0})):
        net = rev - buy
        out[name] = {"sale_price": round(sale, 2), "revenue": round(rev, 2), "net": round(net, 2),
                     "return": round(net / buy, 3) if buy else None, "costs": costs,
                     "max_buy": round(max_buy(rev, net_bar), 2), "counts": name == "eBay shipped" or LOCAL_CHANNEL_COUNTS}
    for name, o in sell_channels.options(kind, title, value, ship).items():  # Swappa, Depop, Mercari, Chrono24, Grailed, StockX, Sweetwater, eBay Live, OfferUp
        net = o["revenue"] - buy
        out[name] = {"sale_price": o["sale_price"], "revenue": o["revenue"], "net": round(net, 2), "return": round(net / buy, 3) if buy else None,
                     "costs": {"fee": o["fee"], "ship_mode": o["ship_mode"], "price_basis": o["price_basis"]}, "max_buy": round(max_buy(o["revenue"], net_bar), 2),
                     "counts": o["counts"], "note": o["note"]}
    return out


def max_buy(revenue: float, net_bar: float | None = None) -> float:
    """Highest asking price that still clears BOTH bars: net >= the net bar and return >= MIN_RETURN (all-in cost = the price)."""
    return max(0.0, min(revenue - (net_bar or MIN_NET), revenue / (1 + MIN_RETURN)))


# ---------------------------------------------------------------- the gate
def _label_factor(label: str) -> float:
    lab = (label or "").lower()
    for key in ("like new", "new", "good", "fair"):
        if key in lab:
            return CONDITION_FACTOR[key]
    return 0.9  # no label: a mild haircut


def _best_channel(ch: dict) -> str:
    usable = [n for n, c in ch.items() if c.get("counts")]
    return max(usable, key=lambda n: ch[n]["net"])


def assess(conn: sqlite3.Connection, row, now: datetime | None = None) -> dict:
    """Run every check. Returns {'status': 'verified'|'unverified', 'failed': first failed check or None, 'failures': [...], 'evidence': {...}}."""
    now = now or datetime.now(timezone.utc)
    title, desc, buy = row["title"], row["description"] or "", float(row["asking_price"])
    brand, kind, _style = catalog.style_of(title, "electronics")
    res: dict = {"listing_id": row["listing_id"], "title": title, "url": row["product_url"], "buy": buy, "location": row["location"],
                 "distance_miles": row["distance_miles"], "condition_label": row["condition"], "kind": kind, "brand": brand,
                 "failures": [], "evidence": {}, "status": "unverified", "failed": None, "needs_check": None}
    fails: list[tuple[str, str]] = res["failures"]
    ev = res["evidence"]

    ident = model_identity(title)
    ev["model"] = ident
    if buy <= 0:
        fails.append(("asking price", "no asking price (offers only): there is no price to evaluate; the max buy price below is what an offer should not exceed"))
    if _MULTI.search(title) or len(re.findall(r";", desc)) >= 2:  # a title that says lot/bundle, or a description listing several separate items
        fails.append(("multi-item lot", "a lot/bundle is several items; price the pieces separately before it can be verified"))
        res["needs_check"] = "ask Muse to list each piece's brand and model as separate rows, then re-import"
        return _finish(res)
    if not ident:
        fails.append(("model not identified", "the title names no brand/model we can match to sold sales"))
        return _finish(res)

    # identity: is it what the listing claims? (title vs description vs spec fields)
    idr = ident_mod.check(title, desc, row["model_number"] if "model_number" in row.keys() else "", row["specs"] if "specs" in row.keys() else None)
    ev["identity"] = {"confidence": idr["confidence"], "claimed": idr["claimed"], "contradictions": idr["contradictions"], "risks": idr["risks"],
                      "corroborated_by": idr["corroborated_by"], "price_based_on": ident}
    if not idr["verified"]:
        fails.append(("identity", idr["reason"]))
        res["needs_check"] = idr["next_step"]

    # condition class (listing text decides; the Marketplace label only scales a working unit)
    cls = repair.classify(title, desc, kind)
    lf = _label_factor(row["condition"])
    ev["condition"] = {"class": cls["class"], "reason": cls["reason"], "label_factor": lf, "defects": cls["defects"]}

    comps = _comps(conn, title, ident)
    ev["comps"] = len(comps)
    if len(comps) < MIN_COMPS:
        fails.append(("sold comps", f"{len(comps)} matching sold sale(s) stored for '{ident}', need {MIN_COMPS}"))
        res["needs_check"] = res["needs_check"] or f"look up eBay sold prices for: {ident}"
    value = None
    tier = evidence.value_tier([p for _, p, _ in comps])
    ev["value_tier"] = {k: tier[k] for k in ("n", "tier", "value", "median", "p25", "iqr_ratio", "wide", "net_bar", "low", "high")}
    net_bar = tier["net_bar"]
    if comps:
        value = tier["value"]  # median (n>=10), 25th percentile (n=5-9 or wide spread) or 25th percentile with a higher bar (n=3-4); None below 3
        ev["sold_median"], ev["sold_low"], ev["sold_high"] = tier["median"], tier["low"], tier["high"]
        if tier["wide"]:
            ev["flags"] = ["wide spread: comps vary by more than half their median, so the 25th percentile is used"]
        dm = evidence.demand(comps, now, evidence.active_count(conn, ident))
        ev["demand"] = dm
        if dm["sold_90d"] < MIN_RECENT_SALES:
            fails.append(("demand", f"only {dm['sold_90d']} of the matching sales are from the last 90 days, need {MIN_RECENT_SALES}"))
    else:
        ev["demand"] = None
        fails.append(("demand", "no sold sales to show demand"))
    if comps and value is None:
        fails.append(("valuation", f"only {len(comps)} comp(s): below 3 gives no value"))

    if value:
        v_work = value * lf
        pv = repair.paths(v_work, cls)
        ev["working_value"] = round(v_work, 2)
        ev["as_is_value"] = round(pv["as_is_value"], 2)
        ev["repaired_value"] = round(pv["repaired_value"], 2) if pv["repaired_value"] else None
        ev["value_basis"] = pv["basis"]
        if pv["prob_weighted"] is not None:
            ev["prob_weighted_value"] = round(pv["prob_weighted"], 2)
        plays: dict = {}
        ch = channels(pv["as_is_value"], kind, title, buy, net_bar)
        b = _best_channel(ch)
        plays["as-is"] = {"channel": b, "value": round(pv["as_is_value"], 2), "net": ch[b]["net"], "return": ch[b]["return"], "max_buy": ch[b]["max_buy"],
                          "channels": ch, "needs": net_bar, "days_added": 0.0, "passes": ch[b]["net"] >= net_bar and (ch[b]["return"] or 0) >= MIN_RETURN}
        if cls["class"] == "repairable":
            all_in = buy + cls["repair_cost"]
            chr_ = channels(v_work, kind, title, all_in, net_bar)
            br = _best_channel(chr_)
            need = net_bar + repair.REPAIR_CUSHION
            plays["repair"] = {"channel": br, "value": round(v_work, 2), "repair_cost": cls["repair_cost"], "repair_source": cls["repair_source"],
                               "net": chr_[br]["net"], "return": chr_[br]["return"], "all_in_cost": round(all_in, 2),
                               "max_buy": round(repair.max_buy_repair(chr_[br]["revenue"], cls["repair_cost"], net_bar, MIN_RETURN), 2),
                               "channels": chr_, "needs": need, "days_added": cls["repair_days"],
                               "passes": chr_[br]["net"] >= need and (chr_[br]["return"] or 0) >= MIN_RETURN}
        ev["plays"] = {k: {kk: vv for kk, vv in v.items() if kk != "channels"} for k, v in plays.items()}
        passing = [k for k, v in plays.items() if v["passes"]]
        play = max(passing, key=lambda k: plays[k]["net"]) if passing else (("repair" if "repair" in plays else "as-is"))
        ev["play"] = play if passing else None
        chosen = plays[play]
        ev["channels"] = chosen["channels"]
        ev["best_channel"], ev["net"], ev["return"], ev["max_buy"] = chosen["channel"], chosen["net"], chosen["return"], chosen["max_buy"]
        if ev.get("demand"):
            ev["demand"]["repair_days_included"] = chosen["days_added"]  # repair time is shown; no time-to-sell is invented
        floors = sell_channels.buyout_floors(kind, title, v_work) if cls["class"] in ("working", "cosmetic") else []
        if floors and floors[0]["low"] is not None:
            fn = floors[0]["low"] - buy
            fr = fn / buy if buy else None
            ev["buyout_floor"] = {"floors": floors, "net_at_low_floor": round(fn, 2), "return_at_low_floor": round(fr, 3) if fr is not None else None,
                                  "clears_at_floor": sell_channels.floor_signal(fn, fr, net_bar, MIN_RETURN)}
        ev["local_note"] = None if LOCAL_CHANNEL_COUNTS else (f"local cash at {LOCAL_FACTOR:.0%} of the sold median would net "
                                                              f"${chosen['channels']['local cash']['net']:,.0f} (assumed price, not counted)")
        if not passing:
            if cls["class"] in ("untested_unknown", "parts_only"):
                fails.append(("condition", f"{cls['reason']}; priced at PARTS value ${pv['as_is_value']:,.0f}, not as a working unit"
                              + (f" (a {repair.P_WORKS:.0%}-chance-it-works weighting would be ${pv['prob_weighted']:,.0f}, information only)" if pv["prob_weighted"] else "")))
            if cls["class"] == "repairable":
                rp = plays["repair"]
                fails.append(("repair economics", f"repaired net ${rp['net']:,.0f} after ${rp['repair_cost']:,.0f} estimated repair "
                              f"(needs ${rp['needs']:,.0f}: the ${net_bar:,.0f} bar plus a ${repair.REPAIR_CUSHION:,.0f} repair cushion) and a {(rp['return'] or 0):.0%} return (needs {MIN_RETURN:.0%})"))
            a = plays["as-is"]
            if cls["class"] in ("working", "cosmetic"):
                if a["net"] < net_bar:
                    fails.append(("profit bar", f"net ${a['net']:,.0f} is under the ${net_bar:,.0f} minimum"))
                if (a["return"] or 0) < MIN_RETURN:
                    fails.append(("profit bar", f"return {(a['return'] or 0):.0%} is under the {MIN_RETURN:.0%} minimum"))
            elif cls["class"] in ("untested_unknown", "parts_only"):
                fails.append(("profit bar", f"at parts value the net is ${a['net']:,.0f} (needs ${net_bar:,.0f} and {MIN_RETURN:.0%})"))
    if not value and cls["class"] in ("untested_unknown", "parts_only"):
        fails.append(("condition", f"{cls['reason']}; it would be priced at PARTS value, never as a working unit"))
    return _finish(res)


def _finish(res: dict) -> dict:
    if res["failures"]:
        res["failed"] = res["failures"][0][0]
    else:
        res["status"] = "verified"
    res["failures"] = [{"check": c, "why": w} for c, w in res["failures"]]
    return res


def assess_all(conn: sqlite3.Connection, now: datetime | None = None) -> list[dict]:
    ensure_schema(conn)
    conn.row_factory = sqlite3.Row
    return [assess(conn, r, now) for r in conn.execute("SELECT * FROM fb_listings ORDER BY first_seen DESC, listing_id")]


def page_payload(conn: sqlite3.Connection, now: datetime | None = None) -> dict:
    rows = assess_all(conn, now)
    return {"verified": sorted([r for r in rows if r["status"] == "verified"], key=lambda r: -r["evidence"]["net"]),
            "unverified": [r for r in rows if r["status"] != "verified"],
            "bar": {"min_net": MIN_NET, "min_return": MIN_RETURN, "min_comps": MIN_COMPS, "min_recent": MIN_RECENT_SALES,
                    "local_factor": LOCAL_FACTOR, "local_counts": LOCAL_CHANNEL_COUNTS}}
