"""Run: .venv/bin/python audit_export.py

READ-ONLY audit export: rebuilds the page's row set with the page's own functions (no pricing code changed) and writes audit_export.json/.csv."""
import collections, csv, json, re, sys
from datetime import datetime, timezone
sys.path.insert(0, ".")
import db, webpage, tracking, catalog, txauction, identity, repair, page_gate, luxury_goods as lg, mcp_auction_pricer as mp
from engine.title_cleaner import model_identity

AS_OF = datetime(2026, 10, 15, 16, 0, tzinfo=timezone.utc)   # the last catalog crawl (16:00 UTC); the page was built right after it
conn = db.connect("arbitrage.db")
snap = txauction.load_snapshot()
rows = webpage.collect(conn, 160, per_style=5, now=AS_OF, mobile_candidates=True)        # same call/limits as `main.py --web-refresh`
mine, _ = tracking.tracked_rows(conn)
des = [d for d in webpage.designer_rows(conn, now=AS_OF) if (d["platform"], d["listing_id"]) not in {(m["platform"], m["listing_id"]) for m in mine}]
rows = tracking.merge(rows, mine + des)
tx = webpage.tx_rows(snap, [], now=AS_OF)
items = []

def comps_for(r):
    """(range key actually used, sold sales stored under that key, [low, median, high] of them, ValueRange fields)."""
    from engine import knowledge as kb
    title = r["title"]
    rng, key = catalog.model_range(conn, title), None
    if rng:
        key = next((n.split("model-specific: ", 1)[1].split(";")[0] for n in rng.notes if "model-specific:" in n), None)
    else:
        style = r.get("style") or ""
        for k in kb.find_items(conn, f"{r.get('brand')} {style}", strict=True):
            vr = kb.compute_range(conn, k)
            if vr and vr.typical is not None:
                rng, key = vr, k
                break
    if not rng:
        return None, 0, None, None
    sales = []
    if key:
        sales = [s for s in mp._stored_sales(conn, key) if not (mp._COMP_JUNK.search(s[0]) or mp._ACCESSORY.search(s[0]) or mp._DAMAGED.search(s[0]))]
    pr = sorted(s[1] for s in sales)
    return key, len(sales), ([pr[0], pr[len(pr)//2], pr[-1]] if pr else None), {"n_obs": rng.n_obs, "basis": rng.basis, "confidence": rng.confidence, "newest_days": rng.newest_days, "notes": rng.notes}

def desc_of(platform, listing_id):
    row = conn.execute("SELECT description FROM catalog WHERE platform=? AND listing_id=?", (platform, str(listing_id))).fetchone()
    return (row[0] or "") if row else ""


def identity_fields(title, desc, price_based_on):
    """What the price assumed the item IS, and every identity / condition risk visible in the listing text."""
    idr = identity.check(title, desc)
    kind = catalog.style_of(title, "electronics")[1]
    dmg = repair.classify(title, desc, kind)
    risks = idr["contradictions"] + idr["risks"] + ([idr["reason"]] if idr["reason"] and not idr["contradictions"] else [])
    if dmg["class"] not in ("working",):
        risks.append(f"condition read as {dmg['class'].replace('_', ' ')}: {dmg['reason']}")
    return {"identity_price_based_on": price_based_on, "identity_claimed": idr["claimed"], "identity_confidence": idr["confidence"],
            "identity_risks": risks, "condition_class": dmg["class"], "repair_cost_estimate": dmg["repair_cost"] or None}


def classify(r):
    notes = " | ".join(r.get("range_notes") or [])
    if r["platform"] == "txauction":
        b = r.get("range_notes") or ""
        b = b if isinstance(b, str) else " | ".join(b)
        if "melt" in b.lower() or r["kind"] == "scrap":
            return "metal_melt", "gold/silver melt value from live spot (no sold comps)"
        if b and "no resale estimate" not in b:
            return "hand_set_estimate", b
        return "none", "no resale estimate"
    if r.get("coarse"):
        return "brand_baseline", notes or "brand-level baseline, not this exact model"
    return "sold_comps_model", notes

for r in rows + tx:
    cs, why = classify(r)
    ident, n, rng, rinfo = comps_for(r) if cs == "sold_comps_model" else (None, None, None, None)
    e = r.get("econ") or {}
    flags = []
    if cs == "sold_comps_model" and rinfo and rinfo["basis"] not in ("sold", "realized"):
        cs = "ask_based_range"
        flags.append("range rests on ASKING prices, not sold sales")
    if ident and "ipad" in r["title"].lower() and "iphone" in ident:
        flags.append(f"range key '{ident}' is a different product (iPhone) than the title (iPad)")
    bucket = ("5+ sold comps" if cs == "sold_comps_model" and (n or 0) >= 5 and not flags else "<5 sold comps" if cs == "sold_comps_model"
              else "fallback / baseline / hand-set / melt")
    items.append({
        "source": "page_row", "title": r["title"], "site": r["platform_label"], "listing_id": r["listing_id"], "url": r.get("url"),
        "kind": r.get("kind"), "brand": r.get("brand"), "current_bid": r["bid"], "bids": r.get("bids"), "ends": r.get("end"),
        "expected_close_bid": r.get("ends_at"), "value_low": (r.get("value") or {}).get("low"), "value_typ": (r.get("value") or {}).get("typ"),
        "value_high": (r.get("value") or {}).get("high"), "comp_source": cs, "value_basis_notes": why, "range_confidence": r.get("conf"),
        "model_identity": ident, "range_info": rinfo, "sold_comps_matching_model": n, "stored_comp_low_median_high": rng, "query_used": r.get("query"),
        "condition": r.get("condition"), "condition_factor": r.get("factor"), "repair_cost": r.get("fix"), "row_notes": r.get("notes"),
        "econ": e, "net_profit_at_bid": r.get("profit_now"), "net_profit_at_expected_close": r.get("profit_end"),
        "return_at_expected_close": r.get("roi_end"), "max_bid_all_in": r.get("max_bid"), "noval": r.get("noval", False),
        "bucket": bucket, "audit_flags": flags,
        **identity_fields(r["title"], desc_of(r["platform"], r["listing_id"]), ident or model_identity(r["title"]) or r.get("query")),
    })
for k, v in json.load(open("txauction_values.json")).items():
    if k.startswith("_"):
        continue
    items.append({"source": "txauction_hand_set", "title": f"TXAuction lot {k}", "site": "TXAuction", "listing_id": k, "url": None, "kind": None, "brand": None,
                  "current_bid": None, "bids": None, "ends": None, "expected_close_bid": None, "value_low": v.get("low"), "value_typ": v.get("value"), "value_high": v.get("high"),
                  "comp_source": "hand_set_estimate", "value_basis_notes": (v.get("basis") or "").replace("owner's estimate", "hand-set estimate"), "range_confidence": None, "model_identity": None, "range_info": None,
                  "sold_comps_matching_model": None, "stored_comp_low_median_high": None, "query_used": None, "condition": None, "condition_factor": None, "repair_cost": None,
                  "row_notes": ["bids and profit are not stored for these; the lots have ended (auction 31520 closed 2026-10-15; the snapshot no longer holds them)"], "econ": {},
                  "net_profit_at_bid": None, "net_profit_at_expected_close": None, "return_at_expected_close": None, "max_bid_all_in": None, "noval": False,
                  "bucket": "fallback / baseline / hand-set / melt"})
# the spec-matched pricer batch (last run output; its inputs are the stored sold sales or the free fallbacks)
for x in csv.DictReader(open("batch_pricing_report.csv")):
    if not x["value"]:
        continue
    src = x["comp_source"] or "database"
    n = int(float(x["comps"])) if x.get("comps") else None
    bucket = ("5+ sold comps" if src == "database" and (n or 0) >= 5 else "<5 sold comps" if src == "database" else "fallback / baseline / hand-set / melt")
    items.append({
        "source": "pricer_batch", "title": x["title"], "site": x["source"], "listing_id": x["lot"], "url": x["url"], "kind": x["kind"], "brand": None,
        "current_bid": float(x["bid"] or 0), "bids": x["bids"], "ends": x["end"], "expected_close_bid": None, "value_low": float(x["comp_low"]) if x.get("comp_low") else None,
        "value_typ": float(x["value"]), "value_high": float(x["comp_high"]) if x.get("comp_high") else None, "comp_source": src, "value_basis_notes": x["basis"],
        "range_confidence": None, "model_identity": None, "range_info": None, "sold_comps_matching_model": n, "stored_comp_low_median_high": None, "query_used": x["basis"],
        "condition": "assumed good", "condition_factor": 1.0, "repair_cost": 0, "row_notes": None, "econ": {"tax": 0.1025, "premium": None, "inbound": None},
        "net_profit_at_bid": float(x["profit"]), "net_profit_at_expected_close": None, "return_at_expected_close": None,
        "return_at_bid": float(x["roi"]) / 100, "max_bid_all_in": float(x["max_bid_ceiling"]), "noval": False, "bucket": bucket,
        **identity_fields(x["title"], desc_of(x["source"], x["lot"]), x["basis"]),
    })

# ---- designer / luxury items: how they were priced, their damage notes, and what the authenticity + condition gate says about them
for i in items:
    if i["source"] == "txauction_hand_set":
        continue
    desc = desc_of(i["site"].lower().replace(" ", ""), i["listing_id"]) if i["source"] == "page_row" else desc_of(i["site"], i["listing_id"])
    au = lg.authenticity(i["title"], desc)
    if not au["brand"]:
        continue
    dm = lg.find_damage(i["title"], desc)
    e = i.get("econ") or {}
    gate = lg.assess(conn, {"title": i["title"], "description": desc, "platform": i["site"], "platform_label": i["site"], "listing_id": i["listing_id"], "url": i["url"],
                            "end": i["ends"], "bid": i["current_bid"] or 0.0, "climb": e.get("climb"), "prem": e.get("prem") or 0.0, "tax": e.get("tax") or 0.1025,
                            "inbound": e.get("inbound") or 15.0}, AS_OF)
    ev = gate["evidence"]
    i["designer_review"] = {
        "brand": au["brand"], "brand_tier": lg.BRAND_TIER.get(au["brand"], "other"), "authenticity_verdict": au["verdict"], "authenticity_evidence": au["evidence"],
        "authenticity_reasons": au["reasons"], "damage_notes": [f"{d['label']} ({d['verdict']}; \"{d['matched']}\")" for d in dm["items"] + dm["killers"]],
        "value_basis_used_by_existing_pricing": {"value_typ": i["value_typ"], "range": [i["value_low"], i["value_high"]], "comp_source": i["comp_source"],
                                                  "notes": i["value_basis_notes"], "confidence": i["range_confidence"], "sold_comps_matching_model": i["sold_comps_matching_model"],
                                                  "condition_assumption": f"condition factor {i['condition_factor']}; authenticity factor applied on the page: see row_notes"},
        "luxury_gate": {"status": gate["status"], "failed_check": gate["failed"], "why": [f["why"] for f in gate["failures"]],
                        "condition_matched_comps": ev.get("comps"), "plays": ev.get("plays")},
    }
n_des = [i for i in items if "designer_review" in i]
print("designer items", len(n_des), "| gate:", collections.Counter(i["designer_review"]["luxury_gate"]["status"] for i in n_des),
      "| failed:", collections.Counter(i["designer_review"]["luxury_gate"]["failed_check"] for i in n_des),
      "| with damage notes:", sum(1 for i in n_des if i["designer_review"]["damage_notes"]), "| priced on page rows:", sum(1 for i in n_des if i["source"] == "page_row"))

# ---- the verified gate applied to the page rows: what each old claim becomes
gate_by_key = {}
for r in rows:
    if r.get("kind") == "scrap":
        continue
    g = page_gate.assess_row(conn, r, AS_OF)
    gate_by_key[(r["platform"], str(r["listing_id"]))] = g
for i in items:
    if i["source"] != "page_row":
        continue
    g = gate_by_key.get((next((r["platform"] for r in rows if str(r["listing_id"]) == str(i["listing_id"]) and r["title"] == i["title"]), None), str(i["listing_id"])))
    if g:
        ev = g["evidence"]
        i["verified_gate"] = {"status": g["status"], "failed_check": g["failed"], "why": [f["why"] for f in g["failures"]], "identity_confidence": ev.get("identity", {}).get("confidence"),
                              "spec_matched_comps": (ev.get("value_tier") or {}).get("n"), "value_tier": (ev.get("value_tier") or {}).get("tier"),
                              "verified_value": (ev.get("value_tier") or {}).get("value"), "verified_net_at_expected_close": ev.get("net"),
                              "old_claimed_net_at_expected_close": i["net_profit_at_expected_close"], "old_claimed_value": i["value_typ"],
                              "demand": ev.get("demand")}
print("page rows gated:", collections.Counter(i["verified_gate"]["status"] for i in items if "verified_gate" in i),
      "| failed:", collections.Counter(i["verified_gate"]["failed_check"] for i in items if "verified_gate" in i))

json.dump({"as_of": AS_OF.isoformat(), "note": "page rows regenerated with the page's own functions at the last crawl; pricer_batch rows are the saved batch_pricing_report.csv (priced rows only)", "items": items}, open("audit_export.json", "w"), indent=1, default=str)
keys = ["source","site","title","listing_id","kind","current_bid","ends","expected_close_bid","value_low","value_typ","value_high","comp_source","value_basis_notes","range_confidence","model_identity","sold_comps_matching_model","query_used","condition","condition_factor","net_profit_at_bid","net_profit_at_expected_close","return_at_expected_close","max_bid_all_in","bucket","audit_flags","identity_price_based_on","identity_confidence","identity_risks","condition_class","repair_cost_estimate","url"]
with open("audit_export.csv", "w", newline="") as f:
    w = csv.DictWriter(f, keys, extrasaction="ignore"); w.writeheader(); w.writerows(items)
import collections
print("total", len(items), collections.Counter(i["source"] for i in items))
print(collections.Counter(i["bucket"] for i in items)); print(collections.Counter((i["source"], i["comp_source"]) for i in items))
priced = [i for i in items if i["source"] != "txauction_hand_set" and not i["noval"] and (i["net_profit_at_expected_close"] is not None or i["net_profit_at_bid"] is not None)]
def claimed(i): return i["net_profit_at_expected_close"] if i["net_profit_at_expected_close"] is not None else i["net_profit_at_bid"]
for i in sorted(priced, key=lambda i: -(claimed(i) or 0))[:20]:
    print(f"{claimed(i):>8.0f} | {i['source'][:5]} | {i['site'][:10]} | {i['title'][:55]} | bid {i['current_bid']:.0f} val {i['value_typ']:.0f} | {i['comp_source']} n={i['sold_comps_matching_model']} | {i['bucket']}")
