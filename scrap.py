"""Hand-entered scrap-metal lots (jewelry and bullion sold by weight) for the page's scrap section.

These are priced by melt, not by eBay sold prices, so they stay out of the main table. The lots live in
scrap_lots.json (karat/fineness and stated grams read from the lot titles); the page recomputes with
editable spot, payout, metal-share and shipping boxes. Python mirrors the same arithmetic for tests.
"""
import json
import time
from pathlib import Path

from engine.melt import GOLD_KARAT, OZT

DEFAULT_PAYOUT = 0.85      # refiner share of melt (config.melt_payout_scrap)
DEFAULT_BULLION_PAYOUT = 0.95
DEFAULT_REFINER_SHIP = 12.0  # insured shipping to the refiner (config.melt_ship_usd)
DEFAULT_METAL_SHARE = 0.90   # stated weights are gross: clasps, stones and solder count in the grams (assumption)
MIN_ROI = 0.30
MIN_PROFIT = 15.0


def fine_grams(lot: dict) -> dict:
    """Pure gold and silver grams in a lot at the stated weights (before the metal-share haircut)."""
    gold = sum(g * GOLD_KARAT[int(k)] for k, g in lot.get("gold", []))
    silver = sum(g * float(f) for f, g in lot.get("silver", []))
    return {"gold": round(gold, 3), "silver": round(silver, 3)}


def economics(lot: dict, auction: dict, spot: dict, payout: float = DEFAULT_PAYOUT, metal_share: float = DEFAULT_METAL_SHARE,
              refiner_ship: float = DEFAULT_REFINER_SHIP, bid: float | None = None) -> dict:
    fg = fine_grams(lot)
    melt = metal_share * (fg["gold"] * spot["gold"] + fg["silver"] * spot["silver"]) / OZT
    pay = DEFAULT_BULLION_PAYOUT if lot.get("bullion") else payout
    proceeds = melt * pay - refiner_ship
    prem, ship_in = auction.get("buyer_premium", 0.0), auction.get("ship_in", 0.0)
    bid = lot["bid"] if bid is None else bid
    cost = bid * (1 + prem) + ship_in
    profit = proceeds - cost
    room = min(proceeds / (1 + MIN_ROI), proceeds - MIN_PROFIT)  # total cost that still clears both rules
    max_bid = max(0.0, (room - ship_in) / (1 + prem))
    return {"melt": round(melt, 2), "proceeds": round(proceeds, 2), "cost": round(cost, 2), "profit": round(profit, 2),
            "roi": round(profit / cost, 4) if cost else 0.0, "max_bid": round(max_bid, 2),
            "passes": profit >= MIN_PROFIT and cost > 0 and profit / cost >= MIN_ROI}


def load(path: str | Path = "scrap_lots.json") -> list[dict]:
    p = Path(path)
    return json.loads(p.read_text(encoding="utf-8")).get("auctions", []) if p.exists() else []


def payload(auctions: list[dict], spot: dict | None = None) -> list[dict]:
    """What the page needs: each auction with per-lot fine grams; spot comes from the live feed or the stored fallback."""
    out = []
    for a in auctions:
        s = spot or {k: v for k, v in a.get("spot_fallback", {}).items() if k in ("gold", "silver")}
        out.append({"id": a["id"], "source": a["source"], "name": a["name"], "url": a.get("url"), "location": a.get("location"),
                    "premium": a.get("buyer_premium", 0.0), "ship_in": a.get("ship_in", 0.0), "as_of": a.get("as_of"), "notes": a.get("notes"), "end": a.get("end"),
                    "spot": {"gold": s.get("gold"), "silver": s.get("silver"), "live": bool(spot),
                             "as_of": None if spot else a.get("spot_fallback", {}).get("as_of")},
                    "lots": [{"lot": l["lot"], "title": l["title"], "bid": l["bid"], "bids": l["bids"], "bullion": bool(l.get("bullion")),
                              "url": (f"https://www.txauction.com/auctions/{a['id'].rsplit('-', 1)[-1]}/lot/{l['lot_id']}-lot-{l['lot']}" if l.get("lot_id") else None),
                              **{f"fine_{m}": g for m, g in fine_grams(l).items()}} for l in a["lots"]]})
    return out


def apply_snapshot(auctions: list[dict], snap: dict) -> list[dict]:
    """Overlay live bids, bid counts and the end time from a TXAuction snapshot onto the hand-entered lots."""
    by_id = {a["auction"]["id"]: a for a in snap.get("auctions", [])}
    out = []
    for a in auctions:
        live = by_id.get(a["id"].rsplit("-", 1)[-1])
        if not live:
            out.append(a)
            continue
        lots_now = {l["lot"]: l for l in live["lots"]}
        lots = []
        for l in a["lots"]:
            cur = lots_now.get(str(l["lot"]))
            if cur is None:
                lots.append(l)
                continue
            lots.append({**l, "bid": cur["bid"] if cur["bid"] is not None else cur.get("start") or l["bid"], "bids": cur["bids"],
                         "lot_id": cur.get("lot_id")})
        out.append({**a, "lots": lots, "end": live["auction"].get("end") or a.get("end"),
                    "as_of": time.strftime("%Y-%m-%d %H:%M", time.localtime(snap.get("fetched_at", 0))) if snap.get("fetched_at") else a.get("as_of")})
    return out
