"""Designer bags and clothing from seizure / surplus auctions: damage, authenticity and resale-channel gate.

A designer item is VERIFIED profitable only when ALL of these hold; the first failed check is named on the row.
  1. authenticity     supported (named service / receipt) or plausible (2+ proofs: date code, serial, card ...). Counterfeit red flags,
                      a brand with no proof, or any unsupported claim HARD-FAILS the item: price math can never rescue it.
  2. damage           no value-killing damage (ink on light leather, mould/deep odour, cracked or peeling canvas, sticky lining ...).
                      Repairable damage is priced from a labelled repair range, never from a flat haircut.
  3. condition comps  >= MIN_COMPS stored SOLD sales of the same model in the SAME condition grade as the item (as-is grade for the
                      as-is play, post-repair grade for the repair play). Ungraded comps and clean comps minus a factor are never used.
  4. demand           sold sales in the last 90 days by BRAND tier (Hermes / Chanel / Louis Vuitton hold value and sell fast; the rest need more
                      evidence) plus a brand-scaled time to sell.
  5. channel + costs  best channel for the item with its own fee schedule (eBay, consignment houses, Poshmark, local), buyer's premium,
                      tax, inbound shipping, third-party authentication, and the EXPECTED value after the counterfeit risk.
  6. profit bar       expected net >= MIN_NET and >= MIN_RETURN of all-in cost; a repair play also needs the repair cushion.

Every fee, repair price and probability below is labelled with where it comes from. Most are ESTIMATES because no official or reliable
published schedule could be verified; none is a quote. Change them in one place (this file) and the gate follows.
"""
from __future__ import annotations

import re
import sqlite3
import statistics
from datetime import datetime, timezone

import catalog
import evidence
import repair
from engine.luxury_authenticator import LuxuryAuthenticator
from engine.title_cleaner import model_identity

MIN_COMPS = evidence.VERIFY_MIN_COMPS
MIN_NET = evidence.MIN_NET        # ruling: $100 net
MIN_RETURN = evidence.MIN_RETURN  # ruling: 40% of all-in cost
P_AUTH = {"supported": 0.95, "plausible": 0.75}  # ASSUMPTION: chance the item is genuine given the evidence level (no measured data)
AUTH_COST = {"bag": 30.0, "clothing": 15.0, "hermes": 139.0}
AUTH_COST_SOURCE = ("bags: Real Authentication $30 per item with 24-hour turnaround (published price, relayed by Muse; not re-fetched here). "
                    "ESTIMATES: Hermes $139 (Entrupy Hermes Premium $119-$139) and clothing $15 (Entrupy apparel pay-as-you-go, third-party guide)")
BAG_WORDS = re.compile(r"\b(bag|handbag|tote|purse|wallet|clutch|backpack|satchel|crossbody|hobo|pochette|speedy|neverfull|keepall|birkin|kelly|flap|duffle|duffel|luggage|suitcase|belt)\b", re.I)

# ---- brand demand tiers: how fast and how reliably a brand resells (ASSUMPTION from consignment-market commentary, not measured here)
BRAND_TIER = {"hermes": "top", "chanel": "top", "louis vuitton": "top", "goyard": "top",
              "gucci": "mid", "prada": "mid", "dior": "mid", "saint laurent": "mid", "celine": "mid", "fendi": "mid", "bottega veneta": "mid",
              "burberry": "mid", "balenciaga": "mid", "valentino": "mid", "givenchy": "mid"}
MIN_RECENT_SALES = {"top": 3, "mid": 5, "other": 8}      # sold in the last 90 days among the condition-matched comps

# ---- resale channels: name -> (revenue function of the sale price BEFORE shipping/order fees, ship-out cost, brand tiers accepted, source)
def _flat(rate):
    return lambda v: v * (1 - rate)


def _realreal(v: float) -> float:
    """Keep-rate by sale price. Published points (relayed by Muse): 20% under $100, 70% at $1,500-$4,999, 80% at $7,500+.
    UNVERIFIED interpolation (conservative: the lower published neighbour or 50%): $100-$1,499 -> 50%, $5,000-$7,499 -> 70%. Get the full table."""
    keep = 0.20 if v < 100 else 0.50 if v < 1500 else 0.70 if v < 7500 else 0.80
    return v * keep


def _fashionphile(v: float) -> float:
    """Consignment keeps 70% for the seller, 85% on the portion above $3,000 (published, relayed by Muse)."""
    return min(v, 3000.0) * 0.70 + max(v - 3000.0, 0.0) * 0.85


