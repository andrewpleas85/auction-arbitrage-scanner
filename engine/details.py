"""Listing-detail assessment: what a listing's own description says beyond its title.

Two jobs:
  * title_missing_parts(): "Mixer Used No Power Cord" -> {"power cord": 8.0}. Cheap, works on any source.
  * parse_description(): reads a PropertyRoom detail page (a bulleted list of "Key: Value" lines such as
    "Apple Account Activation Lock: No", "Testing Results: Powers on, displays", "Power Adapter: None",
    "Hard Drive: Removed") into an Assessment: lock status, whether it was tested/powers on, whether it is
    dead, and which parts are missing with an estimated replacement cost.

Replacement costs are rough defaults (FIX_COSTS in .env overrides them). They are NOT quotes: proprietary
parts cost more, and an untested unit with no cord may simply be dead. Unknown means unknown: when the
description block can't be found, nothing is assumed.
"""
import html
import re
from dataclasses import dataclass, field

from engine.title_cleaner import has_telecom_disclaimer

DEFAULT_FIX_COSTS = {
    "power adapter": 25.0, "power cord": 8.0, "charger": 15.0, "remote": 12.0, "stand": 20.0,
    "battery": 40.0, "storage drive": 35.0, "controller": 30.0, "stylus": 10.0,
}
CHEAP_BATTERY = 5.0  # non-portable devices: AA / coin cells
PORTABLE_RE = re.compile(r"\b(laptop|notebook|chromebook|macbook|phone|iphone|tablet|ipad|camera|camcorder|"
                         r"drone|headphones?|watch|handheld|e-?reader)\b", re.I)

_SYNONYMS = (
    (r"power\s+(?:adapter|adaptor|supply|brick)|ac\s+(?:adapter|adaptor)|adapter|adaptor|power\s+supply", "power adapter"),
    (r"power\s+(?:cord|cable)|ac\s+cord|cord|cable", "power cord"),
    (r"charger|charging\s+(?:cable|dock)", "charger"),
    (r"remote(?:\s+control)?", "remote"),
    (r"hard\s*(?:disk\s*)?drive|hdd|ssd|storage|drive", "storage drive"),
    (r"batter(?:y|ies)", "battery"),
    (r"stand", "stand"),
    (r"controller", "controller"),
    (r"stylus|pen", "stylus"),
)
_PART_WORDS = "|".join(p for p, _ in _SYNONYMS)
_MISSING_PHRASES = (
    re.compile(rf"\b(?:no|missing|without|w/o|lacks?|doesn'?t (?:come with|include|have)|does not (?:come with|include|have))"
               rf"\s+(?:the\s+|a\s+|an\s+)?(?:original\s+|oem\s+|ac\s+)?({_PART_WORDS})\b", re.I),
    re.compile(rf"\b({_PART_WORDS})\s+(?:is\s+|are\s+)?(?:missing|not\s+included)\b", re.I),
)
_NONE_VALUE = re.compile(r"^(?:none|no|missing|removed|not included|n/?a|absent)\b", re.I)
_DEAD = re.compile(r"\b(?:does\s?n[o']t\s+(?:power|turn)\s+on|won'?t\s+(?:power|turn)\s+on|unable\s+to\s+power\s+on|"
                   r"fails?\s+to\s+(?:power|turn)\s+on|did\s+not\s+(?:power|turn)\s+on|not\s+working|"
                   r"non[- ]?working|no\s+power(?!\s+(?:cord|adapter|adaptor|supply))|dead|cracked\s+motherboard)\b", re.I)
_UNTESTED = re.compile(r"\b(?:untested|not\s+tested|could\s+not\s+be\s+tested|unable\s+to\s+test(?!\s+sim))\b", re.I)
_TESTED_OK = re.compile(r"\b(?:powers?\s+on|powered\s+on|turns?\s+on|powers?\s+up|boots?|tested\s+(?:and\s+)?(?:working|good|ok)|"
                        r"tested[, ]+(?:powered|working)|works?|working|functional)\b", re.I)
_ATTR = re.compile(r"^([A-Za-z][A-Za-z0-9 /&()'.-]{1,45}):\s*(.+)$")


def canon_part(text: str) -> str | None:
    for pat, name in _SYNONYMS:
        if re.fullmatch(pat, text.strip(), re.I):
            return name
    return None


def parse_fix_costs(raw: str) -> dict[str, float]:
    """'power adapter=30,remote=15' merged over the defaults; bad entries are ignored."""
    costs = dict(DEFAULT_FIX_COSTS)
    for chunk in (raw or "").split(","):
        if "=" in chunk:
            name, _, val = chunk.partition("=")
            try:
                costs[name.strip().lower()] = float(val)
            except ValueError:
                continue
    return costs


def cost_of(part: str, title: str, costs: dict[str, float]) -> float:
    if part == "battery" and not PORTABLE_RE.search(title):
        return CHEAP_BATTERY
    return costs.get(part, 0.0)


