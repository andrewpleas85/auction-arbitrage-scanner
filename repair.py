"""Damage classification and repair pricing, shared by every source (Facebook gate and the spec-matched pricer batch).

Classes, from the listing text (worst signal wins):
  parts_only        "for parts", "parts only", "motherboard dead", water damage, bent frame: value is parts value only
  untested_unknown  untested, no power cord, "won't power on", "not working" with no stated cause: working order unknown
  repairable        a SPECIFIC stated defect with a known fix for this item type (cracked screen, bad battery ...)
  cosmetic          scratches / dents / scuffs / wear, nothing functional
  working           no damage stated

Every dollar figure below that is not read from the listing is an ASSUMPTION and is labelled as such where it is shown:
  * repair costs are service-price estimates (parts + shop labour), not quotes
  * the parts value is a share of the working-unit sold price
  * untested items are priced at parts value; a probability-weighted figure is shown for information only

A repair play must clear the normal bar PLUS a risk cushion (the repaired net must be >= MIN_NET + REPAIR_CUSHION and the return on
buy + repair must be >= MIN_RETURN), because repairs fail, run over and take time.
"""
from __future__ import annotations

import re

PARTS_FACTOR = 0.30        # ASSUMPTION: a for-parts/repair listing sells at about this share of a working unit's price
COSMETIC_FACTOR = 0.85     # ASSUMPTION: visible cosmetic wear costs this share of the comp price
P_WORKS = 0.50             # ASSUMPTION: chance an untested unit with an unknown fault works (information only, never used to pass the gate)
REPAIR_DAYS = 7.0          # ASSUMPTION: parts shipping + shop turnaround, added to the time to sell
REPAIR_CUSHION = 25.0      # extra net dollars a repair play must clear on top of the normal bar

# (item type, fix) -> (estimated cost, what it covers). ESTIMATES: parts plus a third-party service price, not quotes.
REPAIR_ESTIMATES = {
    ("phone", "screen"): (110.0, "screen: ~$60 part + ~$50 shop labour"),
    ("phone", "battery"): (60.0, "battery: ~$25 part + ~$35 shop labour"),
    ("phone", "back_glass"): (70.0, "back glass: ~$25 part + ~$45 labour"),
    ("phone", "charging_port"): (70.0, "charging port: ~$20 part + ~$50 labour"),
    ("tablet", "screen"): (130.0, "screen: ~$80 part + ~$50 labour"),
    ("tablet", "battery"): (80.0, "battery: ~$30 part + ~$50 labour"),
    ("laptop", "screen"): (150.0, "screen: ~$90 part + ~$60 labour"),
    ("laptop", "battery"): (90.0, "battery: ~$45 part + ~$45 labour"),
    ("laptop", "keyboard"): (100.0, "keyboard: ~$40 part + ~$60 labour"),
}

_PARTS = re.compile(r"\b(for parts|parts only|parts or repair|salvage|motherboard (?:is )?(?:dead|bad|fried)|water damage|liquid damage|"
                    r"bent frame|no display and|board (?:is )?(?:dead|fried)|blown (?:board|amp|speaker)|dead board)\b", re.I)
_UNKNOWN = re.compile(r"\b(untested|not tested|haven'?t tested|never tested|unknown (?:condition|if)|no power (?:cord|cable|supply)|"
                      r"without (?:a )?power (?:cord|cable)|power[- ]on only|only powers? on|powers? on only|won'?t (?:turn on|power on|boot)|"
                      r"doesn'?t (?:turn on|power on|boot|work)|does not (?:turn on|power on|work)|not working|dead|as[- ]is|sold as is|"
                      r"broken|needs? (?:repair|work|service))\b", re.I)
_COSMETIC = re.compile(r"\b(scratch(?:es|ed)?|scuff(?:s|ed)?|dents?|dings?|cosmetic (?:wear|damage|flaws?)|minor wear|wear and tear|worn|"
                       r"fading|faded|peeling|chips?)\b", re.I)
