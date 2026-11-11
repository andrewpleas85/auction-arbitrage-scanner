"""Batch-price every open auction lot in arbitrage.db that fits the high-yield target list and keep those netting $100+.

Run:  .venv/bin/python batch_evaluate.py            (no network, no paid quota: reads stored sold sales only)
Writes batch_pricing_report.csv and prints the lots that clear $100 net profit, biggest first.
Values come from `mcp_auction_pricer.evaluate_high_value_tech` / `evaluate_luxury_designer_goods` (stored eBay SOLD sales
whose titles agree on chip, RAM, storage and screen). Lots with fewer than 3 agreeing sales are SKIPPED, never guessed.
"""
import csv
import json
import re
import sqlite3
from pathlib import Path
from datetime import datetime, timezone

import catalog
import mcp_auction_pricer as m
import repair
import txauction
from engine.luxury_authenticator import detect_brand_and_tier

MID_TIER = re.compile(r"michael\s+kors|\bmk\b|kate\s+spade|\bcoach\b|dooney|tommy\s+hilfiger|vera\s+bradley|\bfossil\b|"
                      r"tory\s+burch|steve\s+madden|guess\b|nine\s+west|calvin\s+klein|kenneth\s+cole", re.I)
JUNK = re.compile(r"for parts|parts only|parts or|repair|broken|cracked|shattered|not working|no power|locked|icloud|read (?:desc|below)|"
                  r"bad [a-z] key|lcd|assembly|housing|replacement|empty box|box only|motherboard|screen only|damaged|salvage|"
                  r"untested|as[- ]is|abandoned|\bbios\b|activation|\bno (?:ram|ssd|hdd|battery|os)\b|lot of|\blots?\b|"
                  r"case|cover|charger|stylus|pencil only|keyboard only|skin|sleeve", re.I)
LOW_SPEC = re.compile(r"chromebook|kindle\s*(?:6|paperwhite|basic|fire)|fire\s+hd|ipad\s*(?:mini\s*)?[1-8](?:st|nd|rd|th)?\s*gen|"
                      r"ipad\s*(?:6|7|8)\b|\ba1\d{3}\b.{0,40}\b(?:2015|2016|2017|2018)\b|\b201[2-8]\b.{0,30}macbook|"
                      r"macbook.{0,60}\b201[2-8]\b|\b(?:a1708|a1534|a1466|a1502|a1398)\b", re.I)
# (name, family for the comp search, title regex, optional reject predicate on specs)
FAMILIES = [
    ("drone", "", re.compile(r"\bdji\b.{0,30}\b(?:mavic|mini|air\s*[23]|avata|phantom|inspire)|\bautel\b.{0,20}\bevo\b", re.I)),
    ("ipad pro", "ipad pro", re.compile(r"ipad\s*pro", re.I)),
    ("ipad air", "ipad air", re.compile(r"ipad\s*air", re.I)),
    ("galaxy tab", "samsung galaxy tab", re.compile(r"galaxy\s*tab\s*s(?:8|9|10)\s*(?:ultra|\+|plus)?", re.I)),
    ("macbook pro", "macbook pro", re.compile(r"macbook\s*pro", re.I)),
    ("macbook air", "macbook air", re.compile(r"macbook\s*air", re.I)),
    ("dell xps", "dell xps", re.compile(r"\bxps\s*(?:15|17)", re.I)),
    ("thinkpad x1", "thinkpad x1 carbon", re.compile(r"thinkpad\s*x1\s*carbon", re.I)),
    ("gaming laptop", "", re.compile(r"\brtx\s*(?:3070|3080|4060|4070|4080|4090)\b", re.I)),
    ("mirrorless", "", re.compile(r"sony\s*(?:alpha|a7|a9|a6)|canon\s*eos\s*r\d?|\beos\s*r\d", re.I)),
    ("handheld", "", re.compile(r"steam\s*deck|rog\s*ally|legion\s*go", re.I)),
    ("e-reader", "", re.compile(r"kindle\s*scribe|\bboox\b", re.I)),
]
LUX_BRANDS = {"louis vuitton", "chanel", "gucci", "hermes", "prada", "dior", "rolex", "cartier", "van cleef", "bottega veneta",
              "saint laurent", "goyard", "tiffany"}
LUX_EXTRA = re.compile(r"\b(rolex|cartier|van\s+cleef|tiffany)\b", re.I)


PART_LISTING = re.compile(r"lcd|display|assembly|housing|replacement|empty box|box only|motherboard|screen only|keyboard only|\\bcase\\b|cover|charger|"
                          r"stylus|skin|sleeve|lot of|\\blots?\\b", re.I)  # a listing FOR a part or an accessory, never the whole device
DAMAGED = ("repairable", "untested_unknown", "parts_only")


