"""Precious-metal melt value from a listing title: spot price x weight x purity x payout.

A melt value needs no sold comps. The hard part is not the arithmetic but not being fooled:
  * plated / filled / overlay / vermeil / "tone" / costume pieces contain almost no precious metal;
  * a stated weight usually includes stones, pearls, beads or enamel ("TW" = total weight), so when
    a piece has stones the metal share is cut sharply;
  * a refiner or dealer pays only a share of melt (default 85%; 95% for bullion).
Anything ambiguous returns None (no estimate) rather than a guess.
"""
import re
from dataclasses import dataclass

OZT = 31.1035  # grams per troy ounce
DWT = 1.55517  # grams per pennyweight

GOLD_KARAT = {8: 0.333, 9: 0.375, 10: 0.417, 12: 0.5, 14: 0.585, 18: 0.75, 20: 0.833, 21: 0.875, 22: 0.916, 24: 0.999}
_KARAT_RE = re.compile(r"\b(8|9|10|12|14|18|20|21|22|24)\s*-?\s*(?:k|kt|karat|carat)\b", re.I)
_GOLD_FINENESS = {"417": 10, "585": 14, "750": 18, "916": 22}
_GOLD_FINENESS_RE = re.compile(r"\b(417|585|750|916)\b")
_STERLING_RE = re.compile(r"\b(?:sterling|925|\.925)\b", re.I)
_FINE_SILVER_RE = re.compile(r"(?:\bfine\s+silver\b|\.999\b|\b999\s*(?:fine|silver)\b)", re.I)
_PLATINUM_RE = re.compile(r"\b(?:platinum|plat|pt\s?950|pt\s?900|pt\s?850)\b", re.I)

# Items that are NOT solid precious metal, or that carry little of it.
EXCLUDE_RE = re.compile(
    r"(?:plated?|plating|filled|goldfilled|gold[- ]?tone|silver[- ]?tone|\btone\b|\bgf\b|\bgep\b|\bhge\b|\brgp\b|"
    r"rolled\s+gold|overlay|vermeil|clad|electroplate|costume|faux|simulated|inspired|stainless|brass|copper|"
    r"bronze|pewter|alloy|plique|\bwatch\b|\bwatches\b|movement|\blot of \d+ watches)", re.I)
# Gemstones / non-metal parts: the stated weight probably includes them.
STONE_RE = re.compile(
    r"\b(?:pearl|pearls|bead|beads|beaded|stone|stones|diamond|diamonds|gem|gems|gemstone|cz|opal|jade|coral|onyx|garnet|"
    r"amethyst|topaz|turquoise|ruby|sapphire|emerald|peridot|citrine|carnelian|moonstone|glass|enamel|cameo|"
    r"crystal|quartz|lapis|malachite|tanzanite|aquamarine|zircon|ctw|cttw|ct)\b", re.I)
WEIGHTED_RE = re.compile(r"\b(?:knife|knives|candlestick|candlesticks|candelabra|weighted|base|cruet|holloware)\b", re.I)
TOTAL_WEIGHT_RE = re.compile(r"\b(?:tw|t\.w\.|total\s+weight|gross\s+weight)\b", re.I)
SCRAP_RE = re.compile(r"\b(?:scrap|melt|bullion|ingot|bar|round|coin|krugerrand|eagle|maple|libertad|philharmonic|ozt|troy)\b", re.I)
BULLION_RE = re.compile(r"\b(?:bullion|ingot|bar|round|krugerrand|eagle|maple|libertad|philharmonic|ozt|troy)\b", re.I)
_WEIGHT_RE = re.compile(
    r"(?<![\w.])(\d{1,4}(?:\.\d{1,3})?)\s*(?:-\s*)?(grams?|gms?|grs?|g|dwt|ozt|troy\s*oz(?:ounces?)?|oz)\b", re.I)


