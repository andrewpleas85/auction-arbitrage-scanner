"""A short list of every running designer piece that could be genuine luxury, so none is missed.

`--watch-luxury` reads the catalog (all sites we crawl), keeps running auctions that the authenticity screen recognises as a
tier-1 luxury or tier-2 streetwear brand (plus other designer brands on request), applies the same Chicago pickup rule as the page,
and prints each with its evidence level, price knowledge and link. Ended or far-away lots are not shown.
"""
import sqlite3
from datetime import datetime, timezone

import catalog
import webpage
from engine.luxury_authenticator import LuxuryAuthenticator

_AUTH = LuxuryAuthenticator()
_ORDER = {"TIER_1_LUXURY": 0, "TIER_2_STREETWEAR": 1, None: 2, "LOW_RESALE_MARGIN": 3}


def watch(conn: sqlite3.Connection, include_generic: bool = False, now: datetime | None = None) -> list[dict]:
    now = now or datetime.now(timezone.utc)
    out = []
    for r in conn.execute("SELECT * FROM catalog WHERE listing_type IN ('auction','daily_auction')").fetchall():
        end = catalog._end(r)
        if end is not None and end <= now:
            continue
        if r["platform"] in webpage.LOCAL_PICKUP_PLATFORMS and not webpage._gsa_near_chicago(r):
            continue
        text = f"{r['title']} {r['description'] or ''}"
        a = _AUTH.assess(r["title"], r["description"] or "")
        if a is None or a.brand_tier is None and not include_generic:
            continue
        if a.brand_tier == "LOW_RESALE_MARGIN" and not include_generic:
            continue
        if r["kind"] in ("accessory", "other") and "bag" not in text.lower():
            continue
        rng = catalog.model_range(conn, r["title"]) or catalog.known_range(conn, r["brand"], r["kind"], r["style"])
        out.append({"platform": r["platform"], "brand": a.brand, "tier": a.brand_tier, "kind": r["kind"], "title": r["title"],
                    "bid": r["current_bid"], "end": end, "url": r["url"], "location": r["location"], "assess": a,
                    "value": rng.typical if rng else None, "basis": rng.basis if rng else None})
    out.sort(key=lambda x: (_ORDER.get(x["tier"], 9), x["end"] or now, -x["bid"]))
    return out


def format_watch(rows: list[dict], now: datetime | None = None) -> str:
    now = now or datetime.now(timezone.utc)
    if not rows:
        return "No running tier-1 / tier-2 designer pieces in the catalog (Chicago pickup rule applied)."
    lines = [f"{len(rows)} running designer pieces (tier 1 luxury first)", ""]
    for r in rows:
        a = r["assess"]
        left = "?"
        if r["end"]:
            s = (r["end"] - now).total_seconds()
            left = f"{int(s // 86400)}d {int(s % 86400 // 3600)}h" if s > 86400 else f"{int(s // 3600)}h {int(s % 3600 // 60)}m"
        val = f"worth ~${r['value']:,.0f} ({r['basis']})" if r["value"] else "no price range"
        ev = {"third_party": "INDEPENDENT proof", "documented": "seller documents", "claimed": "a claim only", "none": "NO PROOF"}[a.evidence_level]
        lines.append(f"[{(r['tier'] or 'designer').replace('TIER_', 'T').replace('_LUXURY', '').replace('_STREETWEAR', '')}] {r['brand'].title()} "
                     f"| {r['platform']} | bid ${r['bid']:,.0f} | ends in {left} | {val} | {ev}")
        lines.append(f"      {r['title'][:90]}")
        if a.evidence:
            lines.append("      evidence: " + ", ".join(a.evidence))
        if a.red_flags:
            lines.append("      RED FLAGS: " + ", ".join(a.red_flags))
        lines.append(f"      valued at {a.factor:.0%} of genuine ({a.tier}) | {r['url']}")
    return "\n".join(lines)