def classify(title: str, kind: str, allow_damaged: bool = False) -> tuple[str, str] | None:
    """(category, family) if the lot is on the target list, else None (auto-SKIPPED)."""
    if MID_TIER.search(title) or (JUNK.search(title) and not allow_damaged) or LOW_SPEC.search(title):
        return None
    for name, fam, rx in FAMILIES:
        if rx.search(title):
            sp = m.parse_tech_specs(title)
            if name == "macbook air" and (sp["ram_gb"] or 0) < 16:
                return None
            if name == "macbook pro" and sp["chip"] is None and not re.search(r"\b20(?:19|2\d)\b", title):
                return None
            if name == "macbook pro" and not (sp["chip"] or "").endswith(("pro", "max")) and \
                    (sp["screen_in"] or (re.search(r"\b(14|16)(?:\.\d)?\b", title) or [None, None])[1] or "0").split(".")[0] not in ("14", "16"):
                return None  # only 14"/16" Pros: a 13" Pro (or one with no stated size) is not on the list
            if name == "ipad pro" and sp["chip"] not in ("m1", "m2", "m4") and not re.search(r"\b202[1-9]\b|5th|6th", title, re.I):
                return None
            if name == "ipad air" and sp["chip"] not in ("m1", "m2", "m3") and not re.search(r"5th|6th", title, re.I):
                return None
            if name == "gaming laptop" and kind not in ("laptop", "other"):
                return None
            return name, fam
    brand = (detect_brand_and_tier(title)[0] or "")
    if kind in ("bag", "wallet", "watch", "clothing", "accessory", "jewelry") and (brand in LUX_BRANDS or LUX_EXTRA.search(title)):
        return "luxury", brand
    return None


def run(db_path: str = "arbitrage.db", snapshot_path: str = "txauction_snapshot.json", now_iso: str | None = None) -> tuple[list[dict], int, int]:
    """(results, lots scanned, lots on the target list). Reads stored data only: no network, no paid quota."""
    now = now_iso or datetime.now(timezone.utc).isoformat()  # now_iso lets an old crawl be re-priced as of its own date
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    rows = conn.execute("select * from catalog where listing_type='auction' and end_time>?", (now,)).fetchall()
    lots = []
    for r in rows:
        lots.append({"source": r["platform"], "lot": r["listing_id"], "title": r["title"], "kind": r["kind"], "bid": r["current_bid"] or 0.0,
                     "bids": r["bids"], "end": r["end_time"], "url": r["url"], "row": r, "desc": r["description"] or ""})
    snap = json.load(open(snapshot_path)) if Path(snapshot_path).exists() else {"auctions": []}
    for w in txauction.snapshot_watch_rows(snap):
        if w["topic"] in ("watch", "jewelry") and (not w.get("end") or w["end"] >= now):
            lots.append({"source": "txauction", "lot": w.get("key") or w.get("lot", ""), "title": w["title"], "kind": w["topic"],
                         "bid": w.get("bid") or 0.0, "bids": w.get("bids"), "end": w.get("end") or "", "url": w.get("url"), "row": None, "desc": ""})
    out, considered = [], 0
    for L in lots:
        if L["source"] == "txauction" and L["kind"] == "jewelry":
            # unbranded gold/silver/platinum jewelry: intrinsic melt from live spot (free). 0% USMS premium, ships to Chicago.
            considered += 1
            res = m.evaluate_luxury_watch_jewelry(L["title"])
            rec = {k: L[k] for k in ("source", "lot", "title", "kind", "bid", "bids", "end", "url")}
            rec.update(category="jewelry (melt)", comp_source=res.get("comp_source"), premium=0.0, inbound=25.0)
            if not res.get("value"):
                rec.update(status="SKIPPED (NO STORED COMPS)", basis=res.get("reason", ""))
                out.append(rec)
                continue
            mg = m.calculate_arbitrage_margin(rec["bid"], res["value"], 0.15, 15.0, 0.0, m.CHICAGO_TAX, 25.0)
            rec.update(value=res["value"], basis=f"{res['metal']} {res['purity_pct']}% x {res['weight_grams']} g @ ${res['spot_price_gram']}/g ({res['spot_source']}), {int(res['dealer_payout']*100)}% payout"
                       + ("; stones in weight = upper bound" if res["stones_flag"] else ""), profit=mg["net_profit"], roi=mg["roi_pct"],
                       max_bid_ceiling=mg["max_bid_ceiling"], max_bid_100=mg["max_bid_for_min_profit"], status=mg["recommendation"])
            out.append(rec)
            continue
        dmg = repair.classify(L["title"], L["desc"], L["kind"])
        damaged = dmg["class"] in DAMAGED and not PART_LISTING.search(L["title"])
        c = classify(L["title"], L["kind"], allow_damaged=damaged)
        if not c:
            continue
        considered += 1
        cat, fam = c
        rec = {k: L[k] for k in ("source", "lot", "title", "kind", "bid", "bids", "end", "url")}
        rec["category"] = cat
        if L["row"] is not None:
            e = catalog.economics(L["row"])
            prem, inbound, fee, ship = e["prem"], e["inbound"], e["fee"], e["ship_out"]
            # a pricer value under 'good' comps; economics fee already reflects bag/watch category fees
        else:
            prem, inbound, fee, ship = 0.0, 25.0, 0.15, 15.0
        if cat == "luxury":
            proof = bool(re.search(r"authenticat|\bcoa\b|certificate|receipt|dust\s*bag|box", L["title"] + " " + L["desc"], re.I))
            res = m.evaluate_luxury_designer_goods(fam, L["kind"], L["title"][:60], "good", proof, proof)
            val, basis = res.get("as_is_value"), "luxury stored comps"
            rec["comps"] = (res.get("range") or {}).get("n_obs")
        else:
            res = m.evaluate_high_value_tech(L["title"], fam)
            val = res.get("value")
            basis = f'{res.get("match_level")}: {res.get("query")}' if val else res.get("reason", "")
            ss = res.get("sold_sample") or {}
            rec["comp_source"] = res.get("comp_source")
            rec["comps"] = ss.get("sample_size")
            rec["comp_low"], rec["comp_high"] = ss.get("low_sold_price"), ss.get("high_sold_price")
            rec["recent"] = ss.get("recent_sold")
            rec["specs"] = json.dumps(res.get("specs"))
        if not val:
            rec.update(status="SKIPPED (NO STORED COMPS)", basis=basis)
            out.append(rec)
            continue
        rec.update(condition_class=dmg["class"], working_value=round(val, 2))
        if dmg["class"] == "cosmetic":
            val = val * repair.COSMETIC_FACTOR
            basis += f"; cosmetic wear x {repair.COSMETIC_FACTOR}"
        if damaged:
            pv = repair.paths(val, dmg)
            as_is = m.calculate_arbitrage_margin(rec["bid"], pv["as_is_value"], fee, ship, prem, m.CHICAGO_TAX, inbound)
            rec.update(as_is_value=round(pv["as_is_value"], 2), as_is_net=as_is["net_profit"], repair_class=dmg["class"], basis=basis + "; " + pv["basis"])
            chosen = as_is
            val = pv["as_is_value"]
            rec["play"] = "as-is (parts value)"
            if dmg["class"] == "repairable":
                rep = m.calculate_arbitrage_margin(rec["bid"], pv["repaired_value"], fee, ship, prem, m.CHICAGO_TAX, inbound + dmg["repair_cost"],
                                                   min_net_profit=m.MIN_NET_PROFIT + repair.REPAIR_CUSHION)
                rec.update(repaired_value=round(pv["repaired_value"], 2), repaired_net=rep["net_profit"], repair_cost=dmg["repair_cost"],
                           repair_source=dmg["repair_source"], repair_days=dmg["repair_days"])
                if rep["net_profit"] > as_is["net_profit"]:
                    chosen, val, rec["play"] = rep, pv["repaired_value"], "repair"
            if dmg["class"] == "untested_unknown":
                rec["prob_weighted_value"] = round(pv["prob_weighted"], 2)
            mg = chosen
            rec["category"] = f"{rec['category']} ({rec['play']})"
        else:
            mg = m.calculate_arbitrage_margin(rec["bid"], val, fee, ship, prem, m.CHICAGO_TAX, inbound)
        rec.update(value=val, basis=basis if not damaged else rec["basis"], profit=mg["net_profit"], roi=mg["roi_pct"], max_bid_ceiling=mg["max_bid_ceiling"],
                   max_bid_100=mg["max_bid_for_min_profit"], status=mg["recommendation"])
        if rec.get("comp_source") in ("swappa_free", "spec_matrix", "metal_spot"):
            rec["status"] += f" [PRICED (FREE FALLBACK): {rec['comp_source']}]"
        out.append(rec)
    return out, len(lots), considered