CHANNELS = {
    "eBay shipped": (_flat(0.15 + catalog.RETURN_RESERVE), 14.0, {"top", "mid", "other"}, "eBay handbag fee 15% (eBay fee page, checked 2026-10-10 in this project) + 3% returns reserve (assumption) + order fee; eBay Authenticity Guarantee is not modelled"),
    "The RealReal (consign)": (_realreal, 0.0, {"top", "mid"}, "keep-rate by price: 20% under $100, 70% at $1,500-$4,999, 80% at $7,500+ (published, relayed by Muse); bands in between are UNVERIFIED conservative interpolations"),
    "Fashionphile (consign)": (_fashionphile, 0.0, {"top", "mid"}, "seller keeps 70%, and 85% of the portion above $3,000 (published, relayed by Muse)"),
    "Vestiaire Collective": (_flat(0.20), 10.0, {"top", "mid"}, "17% + 3% for USD listings (published, changed 2026-10-15, relayed by Muse)"),
    "Poshmark": (_flat(0.20), 0.0, {"top", "mid", "other"}, "20% on sales of $15+, $2.95 flat under (consistent across 2026 third-party sources; Poshmark's own page not retrieved); buyer pays shipping"),
    "local consignment": (_flat(0.40), 0.0, {"top", "mid", "other"}, "ASSUMPTION 40% commission (typical shop split); no source"),
}
CHANNEL_PRICE_NOTE = "every channel is assumed to realize the eBay-sold comp value for the item's condition grade (no per-channel sold data exists here)"

