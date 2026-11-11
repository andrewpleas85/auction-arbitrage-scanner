"""The verified gate for the existing auction page, plus the separate "Repair plays" section.

A page row may appear as PROFITABLE only when
  * identity is HIGH (identity.py: a model in the title, corroborated by a second source, nothing contradicting it),
  * its value rests on >= evidence.VERIFY_MIN_COMPS spec-matched stored SOLD sales (value tiers in evidence.py: median / 25th percentile),
  * the item is a working unit (repairable damage goes to its own "Repair plays" section; untested/parts are priced at parts value),
  * demand shows sold comps in the last 90 days, and
  * net >= the bar and return >= 40% of all-in cost, recomputed from the verified value with the row's own site economics.
Everything else is UNVERIFIED. Its old numbers stay visible, labelled as unverified claims, next to the first failed check.
Rows valued by live metal melt (scrap) are exempt: melt is objective and has its own section.
"""
from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone

import catalog
import evidence
import fb_market
import identity as ident_mod
import repair
from engine.title_cleaner import model_identity

MIN_RECENT_SALES = 3


def _desc(conn: sqlite3.Connection, platform: str, listing_id) -> str:
    r = conn.execute("SELECT description FROM catalog WHERE platform=? AND listing_id=?", (platform, str(listing_id))).fetchone()
    return (r[0] if r else "") or ""


def _auction_net(value: float, econ: dict, bid: float, fix: float, factor: float, net_bar: float, extra_cost: float = 0.0) -> dict:
    """Net profit and return at the price an auction is expected to reach (bid, or the site's climb share of the verified value, whichever is higher)."""
    price = max(bid or 0.0, (econ.get("climb") or 0.0) * value)
    prem, tax, inbound = econ.get("prem", 0.0), econ.get("tax", 0.0), econ.get("inbound", 0.0)
    fee, order = catalog.fees_for(econ, value * factor)
    rev = value * factor * (1 - fee - econ.get("returns", 0.0)) - order - econ.get("ship_out", 0.0)
    all_in = price * (1 + prem) * (1 + tax) + inbound + fix + extra_cost
    net = rev - all_in
    mult = (1 + prem) * (1 + tax)
    a_max = max(0.0, min(rev - net_bar, rev / (1 + evidence.MIN_RETURN)))
    return {"price_assumed": round(price, 2), "all_in": round(all_in, 2), "revenue": round(rev, 2), "net": round(net, 2),
            "return": round(net / all_in, 3) if all_in else None, "max_bid": round(max(0.0, (a_max - inbound - fix - extra_cost) / mult), 2),
            "net_bar": net_bar}


