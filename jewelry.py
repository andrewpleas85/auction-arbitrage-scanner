"""Estimate what a diamond-and-gold jewelry lot is worth from its description: gold melt + diamonds at a per-carat price.

Descriptions from the Marshals sales give the karat, the gross weight in grams and an appraiser's estimated total
carat weight (cttw) with clarity and colour ranges. Gross weight includes the stones (a carat is 0.2 g), and
settings, solder and backs are not gold in any useful sense, so the gold share is trimmed. Diamond value per carat
is the weak point: small (0.7-3 mm) I1-I2, G-M stones are worth a fraction of graded melee, so three scenarios
are returned instead of one number. Lots with several items, no karat or no weight return None.
"""
import re

from engine.melt import GOLD_KARAT, OZT

CARAT_G = 0.2
GOLD_SHARE = 0.90          # of (gross - diamonds): solder, backs, hollow parts
# $ per carat of the diamonds in this kind of lot (low = dealer buyback of small I1-I2 melee, high = resold as finished jewelry)
DIAMOND_PER_CT = {"low": 40.0, "mid": 120.0, "high": 350.0}
GOLD_PAYOUT = 0.90         # scrap dealer share of melt for gold with stones (refiner 85%, jeweler up to 95%)

_KARAT = re.compile(r"\b(10|14|18)\s?-?\s?k(?:t|arat)?\b", re.I)
_GRAMS = re.compile(r"(\d+(?:\.\d+)?)\s*grams?\b", re.I)
_CTTW = re.compile(r"(?:estimated|approx\.?|approximately)?\s*(\d+(?:\.\d+)?)\s*(?:cttw|ctw|carats?\s+total|total\s+carat|carats?\b)", re.I)
_CTTW_ALT = re.compile(r"(\d+(?:\.\d+)?)\s*(?:cttw|ctw)", re.I)
_PIECE = re.compile(r"\b(?:NECKLACE|BRACELET|PENDANT|RING|EARRINGS?|CHAIN|WATCH|BROOCH|CHARM)\s*:", re.I)
_SKIP = re.compile(r"\bwatch\b|\bplated\b|gold[- ]?filled|sterling|synthetic|lab[- ]?grown|dental|grill", re.I)


def parse(desc: str) -> dict | None:
    """karat, grams, cttw for a single-piece diamond-and-gold lot, else None."""
    if _SKIP.search(desc):
        return None
    if len(_PIECE.findall(desc)) > 1 or len(_GRAMS.findall(desc)) != 1:
        return None
    ks = {int(k) for k in _KARAT.findall(desc)}
    if len(ks) != 1:
        return None
    m = _CTTW_ALT.search(desc) or _CTTW.search(desc)
    grams = float(_GRAMS.search(desc).group(1))
    return {"karat": ks.pop(), "grams": grams, "cttw": float(m.group(1)) if m else 0.0}


def estimate(desc: str, gold_per_ozt: float) -> dict | None:
    """{'gold': melt-based gold value, 'low'/'mid'/'high': gold + diamonds, 'cttw', 'karat', 'grams'} or None."""
    p = parse(desc)
    if p is None or p["grams"] <= 0:
        return None
    gold_g = max(p["grams"] - CARAT_G * p["cttw"], 0.0) * GOLD_SHARE
    gold = gold_g * GOLD_KARAT[p["karat"]] * gold_per_ozt / OZT * GOLD_PAYOUT
    out = {"gold": round(gold, 2), **{k: round(gold + p["cttw"] * v, 2) for k, v in DIAMOND_PER_CT.items()}, **p}
    return out