def main() -> None:
    out, n_lots, considered = run()
    fields = ["source", "lot", "title", "category", "kind", "bid", "bids", "end", "value", "max_bid_ceiling", "max_bid_100", "profit",
              "roi", "condition_class", "play", "working_value", "as_is_value", "as_is_net", "repaired_value", "repaired_net", "repair_cost", "repair_days", "repair_source", "comp_source", "comps", "comp_low", "comp_high", "basis", "status", "specs", "url"]
    with open("batch_pricing_report.csv", "w", newline="") as f:
        w = csv.DictWriter(f, fields, extrasaction="ignore")
        w.writeheader()
        w.writerows(out)
    good = sorted([o for o in out if o.get("value") and o["profit"] >= m.MIN_NET_PROFIT], key=lambda o: -o["profit"])
    skipped = [o for o in out if not o.get("value")]
    print(f"{n_lots} open lots scanned, {considered} on the target list, {len(out)-len(skipped)} priced, {len(good)} net >= $100, {len(skipped)} no comps")
    print("| Lot | Item | Bid | Value | Max bid (25% ROI) | Max bid ($100) | Net profit | ROI | Comps | Ends | Source |")
    for o in good:
        print(f"| {o['lot']} | {o['title'][:60]} | ${o['bid']:.0f} | ${o['value']:.0f} | ${o['max_bid_ceiling']:.0f} | ${o['max_bid_100']:.0f} | ${o['profit']:.0f} | {o['roi']}% | {o.get('comps') or o.get('comp_source') or ''} | {o['end'][5:16]} | {o['source']} |")
    import collections
    print("skipped by category:", collections.Counter(o["category"] for o in skipped))


if __name__ == "__main__":
    main()