# ---- damage taxonomy: key -> dict(kind, verdict, rx, cost range, source, post-repair grade, days)
_E = "ESTIMATE (not a quote)"
_LBR = "Luxury Bag Rehab published price list (relayed by Muse; not re-fetched here)"
_ZIP = "published zipper replacement range $75-$300 (relayed by Muse)"
_LINING = "ESTIMATE from cost guides (SEO pages, unverified): lining repair $80 / $180 / $350; Purseblog LV re-lining $180-$225 (anecdote)"
_HANDLE = _LBR + ": handle/strap replacement $150"
LIGHT = re.compile(r"\b(white|cream|beige|ivory|light|pale|tan|natural|vachetta|pastel|blush|nude|off[- ]white|ecru|oatmeal)\b", re.I)
SUEDE = re.compile(r"\b(suede|nubuck)\b", re.I)
DAMAGE = [
    # ---- value-killers first: they hard-fail the item whatever the discount
    {"key": "mould_or_deep_odour", "verdict": "killer", "kinds": "both", "label": "mould / mildew / strong smoke or pet odour",
     "rx": r"\b(mold(?:y)?|mould(?:y)?|mildew\w*|musty|strong (?:smoke|smoky|odou?r|smell|perfume)|heavy (?:smoke|smoky|odou?r|smell)|smells? (?:strongly|bad|terrible|like (?:smoke|mildew|urine|cigarette\w*))|pet (?:odou?r|urine)|urine|cigarette (?:smoke|smell|odou?r))\b"},
    {"key": "cracked_or_peeling_coating", "verdict": "killer", "kinds": "both", "label": "cracked / peeling / sticky canvas, coating, patent or lining",
     "rx": r"\b(?:cracked|cracking|peeling|flaking|sticky|tacky|gummy|crumbling|delaminat\w*|dry[- ]?rot\w*)\s+(?:the\s+)?(?:canvas|coating|vinyl|pvc|patent|lining|interior)\b|\b(?:canvas|coating|vinyl|patent|lining|interior)\s+(?:is\s+)?(?:cracked|cracking|peeling|flaking|sticky|tacky|gummy|crumbling)\b|\bdry[- ]?rot\b"},
    {"key": "ink_on_light_leather", "verdict": "killer_if_light", "kinds": "bag", "label": "ink / pen / marker on light leather",
     "rx": r"\b(?:ink|pen|marker|sharpie)\s+(?:stain|mark|marks|spot|spots|bleed\w*|transfer)\b|\bdye\s+transfer\b"},
    {"key": "suede_water_stain", "verdict": "killer_if_suede", "kinds": "bag", "label": "water damage on suede / nubuck",
     "rx": r"\bwater\s+(?:stain|spot|mark|damage)s?\b|\bwatermarks?\b"},
    {"key": "extensive_moth_damage", "verdict": "killer_if_many", "kinds": "clothing", "label": "extensive moth damage",
     "rx": r"\b(?:several|many|multiple|numerous|extensive|lots of|a lot of)\s+(?:moth\s+)?holes?\b|\bmoth[- ]eaten\b|\bextensive moth\b"},
    {"key": "set_in_stain_or_burn", "verdict": "killer", "kinds": "clothing", "label": "set-in stain, bleach damage or burns",
     "rx": r"\b(?:set[- ]in|permanent)\s+stains?\b|\bbleach(?:ed)?\s+(?:spots?|stains?|marks?)\b|\bburn\s+holes?\b|\bcigarette\s+burns?\b|\blarge\s+(?:hole|tear|rip)\b"},
    # ---- repairable at a price
    {"key": "corner_wear", "verdict": "repairable", "structural": False, "kinds": "bag", "label": "corner rub / wear", "cost": (60, 240), "src": _LBR + ": $60 per corner, 1-4 corners (4 assumed when the listing does not say)", "post": "good", "days": 21,
     "rx": r"\bcorner(?:s)?\s+(?:rub\w*|wear|worn|scuff\w*|damage|are (?:worn|rubbed))\b|\b(?:rubbed|worn)\s+corners?\b"},
    {"key": "scuffs", "verdict": "repairable", "structural": False, "kinds": "bag", "label": "scuffs / surface wear", "cost": (175, 250), "src": _LBR + ": basic restoration $175 / extensive restoration $250", "post": "good", "days": 14,
     "rx": r"\bscuff(?:s|ed|ing)?\b|\bscratch(?:es|ed)?\b|\bsurface wear\b|\blight wear\b"},
    {"key": "edge_glazing", "verdict": "repairable", "structural": False, "kinds": "bag", "label": "cracked / peeling edge glazing", "cost": (145, 250), "src": _LBR + ": strap re-edging $145; extensive restoration $250 (nearest published items)", "post": "good", "days": 21,
     "rx": r"\b(?:cracked|peeling|chipped|flaking)\s+(?:edge\s+)?(?:glazing|edge paint|edge coat|piping)\b|\bedge\s+(?:paint|glazing)\s+(?:is\s+)?(?:cracked|peeling|chipped)\b"},
    {"key": "zipper", "verdict": "repairable", "structural": True, "kinds": "both", "label": "broken / stuck zipper", "cost": (75, 300), "src": _ZIP, "post": "good", "days": 14,
     "rx": r"\b(?:broken|stuck|jammed|damaged|missing)\s+(?:zipper|zip)(?:\s+pull)?\b|\b(?:zipper|zip)\s+(?:is\s+)?(?:broken|stuck|jammed|not working|doesn'?t work|damaged|needs? (?:repair|replac\w+))\b"},
    {"key": "hardware_tarnish", "verdict": "repairable", "structural": False, "kinds": "bag", "label": "tarnished / worn hardware (polish)", "cost": (35, 35), "src": _LBR + ": hardware polish $35", "post": "good", "days": 21,
     "rx": r"\b(?:tarnish\w*|oxidi[sz]\w*)\b|\bhardware\s+(?:is\s+)?(?:scratched|tarnished|worn)\b|\bplating\s+(?:worn|wear|flaking|chipped)\b"},
{"key": "hardware_missing", "verdict": "repairable", "structural": True, "kinds": "bag", "label": "missing hardware", "cost": (100, 300), "src": _E + " (sourcing replacement hardware; no published price)", "post": "good", "days": 21,
     "rx": r"\bmissing\s+(?:hardware|clasp|lock|key|feet|studs?)\b|\bhardware\s+(?:is\s+)?missing\b"},
    {"key": "handle", "verdict": "repairable", "structural": True, "kinds": "bag", "label": "handle / strap damage", "cost": (150, 150), "src": _HANDLE, "post": "good", "days": 28,
     "rx": r"\b(?:handle|strap)s?\b.{0,25}\b(?:crack\w*|tear|torn|broken|detached|fraying|frayed|peeling|damaged)\b|\b(?:cracked|torn|broken|frayed|detached|peeling)\s+(?:top\s+)?(?:handle|strap)s?\b"},
    {"key": "ink_on_dark_leather", "verdict": "repairable", "structural": True, "kinds": "bag", "label": "ink / pen mark on dark leather", "cost": (75, 250), "src": _E + " (specialist spot treatment; may not fully lift)", "post": "good", "days": 21,
     "rx": r"\b(?:ink|pen|marker|sharpie)\s+(?:stain|mark|marks|spot|spots|bleed\w*|transfer)\b"},
    {"key": "water_stain", "verdict": "repairable", "structural": False, "kinds": "bag", "label": "water stain on smooth leather", "cost": (150, 300), "src": _LBR + ": water-stain removal from $150 (upper end an ESTIMATE)", "post": "good", "days": 14,
     "rx": r"\bwater\s+(?:stain|spot|mark|damage)s?\b|\bwatermarks?\b"},
    {"key": "lining_tear", "verdict": "repairable", "structural": True, "kinds": "bag", "label": "lining tear", "cost": (80, 350), "src": _LINING, "post": "good", "days": 28,
     "rx": r"\b(?:lining|interior)\b.{0,25}\b(?:tear|torn|ripped|rip|hole|damaged)\b|\b(?:torn|ripped)\s+lining\b"},
{"key": "lining_cleaning", "verdict": "repairable", "structural": False, "kinds": "bag", "label": "stained / dirty lining", "cost": (40, 40), "src": _LBR + ": lining cleaning $40", "post": "good", "days": 28,
     "rx": r"\b(?:lining|interior)\b.{0,25}\b(?:stained|dirty|marked|discolou?red)\b|\b(?:stained|dirty)\s+(?:lining|interior)\b"},
    {"key": "mild_odour", "verdict": "repairable", "structural": False, "kinds": "both", "label": "slight odour / smoke", "cost": (30, 90), "src": _E + " (professional deodorising/cleaning)", "post": "excellent", "days": 7,
     "rx": r"\b(?:slight|faint|light|mild)\s+(?:smoke|smoky|musty|odou?r|smell|perfume)\b|\bsmells? (?:slightly|a little|faintly)\b|\b(?:odou?r|smell)\b"},
    {"key": "shape_loss", "verdict": "repairable", "structural": False, "kinds": "bag", "label": "shape loss / slouching", "cost": (40, 150), "src": _E + " (steaming, stuffing, shaper)", "post": "good", "days": 7,
     "rx": r"\b(?:lost (?:its )?shape|saggy|sagging|slouch\w*|collaps\w*|misshapen|deformed|crushed)\b"},
    {"key": "moth_holes", "verdict": "repairable", "structural": False, "kinds": "clothing", "label": "moth hole(s)", "cost": (30, 100), "src": _E + " (invisible mending, per mend)", "post": "good", "days": 10,
     "rx": r"\bmoth\s+holes?\b|\b(?:small|tiny|a few|one|two)\s+holes?\b|\bmoth damage\b"},
    {"key": "missing_buttons", "verdict": "repairable", "structural": False, "kinds": "clothing", "label": "missing / loose buttons", "cost": (5, 20), "src": _E + " (tailor)", "post": "excellent", "days": 5,
     "rx": r"\bmissing\s+(?:a\s+)?buttons?\b|\bbuttons?\s+(?:missing|fell off|loose)\b"},
    {"key": "seam_split", "verdict": "repairable", "structural": False, "kinds": "clothing", "label": "split seam / hem", "cost": (15, 45), "src": _E + " (tailor)", "post": "excellent", "days": 5,
     "rx": r"\b(?:seam|hem)s?\s+(?:split|splitting|ripped|open|undone|torn|coming apart)\b|\bsplit seams?\b|\bripped seam\b"},
    {"key": "stain", "verdict": "repairable", "structural": False, "kinds": "clothing", "label": "stain", "cost": (20, 60), "src": _E + " (dry cleaning / spot treatment)", "post": "excellent", "days": 7,
     "rx": r"\bstains?\b|\bstained\b"},
    {"key": "tailoring", "verdict": "repairable", "structural": False, "kinds": "clothing", "label": "needs tailoring", "cost": (20, 80), "src": _E + " (tailor)", "post": "excellent", "days": 7,
     "rx": r"\bneeds? (?:to be )?(?:hemm?ed|taken in|altered|tailor\w*)\b|\balterations? needed\b"},
]
_COMPILED = [(d, re.compile(d["rx"], re.I)) for d in DAMAGE]
_DENIAL = re.compile(r"\b(?:no|without|free of|zero)\s+(?:visible\s+)?(?:stains?|odou?rs?|smells?|scuffs?|scratch(?:es)?|tears?|holes?|cracks?|damage|wear|moth)\b", re.I)