# stated, specific defects with a known fix. Order matters only for the label.
_DEFECTS = [
    ("screen", re.compile(r"\b(?:cracked|broken|shattered|smashed|damaged)\s+(?:front\s+)?(?:screen|display|lcd|glass)\b|\b(?:screen|display|lcd)\s+(?:is\s+)?(?:cracked|broken|shattered)\b", re.I)),
    ("battery", re.compile(r"\b(?:bad|dead|weak|old|swollen)\s+battery\b|\bbattery\s+(?:is\s+)?(?:dead|bad|weak|swollen|drains|needs (?:replac|to be replac)\w*)\b|"
                           r"\bbattery\s+(?:does(?:n'?t| not)|won'?t)\s+hold\b|\bneeds?\s+(?:a\s+)?new\s+battery\b|\bservice battery\b|\breplace battery\b", re.I)),
    ("back_glass", re.compile(r"\b(?:cracked|broken|shattered)\s+back(?:\s+glass|\s+cover)?\b", re.I)),
    ("charging_port", re.compile(r"\bcharging port\b|\bcharge port\b", re.I)),
    ("keyboard", re.compile(r"\b(?:broken|missing|sticky)\s+keys?\b|\bkeyboard\s+(?:is\s+)?(?:broken|damaged)\b", re.I)),
]
# the listing says the item does NOT power on / has another fault besides the one named: the stated fix is not enough
_ALSO_UNKNOWN = re.compile(r"\b(won'?t (?:turn on|power on|boot|charge)|doesn'?t (?:turn on|power on|boot|charge)|does not (?:turn on|power on)|no power|no display|"
                           r"untested|water damage|liquid damage)\b", re.I)


def classify(title: str, description: str, kind: str) -> dict:
    """{'class', 'defects', 'fix', 'repair_cost', 'repair_source', 'repair_days', 'reason'}. `kind` is the item type from catalog.style_of."""
    text = f"{title} {description or ''}"
    out = {"class": "working", "defects": [], "fix": None, "repair_cost": 0.0, "repair_source": None, "repair_days": 0.0, "reason": None}
    if _PARTS.search(text):
        out.update({"class": "parts_only", "reason": "listing says for parts / parts only / board or water damage"})
        return out
    defects = [name for name, rx in _DEFECTS if rx.search(text)]
    if defects and not _ALSO_UNKNOWN.search(text):
        costs = [(REPAIR_ESTIMATES.get((kind, d)), d) for d in defects]
        if all(c for c, _ in costs):
            out.update({"class": "repairable", "defects": defects, "fix": defects[0], "repair_days": REPAIR_DAYS,
                        "repair_cost": round(sum(c[0] for c, _ in costs), 2),
                        "repair_source": "ESTIMATE (parts + service price, not a quote): " + "; ".join(f"{d} {c[1]}" for c, d in costs),
                        "reason": "stated defect(s) with a known fix: " + ", ".join(defects)})
            return out
        missing = ", ".join(d for c, d in costs if not c)
        out.update({"class": "untested_unknown", "defects": defects, "reason": f"damage stated ({missing}) but no repair estimate exists for a {kind or 'this item type'}"})
        return out
    if _UNKNOWN.search(text) or defects:
        out.update({"class": "untested_unknown", "defects": defects,
                    "reason": "untested, no power cord, not working or an unnamed fault: working order is unknown"})
        return out
    if _COSMETIC.search(text):
        out.update({"class": "cosmetic", "reason": "cosmetic wear only"})
    return out


def paths(working_value: float, cls: dict) -> dict:
    """Values for each way to play the item, from the WORKING-unit sold value (before channel costs):
    as_is_value (what it fetches as it is), repaired_value (working value, for repairable only), prob_weighted (information only)."""
    c = cls["class"]
    if c in ("working", "cosmetic"):
        v = working_value * (COSMETIC_FACTOR if c == "cosmetic" else 1.0)
        return {"as_is_value": v, "repaired_value": None, "prob_weighted": None, "basis": "working" if c == "working" else f"cosmetic wear x {COSMETIC_FACTOR}"}
    parts = working_value * PARTS_FACTOR
    if c == "repairable":
        return {"as_is_value": parts, "repaired_value": working_value, "prob_weighted": None, "basis": f"as-is at parts value ({PARTS_FACTOR:.0%} of working); repaired at working value"}
    pw = P_WORKS * working_value + (1 - P_WORKS) * parts
    return {"as_is_value": parts, "repaired_value": None, "prob_weighted": pw,
            "basis": f"priced at parts value ({PARTS_FACTOR:.0%} of working); probability-weighted {pw:,.0f} assumes a {P_WORKS:.0%} chance it works (information only)"}


def max_buy_repair(revenue: float, repair_cost: float, min_net: float, min_return: float) -> float:
    """Highest asking price where the repaired net clears min_net + cushion and the return on (buy + repair) clears min_return."""
    return max(0.0, min(revenue - repair_cost - (min_net + REPAIR_CUSHION), revenue / (1 + min_return) - repair_cost))