def assess_row(conn: sqlite3.Connection, row: dict, now: datetime | None = None, description: str | None = None) -> dict:
    """row: a page row (title, platform, listing_id, bid, econ{prem,tax,inbound,fee,order_fee,returns,ship_out,climb}, factor, fix, kind, value{typ}, ...)."""
    now = now or datetime.now(timezone.utc)
    title = row["title"]
    desc = description if description is not None else (row.get("description") or _desc(conn, row["platform"], row["listing_id"]))
    econ = row.get("econ") or {}
    claimed = {"worth_typ": (row.get("value") or {}).get("typ"), "worth_low": (row.get("value") or {}).get("low"), "worth_high": (row.get("value") or {}).get("high"),
               "basis": "; ".join(row.get("range_notes") or []) if isinstance(row.get("range_notes"), list) else (row.get("range_notes") or ""),
               "confidence": row.get("conf"), "profit_at_expected_close": row.get("profit_end"), "return_at_expected_close": row.get("roi_end"),
               "profit_at_bid": row.get("profit_now"), "max_bid": row.get("max_bid"), "note": "UNVERIFIED CLAIM: the old page number, not backed by the verified gate"}
    res = {k: row.get(k) for k in ("platform", "platform_label", "listing_id", "url", "title", "end", "bid", "kind", "rank")}
    res.update({"failures": [], "evidence": {}, "claimed": claimed, "status": "unverified", "failed": None, "needs_check": None, "tracked": bool(row.get("tracked"))})
    fails, ev = res["failures"], res["evidence"]

    ident = model_identity(title)
    ev["model"] = ident
    idr = ident_mod.check(title, desc)
    ev["identity"] = {"confidence": idr["confidence"], "claimed": idr["claimed"], "contradictions": idr["contradictions"], "risks": idr["risks"],
                      "corroborated_by": idr["corroborated_by"], "price_based_on": ident}
    if not ident:
        fails.append(("model not identified", "the title names no brand/model we can match to sold sales"))
    if not idr["verified"]:
        fails.append(("identity", idr["reason"]))
        res["needs_check"] = idr["next_step"]
    cls = repair.classify(title, desc, row.get("kind") or "other")
    ev["condition"] = {"class": cls["class"], "reason": cls["reason"], "defects": cls["defects"]}
    if not ident:
        return _finish(res)

    comps = fb_market._comps(conn, title, ident)
    tier = evidence.value_tier([p for _, p, _ in comps])
    ev["value_tier"] = {k: tier[k] for k in ("n", "tier", "value", "median", "p25", "iqr_ratio", "wide", "net_bar", "low", "high")}
    ev["comps"] = len(comps)
    if len(comps) < evidence.VERIFY_MIN_COMPS or tier["value"] is None:
        fails.append(("sold comps", f"{len(comps)} spec-matched sold sale(s) stored for '{ident}', need {evidence.VERIFY_MIN_COMPS}"
                      + (f" (would be valued at ${tier['value']:,.0f} with a ${tier['net_bar']:,.0f} bar if the thin tier were allowed)" if tier["value"] else "")))
        res["needs_check"] = res["needs_check"] or f"look up eBay sold prices for: {ident}"
    if comps:
        ev["demand"] = evidence.demand(comps, now, evidence.active_count(conn, ident))
        if ev["demand"]["sold_90d"] < MIN_RECENT_SALES:
            fails.append(("demand", f"only {ev['demand']['sold_90d']} matching sale(s) in the last 90 days, need {MIN_RECENT_SALES}"))
    if tier["wide"]:
        ev["flags"] = ["wide spread: comps vary by more than half their median, so the 25th percentile is used"]
    if tier["value"] is None:
        return _finish(res)

    value, factor, fix = tier["value"], row.get("factor", 1.0) or 1.0, row.get("fix", 0.0) or 0.0
    net_bar = tier["net_bar"]
    pv = repair.paths(value, cls)
    ev.update({"working_value": round(value, 2), "as_is_value": round(pv["as_is_value"], 2), "value_basis": pv["basis"]})
    plays = {"as-is": {**_auction_net(pv["as_is_value"], econ, row.get("bid") or 0.0, fix, factor, net_bar), "value": round(pv["as_is_value"], 2), "days_added": 0.0}}
    plays["as-is"]["passes"] = plays["as-is"]["net"] >= net_bar and (plays["as-is"]["return"] or 0) >= evidence.MIN_RETURN
    if cls["class"] == "repairable":
        rp = _auction_net(value, econ, row.get("bid") or 0.0, fix, factor, net_bar + repair.REPAIR_CUSHION, extra_cost=cls["repair_cost"])
        plays["repair"] = {**rp, "value": round(value, 2), "repair_cost": cls["repair_cost"], "repair_source": cls["repair_source"], "days_added": cls["repair_days"],
                           "passes": rp["net"] >= net_bar + repair.REPAIR_CUSHION and (rp["return"] or 0) >= evidence.MIN_RETURN}
        ev["repaired_value"] = round(value, 2)
    ev["plays"] = plays
    passing = [k for k, v in plays.items() if v["passes"]]
    chosen = max(passing, key=lambda k: plays[k]["net"]) if passing else ("repair" if "repair" in plays else "as-is")
    c = plays[chosen]
    ev["play"] = chosen if passing else None
    ev["net"], ev["return"], ev["max_bid"], ev["price_assumed"], ev["all_in"] = c["net"], c["return"], c["max_bid"], c["price_assumed"], c["all_in"]
    if ev.get("demand"):
        ev["demand"]["repair_days_included"] = c["days_added"]

    if cls["class"] in ("untested_unknown", "parts_only"):
        fails.append(("condition", f"{cls['reason']}; priced at PARTS value ${pv['as_is_value']:,.0f}, not as a working unit"))
    elif not passing:
        if cls["class"] == "repairable":
            fails.append(("repair economics", f"repaired net ${c['net']:,.0f} after ${c['repair_cost']:,.0f} estimated repair (needs ${c['net_bar'] + 0:,.0f} incl. the "
                          f"${repair.REPAIR_CUSHION:,.0f} cushion) and a {(c['return'] or 0):.0%} return (needs {evidence.MIN_RETURN:.0%})"))
        else:
            if c["net"] < net_bar:
                fails.append(("profit bar", f"net ${c['net']:,.0f} at the expected close is under the ${net_bar:,.0f} minimum"))
            if (c["return"] or 0) < evidence.MIN_RETURN:
                fails.append(("profit bar", f"return {(c['return'] or 0):.0%} is under {evidence.MIN_RETURN:.0%}"))
    if cls["class"] == "repairable" and passing:
        ev["repair_play"] = True
    return _finish(res)