def item_kind(title: str) -> str:
    return "bag" if BAG_WORDS.search(title) else "clothing"


def find_damage(title: str, description: str) -> dict:
    """{'items': [...], 'killers': [...], 'repair_low/mid/high', 'days', 'post_grade'}. A killer hard-fails the item."""
    text = _DENIAL.sub(" ", f"{title}. {description or ''}")
    kind = item_kind(title)
    items, killers, seen = [], [], set()
    for d, rx in _COMPILED:
        if d["kinds"] not in ("both", kind) or d["key"] in seen:
            continue
        m = rx.search(text)
        if not m:
            continue
        verdict = d["verdict"]
        if verdict == "killer_if_light":
            if not LIGHT.search(text):
                continue  # dark leather: the repairable twin entry below picks it up
            verdict = "killer"
        elif verdict == "killer_if_suede":
            if not SUEDE.search(text):
                continue
            verdict = "killer"
        elif verdict == "killer_if_many":
            verdict = "killer"
        if d["key"] == "ink_on_dark_leather" and "ink_on_light_leather" in seen:
            continue
        if d["key"] == "water_stain" and "suede_water_stain" in seen:
            continue
        if d["key"] == "hardware_tarnish" and "hardware_missing" in seen:
            continue
        if d["key"] == "mild_odour" and "mould_or_deep_odour" in seen:
            continue
        if d["key"] == "moth_holes" and "extensive_moth_damage" in seen:
            continue
        if d["key"] == "stain" and "set_in_stain_or_burn" in seen:
            continue
        seen.add(d["key"])
        rec = {"key": d["key"], "label": d["label"], "verdict": verdict, "matched": m.group(0)}
        if verdict == "repairable":
            lo, hi = d["cost"]
            rec.update({"cost_low": lo, "cost_high": hi, "cost_mid": round((lo + hi) / 2, 2), "source": d["src"], "post_grade": d["post"], "days": d["days"],
                        "structural": bool(d.get("structural"))})
            items.append(rec)
        else:
            killers.append(rec)
    out = {"items": items, "killers": killers, "kind": kind, "repair_low": sum(i["cost_low"] for i in items), "repair_high": sum(i["cost_high"] for i in items),
           "repair_mid": round(sum(i["cost_mid"] for i in items), 2), "days": max([i["days"] for i in items], default=0)}
    out["structural"] = any(i["structural"] for i in items) or len(items) >= 3
    return out