@dataclass
class MeltEstimate:
    metal: str  # gold | silver | platinum
    label: str  # "14K gold", "sterling silver"
    grams: float  # stated weight
    metal_fraction: float  # share of the stated weight that is metal (stones etc. removed)
    purity: float
    bullion: bool
    confidence: str  # high | medium
    notes: list[str]

    @property
    def pure_ozt(self) -> float:
        return self.grams * self.metal_fraction * self.purity / OZT

    def melt_value(self, spot_per_ozt: float) -> float:
        return self.pure_ozt * spot_per_ozt

    def summary(self, spot_per_ozt: float | None = None, payout: float | None = None) -> str:
        s = f"{self.label}, {self.grams:g}g stated, {self.metal_fraction:.0%} assumed metal"
        if spot_per_ozt:
            s += f" -> melt ${self.melt_value(spot_per_ozt):,.0f} @ ${spot_per_ozt:,.2f}/oz"
            if payout:
                s += f", {payout:.0%} payout = ${self.melt_value(spot_per_ozt) * payout:,.0f}"
        return s + (" (" + "; ".join(self.notes) + ")" if self.notes else "")


def _grams(title: str) -> tuple[float, bool] | None:
    """(grams, is_troy_oz_unit) for the first usable weight in the title."""
    for m in _WEIGHT_RE.finditer(title):
        n, unit = float(m.group(1)), m.group(2).lower().replace(" ", "")
        if n <= 0:
            continue
        if unit in ("g", "gm", "gms", "gr", "grs", "gram", "grams"):
            return n, False
        if unit == "dwt":
            return n * DWT, False
        if unit in ("ozt",) or unit.startswith("troyoz"):
            return n * OZT, True
        if unit == "oz" and BULLION_RE.search(title):  # a bare "oz" is only trusted on bullion
            return n * OZT, True
    return None


def _metal(title: str) -> tuple[str, str, float] | None:
    low_silver = _STERLING_RE.search(title) or _FINE_SILVER_RE.search(title)
    if low_silver:
        return ("silver", "fine silver", 0.999) if _FINE_SILVER_RE.search(title) else ("silver", "sterling silver", 0.925)
    k = _KARAT_RE.search(title)
    if k:
        n = int(k.group(1))
        return "gold", f"{n}K gold", GOLD_KARAT[n]
    if re.search(r"\bgold\b", title, re.I):
        f = _GOLD_FINENESS_RE.search(title)
        if f:
            n = _GOLD_FINENESS[f.group(1)]
            return "gold", f"{n}K gold", GOLD_KARAT[n]
    if _PLATINUM_RE.search(title):
        return "platinum", "platinum", 0.95
    return None


def estimate_melt(title: str, max_grams: float = 2000) -> MeltEstimate | None:
    """MeltEstimate for a solid precious-metal piece whose title states metal AND weight, else None."""
    if EXCLUDE_RE.search(title):
        return None
    metal = _metal(title)
    weight = _grams(title)
    if not metal or not weight:
        return None
    grams, _ = weight
    if grams > max_grams:
        return None
    name, label, purity = metal
    notes = []
    stones = bool(STONE_RE.search(title))
    total = bool(TOTAL_WEIGHT_RE.search(title))
    bullion = bool(BULLION_RE.search(title)) and not stones
    if bullion:
        fraction, conf = 1.0, "high"
    elif stones:
        heavy = re.search(r"\b(?:bead|beads|beaded|glass|pearl|pearls|enamel|crystal|quartz|turquoise|jade|coral)\b", title, re.I)
        fraction, conf = (0.25 if heavy or total else 0.4), "medium"
        notes.append("stated weight likely includes stones/pearls/beads")
    elif total:
        fraction, conf = 0.9, "medium"
        notes.append("total weight; clasps/solder assumed")
    else:
        fraction, conf = 0.95, "high"
    if name == "silver" and WEIGHTED_RE.search(title):
        fraction, conf = min(fraction, 0.5), "medium"
        notes.append("handles/bases are often weighted or filled")
    if name == "silver" and re.search(r"\b(?:10k|14k|18k)\b", title, re.I):
        fraction, conf = min(fraction, 0.5), "medium"
        notes.append("also mentions gold: only silver counted")
    return MeltEstimate(name, label, grams, fraction, purity, bullion, conf, notes)