def _finish(res: dict) -> dict:
    if res["failures"]:
        res["failed"] = res["failures"][0][0]
    else:
        res["status"] = "verified"
    res["failures"] = [{"check": c, "why": w} for c, w in res["failures"]]
    return res


def split(conn: sqlite3.Connection, rows: list[dict], now: datetime | None = None) -> dict:
    """Apply the gate to every page row. Returns {'main': rows allowed in the main table (verified + the owner's tracked rows + melt-valued scrap),
    'unverified': gate results with the old claims, 'verified': gate results}. Rows get a 'gate' field."""
    now = now or datetime.now(timezone.utc)
    main, unverified, verified, repair_rows = [], [], [], []
    for r in rows:
        if r.get("kind") == "scrap":  # valued by live metal melt, not by sold comps; it has its own section and is not part of this gate
            r["gate"] = {"status": "exempt", "note": "scrap valued by live metal melt"}
            main.append(r)
            continue
        g = assess_row(conn, r, now)
        r["gate"] = {"status": g["status"], "failed": g["failed"], "play": g["evidence"].get("play")}
        if g["status"] == "verified" and g["evidence"].get("play") == "as-is":
            main.append(r)
            verified.append(g)
        elif g["status"] == "verified":  # a verified REPAIR play: its own section, never the main table
            repair_rows.append(g)
            if r.get("tracked"):
                main.append(r)
        else:
            if r.get("tracked"):
                main.append(r)  # the owner's own tracked lots always show; the gate result is attached
            unverified.append(g)
    return {"main": main, "unverified": unverified, "verified": verified, "repair": repair_rows}


def repair_candidates(conn: sqlite3.Connection, now: datetime | None = None, max_days: float = 2.0) -> list[dict]:
    """Open auction lots that state a repairable defect, as gate inputs (their own section: the main table's filters are untouched)."""
    import webpage
    now = now or datetime.now(timezone.utc)
    end = (now + timedelta(days=max_days)).isoformat()
    out = []
    for r in conn.execute("SELECT * FROM catalog WHERE listing_type='auction' AND end_time>? AND end_time<?", (now.isoformat(), end)):
        if webpage.FOCUS_KINDS and r["kind"] not in webpage.FOCUS_KINDS:
            continue
        desc = r["description"] or ""
        if repair.classify(r["title"], desc, r["kind"])["class"] != "repairable":
            continue
        econ = catalog.economics(r)
        out.append({"title": r["title"], "platform": r["platform"], "platform_label": webpage.PLATFORM_LABEL.get(r["platform"], r["platform"]), "listing_id": r["listing_id"],
                    "url": r["url"], "end": r["end_time"], "bid": r["current_bid"] or 0.0, "kind": r["kind"], "econ": econ, "factor": 1.0, "fix": 0.0, "_desc": desc})
    return out


def repair_plays(conn: sqlite3.Connection, now: datetime | None = None) -> dict:
    now = now or datetime.now(timezone.utc)
    results = [assess_row(conn, c, now, description=c.pop("_desc")) for c in repair_candidates(conn, now)]
    return {"verified": sorted([g for g in results if g["status"] == "verified"], key=lambda g: -g["evidence"]["net"]),
            "unverified": [g for g in results if g["status"] != "verified"]}


def page_payload(conn: sqlite3.Connection, rows: list[dict], now: datetime | None = None) -> dict:
    now = now or datetime.now(timezone.utc)
    sp = split(conn, rows, now)
    rp = repair_plays(conn, now)
    seen = {(g["platform"], str(g["listing_id"])) for g in rp["verified"] + rp["unverified"]}
    rp["verified"] = sorted(rp["verified"] + [g for g in sp["repair"] if (g["platform"], str(g["listing_id"])) not in seen], key=lambda g: -g["evidence"]["net"])
    return {"main": sp["main"], "unverified": sp["unverified"], "verified": sp["verified"], "repair": rp,
            "bar": {"min_net": evidence.MIN_NET, "min_return": evidence.MIN_RETURN, "min_comps": evidence.VERIFY_MIN_COMPS, "repair_cushion": repair.REPAIR_CUSHION,
                    "thin_net": evidence.THIN_NET}}