# ---------------------------------------------------------------- comps: condition-matched
GRADES = ["fair", "good", "very good", "excellent"]  # ascending
# published value bands as a share of CLEAN (new) value (relayed by Muse): used to sanity-check the grade medians, never to price an item
BANDS = {"excellent": (0.85, 0.94), "very good": (0.70, 0.84), "good": (0.50, 0.69), "fair": (0.30, 0.49)}
_NEW = re.compile(r"\b(new with tags|nwt|never used|unused|brand new|new in box|nib)\b", re.I)
_FAIR = re.compile(r"\b(fair|worn|heavy wear|visible wear|scuff\w*|rubbing|stain\w*|flaws?|distressed|damage\w*|peeling|as[- ]is|well used|loved|wear)\b", re.I)
_EXC = re.compile(r"\b(excellent|like new|mint|pristine|immaculate|flawless)\b", re.I)
_VG = re.compile(r"\bvery good\b", re.I)
_GOOD = re.compile(r"\b(good|gently used|minor wear)\b", re.I)
_LUX_COMP_JUNK = re.compile(r"\b(organizer|insert|shaper|base shaper|strap only|dust ?bag only|box only|lock and key|charm|key ?chain|replica|fake|for parts|repair parts|"
                            r"authenticity card only|keychain|cles|key pouch|card holder|coin purse)\b", re.I)


def comp_grade(title: str) -> str | None:
    """'new' | 'fair' | 'excellent' | 'very good' | 'good' | None (ungraded: the title says nothing about condition, so it can match no grade)."""
    if _NEW.search(title):
        return "new"
    if _FAIR.search(title):
        return "fair"
    if _EXC.search(title):
        return "excellent"
    if _VG.search(title):
        return "very good"
    if _GOOD.search(title):
        return "good"
    return None


def item_grade(title: str, description: str, damage: dict) -> tuple[str, bool]:
    """(grade the item is in as it sits, whether the grade was stated). Structural damage (or 3+ items) = fair; other repairable damage = good;
    otherwise the text decides; no statement = 'good'."""
    if damage["items"]:
        return ("fair" if damage["structural"] else "good"), True
    text = f"{title} {description or ''}"
    for g, rx in (("excellent", _EXC), ("very good", _VG), ("fair", _FAIR), ("good", _GOOD)):
        if rx.search(text):
            return g, True
    return "good", False


def repaired_grade(as_is: str) -> str:
    """A repair lifts an item at most ONE grade and never to 'excellent' (ruling)."""
    return GRADES[min(GRADES.index(as_is) + 1, GRADES.index("very good"))]


def graded_comps(conn: sqlite3.Connection, title: str) -> dict:
    """{'excellent': [...], 'good': [...], 'worn': [...], 'ungraded': n, 'ident': model identity}: stored sold sales of the same model by grade."""
    import mcp_auction_pricer as mp
    ident = model_identity(title)
    out = {"ident": ident, "new": [], "excellent": [], "very good": [], "good": [], "fair": [], "ungraded": 0}
    if not ident:
        return out
    for t, p, d in mp._stored_sales(conn, ident):
        if _LUX_COMP_JUNK.search(t):
            continue
        g = comp_grade(t)
        if g is None:
            out["ungraded"] += 1
        else:
            out[g].append((t, p, d))
    return out


# ---------------------------------------------------------------- authenticity
_NAMED_AUTH = re.compile(r"\bauthenticated\s+by\s+([A-Z][\w&' .-]{2,40}?)(?=[.,;]|$|\s+(?:and|with|on)\b)|\b(what goes around comes around|fashionphile|the realreal|rebag|entrupy|real authentication)\b", re.I)
_la = LuxuryAuthenticator()