def _missing_in_text(text: str) -> set[str]:
    found = set()
    for rx in _MISSING_PHRASES:
        for m in rx.finditer(text):
            part = canon_part(m.group(1))
            if part:
                found.add(part)
    return found


def title_missing_parts(title: str, costs: dict[str, float] | None = None) -> dict[str, float]:
    """{part: est. replacement cost} for parts the TITLE says are missing."""
    costs = costs or DEFAULT_FIX_COSTS
    return {p: cost_of(p, title, costs) for p in sorted(_missing_in_text(title))}


@dataclass
class Assessment:
    attrs: dict[str, str] = field(default_factory=dict)
    found: bool = False  # the description block was located and parsed
    text: str = ""  # the description block as plain text (for the telecom risk model)
    telecom_disclaimer: bool = False  # government telecom boilerplate present: lock/blacklist status unverified
    activation_lock: bool | None = None
    tested_ok: bool | None = None  # explicit "powers on / works" evidence
    dead: bool = False  # explicitly does not power on / not working
    untested: bool = False
    missing: dict[str, float] = field(default_factory=dict)
    notes: list[str] = field(default_factory=list)

    @property
    def fix_cost(self) -> float:
        return round(sum(self.missing.values()), 2)

    def summary(self) -> str:
        bits = []
        if self.missing:
            bits.append("Needs: " + ", ".join(f"{p} (~${c:,.0f})" for p, c in self.missing.items()))
        if self.dead:
            bits.append("Does NOT power on")
        elif self.tested_ok:
            bits.append("Tested: powers on")
        elif self.untested:
            bits.append("Untested")
        if self.activation_lock is True:
            bits.append("ACTIVATION LOCKED")
        elif self.activation_lock is False:
            bits.append("Activation lock: No")
        bits += self.notes
        return " | ".join(bits)


def html_to_lines(page: str) -> list[str]:
    s = re.sub(r"(?is)<(script|style)[^>]*>.*?</\1>", "", page)
    s = re.sub(r"(?i)</?(?:li|div|p|br|tr|ul|ol|h\d)\b[^>]*>", "\n", s)
    s = html.unescape(re.sub(r"<[^>]+>", "", s)).replace("\xa0", " ")
    return [l for l in (re.sub(r"\s+", " ", ln).strip() for ln in s.split("\n")) if l]


def description_lines(page: str) -> list[str] | None:
    """The seller/PropertyRoom description block, or None if it can't be located."""
    lines = html_to_lines(page)
    start = next((i for i, l in enumerate(lines) if l.startswith("Get a great deal")), None)
    if start is None:
        return None
    end = next((i for i in range(start + 1, len(lines))
                if lines[i].startswith(("Shipping and Returns", "Warranties and Disclaimer", "Terms of Sale"))),
               min(len(lines), start + 60))
    return lines[start + 1:end]


def parse_description(page: str, title: str = "", costs: dict[str, float] | None = None) -> Assessment:
    costs = costs or DEFAULT_FIX_COSTS
    lines = description_lines(page)
    if lines is None:
        return Assessment()
    a = Assessment(found=True, text=" ".join(lines)[:6000])
    a.telecom_disclaimer = has_telecom_disclaimer(a.text)
    for line in lines:
        m = _ATTR.match(line)
        if m:
            a.attrs[m.group(1).strip()] = m.group(2).strip()
    lock = next((v for k, v in a.attrs.items() if re.search(r"activation\s*lock", k, re.I)), None)
    if lock is not None:
        if re.match(r"(?:yes|on|locked)\b", lock, re.I):
            a.activation_lock = True
        elif re.match(r"(?:no|off|unlocked)\b", lock, re.I):
            a.activation_lock = False
    testing = " ".join(v for k, v in a.attrs.items() if re.search(r"test", k, re.I))
    body = " ".join(lines)
    if _DEAD.search(testing) or _DEAD.search(" ".join(l for l in lines if l.lower().startswith(("condition", "testing")))):
        a.dead = True
    elif _UNTESTED.search(testing) or (not testing and _UNTESTED.search(body)):
        a.untested = True
    elif testing and _TESTED_OK.search(testing):
        a.tested_ok = True
    for key, value in a.attrs.items():
        part = canon_part(key)
        if part and _NONE_VALUE.match(value):
            a.missing[part] = cost_of(part, title, costs)
    for part in _missing_in_text(body):
        a.missing.setdefault(part, cost_of(part, title, costs))
    if re.search(r"battery life on this unit has not been tested", body, re.I):
        a.notes.append("battery untested")
    if re.search(r"unable to test\s+sim", body, re.I):
        a.notes.append("SIM untested")
    if re.search(r"without any software", body, re.I):
        a.notes.append("no operating system")
    carrier = next((v for k, v in a.attrs.items() if re.search(r"carrier activation", k, re.I)), None)
    if carrier and re.match(r"unknown", carrier, re.I):
        a.notes.append("carrier status unknown")
    return a
