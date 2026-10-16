"""Valuing multi-unit surplus lots (GSA laptop lots): how many units, which models, what one working unit is worth.

Unit prices are used-market estimates from web research (Oct 2026), US asking/sold prices for working units with an SSD.
Lots from government surplus usually come untested and often with the drives removed, so callers apply a condition haircut
and a per-unit fix cost; this module only answers "what would N working units of these models be worth".
"""
import re

# (pattern, name, low, typical, high) per working unit
LAPTOP_MODELS = (
    (re.compile(r"probook\s*630\s*g8", re.I), "HP ProBook 630 G8", 140, 190, 260),
    (re.compile(r"elitebook\s*(?:x360\s*)?(?:830|1030)|elitebook\s*x360", re.I), "HP EliteBook x360", 255, 330, 400),
    (re.compile(r"thinkpad\s*l13", re.I), "Lenovo ThinkPad L13", 100, 140, 190),
    (re.compile(r"latitude\s*7410", re.I), "Dell Latitude 7410", 130, 200, 300),
    (re.compile(r"(?:latitude\s*|dell\s+)5400\b", re.I), "Dell Latitude 5400", 110, 160, 230),
    (re.compile(r"latitude\s*7590", re.I), "Dell Latitude 7590", 100, 150, 220),
    (re.compile(r"latitude\s*e?5570", re.I), "Dell Latitude E5570", 55, 85, 130),
    (re.compile(r"elitebook\s*840\s*g3", re.I), "HP EliteBook 840 G3", 55, 85, 130),
    (re.compile(r"xps\s*15", re.I), "Dell XPS 15 9570", 230, 330, 460),
)
_WORDS = {w: i for i, w in enumerate(
    "zero one two three four five six seven eight nine ten eleven twelve thirteen fourteen fifteen sixteen seventeen "
    "eighteen nineteen twenty".split())}
_NUM = r"(\d{1,3}|" + "|".join(w for w in _WORDS if w != "zero") + r")"
_PAREN = re.compile(r"\((\d{1,3})\)(?!\s*lots?\b)")
_LOT_OF = re.compile(r"\blots?\s+of\s+" + _NUM + r"\b", re.I)
_BEFORE_LAPTOPS = re.compile(r"\b" + _NUM + r"\s+(?:\w+\s+){0,2}?(?:laptops?|notebooks?|computers?|workstations?)\b", re.I)
_LOT_VERB = re.compile(r"(?:contains?|includes?|consists? of|total of)\s+" + _NUM + r"\b", re.I)
DRIVES_REMOVED = re.compile(r"(?:hard\s*drives?|hdds?|ssds?|drives?|storage)[^.]{0,30}(?:removed|not included|missing)|"
                            r"(?:removed|without|no)\s+(?:hard\s*)?(?:drives?|hdds?|ssds?)", re.I)


def _to_int(tok: str) -> int:
    return int(tok) if tok.isdigit() else _WORDS[tok.lower()]


def lot_quantity(text: str) -> int | None:
    """Units in the lot: '(7) ... and (3) ...' sums; otherwise 'ten laptops' / 'lot contains ten'. None when unknowable."""
    m = _LOT_OF.search(text)
    if m and 0 < _to_int(m.group(1)) < 200:
        return _to_int(m.group(1))
    paren = [int(n) for n in _PAREN.findall(text) if 0 < int(n) < 200]
    if len(paren) > 2 and paren[0] == sum(paren[1:]):  # '(10) laptops: (5) ProBook and (5) EliteBook' states the total first
        return paren[0]
    if paren:
        return sum(paren)
    for rx in (_LOT_VERB, _BEFORE_LAPTOPS):
        m = rx.search(text)
        if m:
            n = _to_int(m.group(1))
            if 0 < n < 200:
                return n
    return None


def lot_models(text: str) -> list[tuple[str, int, int, int]]:
    """(name, low, typ, high) for each known laptop model named in the text."""
    return [(name, lo, ty, hi) for rx, name, lo, ty, hi in LAPTOP_MODELS if rx.search(text)]


def _model_counts(text: str) -> list[tuple[tuple, int | None]]:
    """[(model row, units stated right before its name or None)] for each known model in the text."""
    out = []
    for row in LAPTOP_MODELS:
        m = row[0].search(text)
        if not m:
            continue
        before = text[max(0, m.start() - 40):m.start()]
        c = re.search(r"(?:\((\d{1,3})\)|\b" + _NUM + r")\s+(?:(?:hp|dell|lenovo)\s+)?(?:\w+\s+)?$", before, re.I)
        n = _to_int(c.group(1) or c.group(2)) if c else None
        out.append((row, n))
    return out


def lot_value(text: str) -> dict | None:
    """{'qty', 'models', 'low', 'typ', 'high', 'drives_removed'} for the whole lot, or None if units or models are unknown.

    When every model carries its own count ('(7) ProBook and (3) EliteBook', 'one ProBook and nine EliteBook') each is
    weighted by it; otherwise the models are averaged equally."""
    qty, found = lot_quantity(text), _model_counts(text)
    if not qty or not found:
        return None
    counts = [n for _, n in found]
    if all(counts) and sum(counts) == qty:
        w = [n / qty for n in counts]
    else:
        w = [1 / len(found)] * len(found)
    lo, ty, hi = (sum(wi * row[k] for wi, (row, _) in zip(w, found)) for k in (2, 3, 4))
    return {"qty": qty, "models": [row[1] for row, _ in found], "low": lo * qty, "typ": ty * qty, "high": hi * qty,
            "drives_removed": bool(DRIVES_REMOVED.search(text))}