def authenticity(title: str, description: str) -> dict:
    """verdict: supported | plausible | unsupported | counterfeit_risk. Text only: a claim, never proof. Hard-fails below plausible."""
    la = _la.assess(title, description or "")
    out = {"verdict": "unsupported", "brand": None, "probability": 0.0, "evidence": [], "red_flags": [], "reasons": []}
    if la is None:
        out["reasons"].append("brand not recognised as a designer name in the listing text")
        return out
    out.update({"brand": la.brand, "evidence": la.evidence, "red_flags": la.red_flags})
    named = _NAMED_AUTH.search(f"{title} {description or ''}")
    if la.red_flags:
        out.update({"verdict": "counterfeit_risk", "reasons": [f"counterfeit red flag(s): {', '.join(la.red_flags)}"]})
    elif la.evidence_level == "third_party" or named:
        out.update({"verdict": "supported", "probability": P_AUTH["supported"],
                    "reasons": ["a named authentication service / receipt is mentioned (still the seller's statement: verify before relying on it)"]})
    elif la.evidence_level == "documented":
        out.update({"verdict": "plausible", "probability": P_AUTH["plausible"],
                    "reasons": ["two or more seller-stated proofs (" + ", ".join(la.evidence) + ") but no independent authentication: third-party authentication is priced in"]})
    else:
        out["reasons"].append("no independent authentication and fewer than two stated proofs (date code, serial, card, receipt): authenticity cannot be supported")
    return out


# ---------------------------------------------------------------- the gate
def _best_channel(value: float, tier: str, kind: str) -> tuple[str, dict]:
    res = {}
    for name, (fn, ship, tiers, src) in CHANNELS.items():
        if tier not in tiers or (name.startswith("Fashionphile") and kind != "bag"):
            continue
        order = catalog.EBAY_ORDER_FEE if name == "eBay shipped" else 0.0
        ship_out = ship if kind == "bag" else min(ship, 10.0)
        net_sale = fn(value)
        rev = net_sale - order - ship_out
        res[name] = {"revenue": round(rev, 2), "fee": round(1 - net_sale / value, 4) if value else 0.0, "ship_out": ship_out, "order_fee": order, "source": src}
    best = max(res, key=lambda n: res[n]["revenue"])
    return best, res


def _median(rows) -> float | None:
    return statistics.median([p for _, p, _ in rows]) if rows else None


def _recent(rows, now: datetime, days: int = 90) -> int:
    n = 0
    for _, _, d in rows:
        try:
            dt = datetime.fromisoformat(d)
        except (TypeError, ValueError):
            continue
        dt = dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
        n += (now - dt).days <= days
    return n


def assess(conn: sqlite3.Connection, item: dict, now: datetime | None = None) -> dict:
    """item: title, description, bid, climb (share of value an auction closes at; None for a fixed price), prem, tax, inbound,
    plus identifiers (platform, platform_label, listing_id, url, end). Returns the gate result."""
    now = now or datetime.now(timezone.utc)
    title, desc = item["title"], item.get("description") or ""
    res = {k: item.get(k) for k in ("platform", "platform_label", "listing_id", "url", "end", "bid", "title")}
    res.update({"failures": [], "evidence": {}, "status": "unverified", "failed": None, "needs_check": None})
    fails, ev = res["failures"], res["evidence"]

    # 1. authenticity outranks price
    au = authenticity(title, desc)
    ev["authenticity"] = au
    brand = au["brand"]
    if au["verdict"] in ("counterfeit_risk", "unsupported"):
        fails.append(("authenticity", "; ".join(au["reasons"]) + " (hard fail: no price can fix this)"))
        res["needs_check"] = "needs proof before bidding: date code/serial photo, receipt, or a named authentication service"
        return _finish(res)
    tier = BRAND_TIER.get(brand, "other")
    kind = item_kind(title)
    ev.update({"brand": brand, "brand_tier": tier, "kind": kind})

    # 2. damage
    dm = find_damage(title, desc)
    ev["damage"] = dm
    if dm["killers"]:
        fails.append(("damage (value-killing)", "; ".join(f"{k['label']} (\"{k['matched']}\")" for k in dm["killers"]) + ": hard fail, no discount makes this safe"))
        return _finish(res)

    # 3. condition-matched comps
    gc = graded_comps(conn, title)
    ev["comps"] = {"model": gc["ident"], **{g: len(gc[g]) for g in ("new", "excellent", "very good", "good", "fair")}, "ungraded_not_used": gc["ungraded"]}
    if not gc["ident"]:
        fails.append(("model not identified", "the title names no model we can match to sold sales"))
        return _finish(res)
    damaged = bool(dm["items"])
    as_is_grade, stated = item_grade(title, desc, dm)
    ev["as_is_grade"], ev["as_is_grade_stated"] = as_is_grade, stated
    plays: dict[str, dict] = {}
    need_grades = {"as-is": as_is_grade}
    if damaged:
        need_grades["repair"] = repaired_grade(as_is_grade)  # at most ONE grade up, never excellent
    ev["repaired_grade"] = need_grades.get("repair")
    for play, grade in need_grades.items():
        rows = gc[grade]
        tier_v = evidence.value_tier([p_ for _, p_, _ in rows])
        if len(rows) < MIN_COMPS or tier_v["value"] is None:
            fails.append(("condition-matched comps", f"{len(rows)} stored sold '{grade}'-condition sale(s) of {gc['ident']} for the {play} play, need {MIN_COMPS} "
                          f"(ungraded or other-condition sales are never used, and no flat discount is applied to clean comps)"))
            res["needs_check"] = res["needs_check"] or f"look up eBay sold prices for {gc['ident']} in {grade} condition"
            continue
        plays[play] = {"grade": grade, "comps": len(rows), "value": tier_v["value"], "value_tier": tier_v["tier"], "median": tier_v["median"], "low": tier_v["low"],
                       "high": tier_v["high"], "wide_spread": tier_v["wide"], "net_bar": tier_v["net_bar"], "demand": evidence.demand(rows, now, evidence.active_count(conn, gc["ident"])),
                       "recent_90d": evidence.demand(rows, now)["sold_90d"]}
    new_median = _median(gc["new"]) if len(gc["new"]) >= 3 else None
    ref = new_median
    ev["band_check"] = []
    if ref:  # compare each graded median with its published share-of-clean band: a mismatch means the grade labels or the market are atypical
        for g in ("excellent", "very good", "good", "fair"):
            if len(gc[g]) >= 3:
                ratio = _median(gc[g]) / ref
                lo, hi = BANDS[g]
                ev["band_check"].append({"grade": g, "share_of_clean": round(ratio, 3), "band": [lo, hi], "in_band": lo - 0.05 <= ratio <= hi + 0.05})
    clean_median = _median(gc["excellent"]) if len(gc["excellent"]) >= 3 else None
    if damaged and plays.get("repair") and clean_median:
        ev["post_repair_discount"] = round(1 - plays["repair"]["value"] / clean_median, 3)
        ev["clean_median"] = round(clean_median, 2)
    if not plays:
        return _finish(res)

    # 4-5. channel, costs, expected value
    prem, tax, inbound = item.get("prem") or 0.0, item.get("tax") or 0.0, item.get("inbound") or 0.0
    auth_cost = 0.0 if au["verdict"] == "supported" else AUTH_COST["hermes"] if brand == "hermes" else AUTH_COST[kind]
    p = au["probability"]
    ev["auth"] = {"verdict": au["verdict"], "probability_genuine": p, "third_party_cost": auth_cost, "cost_source": AUTH_COST_SOURCE, "probability_source": "ASSUMPTION, not measured"}
    out_plays = {}
    for play, pl in plays.items():
        best, chans = _best_channel(pl["value"], tier, kind)
        rev = chans[best]["revenue"]
        price = max(item["bid"] or 0.0, (item.get("climb") or 0.0) * pl["value"])  # an auction climbs toward its usual share of resale value
        buy_all_in = price * (1 + prem) * (1 + tax) + inbound
        repair_cost = dm["repair_mid"] if play == "repair" else 0.0
        expected_cost = buy_all_in + auth_cost + p * repair_cost  # repair is only paid if the piece passes authentication
        net_if_genuine = rev - repair_cost - buy_all_in - auth_cost
        net_expected = p * (rev - repair_cost) - buy_all_in - auth_cost
        ret = net_expected / expected_cost if expected_cost else None
        need = pl["net_bar"] + (repair.REPAIR_CUSHION if play == "repair" else 0.0)
        recent_need = MIN_RECENT_SALES[tier]
        out_plays[play] = {**pl, "channel": best, "channel_fee": chans[best]["fee"], "channels": chans, "revenue": rev, "price_assumed": round(price, 2), "buy_all_in": round(buy_all_in, 2),
                           "repair_cost_mid": repair_cost, "expected_all_in_cost": round(expected_cost, 2), "net_if_genuine": round(net_if_genuine, 2),
                           "net_expected": round(net_expected, 2), "return_expected": round(ret, 3) if ret is not None else None, "needs_net": need,
                           "repair_days": dm["days"] if play == "repair" else 0, "recent_needed": recent_need,
                           "passes": net_expected >= need and (ret or 0) >= MIN_RETURN and pl["recent_90d"] >= recent_need,
                           "max_price": round(max(0.0, min(p * (rev - repair_cost) - auth_cost - need,
                                                           (p * (rev - repair_cost) - auth_cost - MIN_RETURN * (auth_cost + p * repair_cost)) / (1 + MIN_RETURN)
                                                           ) - inbound) / ((1 + prem) * (1 + tax)), 2)}
    ev["plays"] = {k: {kk: vv for kk, vv in v.items() if kk != "channels"} for k, v in out_plays.items()}
    ev["channel_sources"] = {n: src for n, (f, sh, t, src) in CHANNELS.items()}
    ev["channel_price_note"] = CHANNEL_PRICE_NOTE
    passing = [k for k, v in out_plays.items() if v["passes"]]
    chosen = max(passing, key=lambda k: out_plays[k]["net_expected"]) if passing else max(out_plays, key=lambda k: out_plays[k]["net_expected"])
    c = out_plays[chosen]
    ev["play"] = chosen if passing else None
    ev["chosen"] = chosen
    ev["best_channel"], ev["net"], ev["return"], ev["max_price"] = c["channel"], c["net_expected"], c["return_expected"], c["max_price"]
    ev["demand"] = {**c["demand"], "brand_tier": tier, "recent_sales_90d": c["recent_90d"], "recent_needed": c["recent_needed"], "repair_days_included": c["repair_days"]}
    if c["recent_90d"] < c["recent_needed"]:
        fails.append(("demand", f"{c['recent_90d']} condition-matched sale(s) in 90 days; a {tier}-tier brand needs {c['recent_needed']}"))
    if not passing and c["net_expected"] < c["needs_net"]:
        fails.append(("profit bar" if chosen == "as-is" else "repair economics",
                      f"expected net ${c['net_expected']:,.0f} (${c['net_if_genuine']:,.0f} if genuine) after authentication and "
                      f"{'repair, ' if chosen == 'repair' else ''}fees; needs ${c['needs_net']:,.0f}"))
    if not passing and (c["return_expected"] or 0) < MIN_RETURN:
        fails.append(("profit bar", f"expected return {(c['return_expected'] or 0):.0%} is under {MIN_RETURN:.0%}"))
    return _finish(res)


def _finish(res: dict) -> dict:
    if res["failures"]:
        res["failed"] = res["failures"][0][0]
    else:
        res["status"] = "verified"
    res["failures"] = [{"check": c, "why": w} for c, w in res["failures"]]
    return res


# ---------------------------------------------------------------- feeding it: designer rows from the catalog
def items_from_designer_rows(conn: sqlite3.Connection, now: datetime) -> list[dict]:
    """Every running priced designer bag/clothing row the page builder already finds (any site), as gate inputs."""
    import webpage
    items = []
    for r in webpage.designer_rows(conn, now=now):
        row = conn.execute("SELECT description FROM catalog WHERE platform=? AND listing_id=?", (r["platform"], str(r["listing_id"]))).fetchone()
        e = r.get("econ") or {}
        items.append({"title": r["title"], "description": (row[0] if row else "") or "", "platform": r["platform"], "platform_label": r["platform_label"],
                      "listing_id": r["listing_id"], "url": r.get("url"), "end": r.get("end"), "bid": r["bid"], "climb": e.get("climb"),
                      "prem": e.get("prem"), "tax": e.get("tax"), "inbound": e.get("inbound")})
    return items


def items_from_txauction(snap: dict, now: datetime) -> list[dict]:
    """TXAuction / US Marshals designer bag and clothing lots from the saved snapshot (running lots only)."""
    import txauction
    from webpage import TX_CLIMB, TX_SHIP_IN
    items = []
    for a in snap.get("auctions", []):
        auc = a["auction"]
        for lot in a["lots"]:
            if lot.get("category", "") not in ("Clothing, Shoes & Accessories",):
                continue
            end = lot.get("end") or auc.get("end")
            if end and datetime.fromisoformat(end.replace("Z", "+00:00")) <= now:
                continue
            desc = lot.get("desc") or ""
            if not (BAG_WORDS.search(desc) or re.search(r"\b(jacket|coat|blazer|dress|suit|sweater)\b", desc, re.I)):
                continue
            items.append({"title": desc[:160], "description": desc, "platform": "txauction", "platform_label": "TXAuction",
                          "listing_id": f"{auc['id']}-{lot['lot']}", "url": txauction.lot_url(auc["id"], lot), "end": end,
                          "bid": lot.get("bid") or 0.0, "climb": TX_CLIMB["bag"], "prem": auc.get("premium") or 0.0, "tax": catalog.SALES_TAX,
                          "inbound": TX_SHIP_IN["bag"]})
    return items


def page_payload(conn: sqlite3.Connection, now: datetime | None = None, snap: dict | None = None) -> dict:
    now = now or datetime.now(timezone.utc)
    items = items_from_designer_rows(conn, now) + (items_from_txauction(snap, now) if snap else [])
    rows = [assess(conn, it, now) for it in items]
    return {"verified": sorted([r for r in rows if r["status"] == "verified"], key=lambda r: -r["evidence"]["net"]),
            "unverified": [r for r in rows if r["status"] != "verified"],
            "bar": {"min_net": MIN_NET, "min_return": MIN_RETURN, "min_comps": MIN_COMPS, "repair_cushion": repair.REPAIR_CUSHION,
                    "recent_needed": MIN_RECENT_SALES, "p_auth": P_AUTH}}
