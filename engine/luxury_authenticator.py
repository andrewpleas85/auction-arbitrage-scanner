"""Text-based authentication screen for designer apparel and leather goods.

Reads the listing title and description only (photos are not analysed), looks for brand-specific authenticity markers and
for counterfeit red flags, and returns a risk tier that the valuation engine turns into a haircut. Nothing here proves an
item is real: a marker is a seller's CLAIM. The tiers only decide how much of a genuine item's value we are willing to
count before a human inspects it.
"""
import difflib
import re
from dataclasses import dataclass, field

BRAND_TIERS = {
    # Tier 1: high-liquidity heavyweight luxury (needs COA / Entrupy / physical verification)
    "TIER_1_LUXURY": ["louis vuitton", "prada", "gucci", "chanel", "goyard", "bottega veneta", "saint laurent",
                      "yves saint laurent", "ysl", "hermes", "dior"],
    # Tier 2: digital / Certilogo verifiable (high secondary demand, code checkable)
    "TIER_2_STREETWEAR": ["moncler", "stone island", "arc'teryx", "arcteryx", "canada goose", "supreme", "off-white"],
    # Tier 3: low margin / heavy retail depreciation
    "LOW_RESALE_MARGIN": ["michael kors", "coach", "kate spade", "tory burch", "mcm", "philipp plein", "balmain"],
}
# alias -> canonical brand name (canonical names are the entries of BRAND_TIERS, normalised)
ALIASES = {"ysl": "saint laurent", "yves saint laurent": "saint laurent", "lv": "louis vuitton", "arcteryx": "arc'teryx",
           "arc teryx": "arc'teryx", "arc’teryx": "arc'teryx", "virgil abloh": "off-white", "hermès": "hermes",
           "mk": "michael kors"}
# the aliases that need a custom regex (colour 'off white', ambiguous short forms)
_PATTERNS = {"off-white": r"off-white(?!\s+(?:leather|pebbled))|virgil\s+abloh|off\s+white\s+c/o",
             "louis vuitton": r"louis\s+vuitton|\blv\b", "lv": r"(?<![a-z0-9])lv(?![a-z0-9])",  # not Nike 'LV8D'/'LV8'
              "mcm": r"\bmcm\b", "ysl": r"\bysl\b",
             "arc'teryx": r"arc['’ ]?teryx", "coach": r"\bcoach\b", "dior": r"\bdior\b"}
# designer brands with no tier mapping: the standard 30% risk haircut applies
GENERIC_DESIGNER = ["burberry", "fendi", "balenciaga", "celine", "givenchy", "valentino", "loewe", "versace", "lanvin",
                    "berluti", "brioni", "kiton", "pucci", "missoni", "marni", "bally", "mcqueen", "margiela", "bape",
                    "kith", "stussy", "patagonia", "amiri", "woolrich", "belstaff", "mackage", "mulberry", "rimowa",
                    "rick owens", "fear of god", "acne studios", "thom browne", "golden goose", "chrome hearts",
                    "ferragamo", "louboutin", "tom ford", "zegna", "cucinelli", "stella mccartney", "alexander wang"]

# Share of a genuine item's value counted. Tier 1/2 follow the verification matrix; everything else is flat.
FACTOR_COUNTERFEIT = 0.05
FACTOR_VERIFIED = 1.00
FACTOR_TIER1_UNVERIFIED = 0.15   # scrap-level: the bid target for an unverified heavyweight is $30-$60, not the comp
FACTOR_TIER1_DOCUMENTED = 0.40   # tier 1, no independent check, but the seller documents 2+ proofs (assumption, not measured)
FACTOR_TIER2_UNVERIFIED = 0.50
FACTOR_LOW_MARGIN = 0.10
FACTOR_GENERIC = 0.70
TIER1_SCRAP_BID = (30.0, 60.0)

_CANON = {b: t for t, names in BRAND_TIERS.items() for b in names}
for _alias, _canon in ALIASES.items():
    _CANON.setdefault(_alias, _CANON.get(_canon))
_CANON["saint laurent"] = "TIER_1_LUXURY"
_CANON["arc'teryx"] = "TIER_2_STREETWEAR"


def _brand_regex(name: str) -> str:
    return _PATTERNS.get(name) or r"(?<![a-z])" + re.escape(name).replace(r"\ ", r"\s+") + r"(?![a-z])"


_DETECT = [(name, re.compile(_brand_regex(name), re.I)) for name in
           list(_CANON) + [g for g in GENERIC_DESIGNER if g not in _CANON]]
# spelling targets for the misspelling check (single words, lowercase)
_SPELL_TARGETS = ["prada", "gucci", "vuitton", "moncler", "arcteryx", "balenciaga", "chanel", "offwhite", "goyard", "hermes"]
_SPELL_OK = {"pradal", "prado", "moncton", "monclair"}  # real words that merely look like a brand

# a brand name alone is not enough ("supreme pizza"): the listing must also name a garment or leather good
ITEM_WORDS = re.compile(
    r"\b(jacket|coat|parka|puffer|down|vest|hoodie|sweatshirt|sweater|tee|t-?shirt|shirt|pants|jeans|shorts|jersey|"
    r"cardigan|fleece|shell|anorak|windbreaker|bag|handbag|tote|purse|wallet|clutch|backpack|belt|scarf|sneakers?|"
    r"shoes?|loafers?|boots?|cap|beanie|hat|crossbody|satchel|duffle)\b", re.I)

RED_FLAG_PATTERNS = {
    "replica": r"\breplicas?\b", "ua": r"\bu\.?a\.?\b", "1:1": r"\b1\s*:\s*1\b|\b1\s*to\s*1\b",
    "mirror quality": r"\bmirror[\s-]*(?:quality|image)\b", "style of": r"\b(?:in the\s+)?style\s+of\b",
    "custom": r"\bcustom(?!\s*fit)\b", "inspired": r"\b(?:inspired|dupe|knock[\s-]?off|fake)\b",
    "unbranded hardware": r"\b(?:plastic|cheap)\s+zip(?:per)?s?\b|\bunbranded\s+(?:hardware|zip(?:per)?s?)\b|\bno\s+hardware\s+(?:stamp|engraving)\b",
}
_FLAG_TEXT = {"replica": "replica", "ua": "UA", "1:1": "1:1", "mirror quality": "mirror quality", "style of": "style of",
              "custom": "custom", "inspired": "inspired/dupe/fake", "unbranded hardware": "cheap or unbranded hardware"}
_COMMON_DENIAL = re.compile(r"\b(?:not|no|never|isn['’]?t|non)[\s-]+(?:a\s+)?(?:replica|fake|ua|custom)\b", re.I)

_LEATHER_ITEMS = re.compile(r"\b(bag|handbag|tote|purse|wallet|clutch|crossbody|satchel|backpack|belt|leather)\b", re.I)

# positive evidence ------------------------------------------------------------------------------
_CERTILOGO = re.compile(r"\bCLG[\s-]?(\d{3})[\s-]?(\d{3})[\s-]?(\d{3})[\s-]?(\d{3})\b|"
                        r"certilogo\D{0,40}?(\d{12})\b|\b(\d{12})\D{0,20}certilogo", re.I)
_VERIFY_LINK = re.compile(r"https?://\S*(?:certilogo|aura\.(?:luxury|blockchain)|auraluxury)\S*", re.I)
_THIRD_PARTY = re.compile(r"\b(entrupy|real\s+authentication|authenticate\s+first|legit\s*app|legitgrails)\b", re.I)
_SELLER_COA = re.compile(r"\b(certificate\s+of\s+authenticity|coa)\b", re.I)  # seller's own paper: weaker than a named service
_RECEIPT = re.compile(r"\b(original\s+receipt|proof\s+of\s+purchase|boutique\s+receipt|invoice)\b", re.I)
_PACKAGING = re.compile(r"\b(dust\s*bag|authenticity\s+card|original\s+box|care\s+booklet|garment\s+bag|hangtag|"
                        r"hang\s+tag|nwt|new\s+with\s+tags)\b", re.I)

_BRAND_MARKERS = {
    "prada": {
        "branded zipper (Lampo/Riri/IPI/Opti/YKK)": r"\b(lampo|riri|ipi|opti|ykk)\b",
        "PRADA-engraved pull": r"\bprada\b.{0,25}\b(?:pull|puller|zipper\s+tab|engrav\w+)\b|\b(?:pull|puller|engrav\w+)\b.{0,25}\bprada\b",
        "enamel plaque": r"\b(?:enamel|metal)\s+(?:plaque|triangle|logo)\b|\bdal\s+1913\b",
        "notched R leg": r"\bnotch(?:ed)?\s+r\b|\br\s+(?:leg\s+)?notch",
        "factory QA tag": r"\b(?:white\s+)?(?:qa|factory|inspection)\s+tag\b|\btag\s*#?\s*\d{1,3}\b",
        "NFC/RFID passport": r"\b(?:nfc|rfid|digital\s+passport|microchip)\b",
    },
    "moncler": {
        "maintenance tag (Comix)": r"\bcomix\b|\bcartoon\s+(?:care\s+)?tag\b|\bmaintenance\s+tag\b",
        "hologram tag": r"\bhologram\b|\boptical\s+tag\b",
        "numeric interior size": r"\b(?:tag|size)\s*(?:#|no\.?)?\s*[0-7]\b(?!\s*(?:xl|x|oz|mm|in))",
    },
    "arc'teryx": {
        "narrow seam tape (~13mm)": r"\b13\s*mm\b|\bnarrow\s+seam\s+tape\b",
        "laminated hem drawcord": r"\b(?:laminated|heat[\s-]laminated)\s+(?:hem|drawcord)\b",
        "micro-stitched wash tag": r"\bmicro[\s-]?stitch\w*\b|\bwash\s+tag\b",
    },
    "supreme": {
        "neck-tag watermark": r"\bwatermark\b",
        "red-stitched inner tag": r"\bred\s+(?:stitch\w*|thread)\b",
        "neck tag spacing": r"\bneck\s+tag\b",
    },
    "off-white": {
        "neck-tag watermark": r"\bwatermark\b",
        "red-stitched inner tag": r"\bred\s+(?:stitch\w*|thread)\b",
        "neck tag spacing": r"\bneck\s+tag\b",
    },
}
# Evidence a seller can point to on a handbag. Claims only: nothing here is checked against a database or a photo.
_BAG_EVIDENCE = {
    "date code": r"\bdate\s*code\b|\b[a-z]{2}\d{4}\b(?=.{0,40}\b(?:louis\s+vuitton|lv)\b)|\bmade\s+in\s+(?:france|italy|spain)\b.{0,30}\b\d{4}\b",
    "serial number / style number": r"\b(?:serial|style|model|sku|item)\s*(?:no\.?|number|#)\s*[:#]?\s*[a-z0-9-]{4,}|\bserial\s+(?:sticker|tag|stamp)\b",
    "authenticity card": r"\bauthenticity\s+card\b|\bcard\s+of\s+authenticity\b",
    "heat / blind stamp": r"\b(?:heat|blind|hot)\s*stamp(?:ed)?\b|\bcreed\b|\bhardware\s+stamp",
    "microchip / hologram": r"\bmicrochip\b|\bhologram\b|\bnfc\b|\bchip\s+tag\b",
    "dust bag / box": r"\bdust\s*bag\b|\boriginal\s+box\b|\bwith\s+box\b",
    "receipt / proof of purchase": r"\b(?:original\s+)?receipt\b|\bproof\s+of\s+purchase\b|\bboutique\s+(?:receipt|invoice)\b",
}
_BEAUTY_PROMO = re.compile(r"\b(?:parfums?|fragrances?|perfumes?|eau\s+de|cosmetics?|makeup|make-up|toiletr(?:y|ies)|beauty|gwp|"
                           r"gift\s+with\s+purchase|travel\s+(?:set|case)|skincare|lipstick|body\s+lotion)\b", re.I)
_OFFWHITE_BRAND = re.compile(r"\bc/o\b|\bvirgil\b|off[\s-]white\s+(?:industrial|arrow|diag|belt|hoodie|tee|sneaker|out\s+of\s+office|odsy)|"
                             r"\barrows?\b.{0,20}off[\s-]white|off[\s-]white\s+(?:x|for)\s+nike|\bthe\s+ten\b", re.I)
_SUPREME_BRAND = re.compile(r"supreme\s+(?:new\s+york|ny|box\s*logo|x\s|c/o|clothing|streetwear)|\bbox\s*logo\b|"
                            r"\bsupreme\b.{0,18}\b(?:hoodie|tee|crewneck|beanie|hat|cap|backpack|shoulder\s+bag|sweatshirt)\b", re.I)
_QR = re.compile(r"\bqr\s*code\b|\bqr\s+tag\b", re.I)
_TIER_LABEL = {"TIER_1_LUXURY": "tier 1 luxury", "TIER_2_STREETWEAR": "tier 2 streetwear", "LOW_RESALE_MARGIN": "low-margin brand"}


def canonical(name: str) -> str:
    return ALIASES.get(name, name)


def detect_brand_and_tier(text: str) -> tuple[str | None, str | None]:
    """Canonical brand and tier for the brand named EARLIEST in the text: ('saint laurent', 'TIER_1_LUXURY') for 'YSL bag'.

    Designer brands outside the tier lists return (brand, None). No match returns (None, None). The text is not required to
    name a garment here: assess() adds that gate so 'supreme pizza' never reaches the valuation.
    """
    best: tuple[int, str] | None = None
    for name, rx in _DETECT:
        m = rx.search(text)
        if m and (best is None or m.start() < best[0]):
            best = (m.start(), name)
    if best is None:
        return None, None
    brand = canonical(best[1])
    return brand, _CANON.get(brand)


@dataclass
class LuxuryAssessment:
    brand: str
    tier: str                      # counterfeit | verified | unverified | low_margin | generic
    factor: float
    brand_tier: str | None = None  # BRAND_TIERS key, None for designer brands outside the lists
    high_risk_counterfeit: bool = False
    certilogo: str | None = None
    qr_code: bool = False
    markers: list[str] = field(default_factory=list)
    red_flags: list[str] = field(default_factory=list)
    concerns: list[str] = field(default_factory=list)   # soft warnings that do not change the tier
    why: list[str] = field(default_factory=list)   # plain-language reasons the piece might not be real
    max_bid: float | None = None   # bid ceiling for an unverified Tier 1 piece (scrap value)
    evidence_level: str = "none"   # 'third_party' (named service / receipt) | 'documented' (2+ seller-stated proofs) | 'claimed' (1) | 'none'
    evidence: list[str] = field(default_factory=list)

    def note(self) -> str:
        if self.high_risk_counterfeit:
            return f"{self.brand}: counterfeit red flag(s): {', '.join(self.red_flags)} -> valued at {self.factor:.0%}"
        bits = [f"{self.brand}: {self.tier}, valued at {self.factor:.0%}"]
        if self.markers:
            bits.append("claims " + ", ".join(self.markers))
        if self.concerns:
            bits.append("check: " + ", ".join(self.concerns))
        return "; ".join(bits)


class LuxuryAuthenticator:
    def detect_brand(self, text: str) -> str | None:
        brand, _ = detect_brand_and_tier(text)
        if brand is None:
            brand = self._misspelled(text.lower())
        if brand is None or not ITEM_WORDS.search(text):
            return None
        if _BEAUTY_PROMO.search(text):
            return None  # fragrance / cosmetic gift-with-purchase pieces carry the name but are not the brand's handbags or garments
        if brand == "off-white" and not _OFFWHITE_BRAND.search(text):
            return None  # 'off-white' the colour (a North Face hoodie in Off-White) is not the label
        if brand == "supreme" and not _SUPREME_BRAND.search(text):
            return None  # 'Supreme Stretch Jeans', 'Supreme pizza'
        return brand

    @staticmethod
    def _misspelled(low: str) -> str | None:
        """Brand-like words that are close to, but not exactly, a brand name ('Pradda', 'Gucchi', 'Balenciaca')."""
        for word in re.findall(r"[a-z']{5,}", low):
            word = word.replace("'", "")
            if word in _SPELL_TARGETS or word in _SPELL_OK:
                continue
            match = difflib.get_close_matches(word, _SPELL_TARGETS, n=1, cutoff=0.84)
            if match:
                return {"vuitton": "louis vuitton", "arcteryx": "arc'teryx", "offwhite": "off-white"}.get(match[0], match[0])
        return None

    @staticmethod
    def find_certilogo(text: str) -> str | None:
        """A 12-digit Certilogo code (plain, 'CLG 123 456 789 012', or next to the word Certilogo) or a Certilogo/Aura link."""
        m = _CERTILOGO.search(text)
        if m:
            return "".join(m.group(1, 2, 3, 4)) if m.group(1) else (m.group(5) or m.group(6))
        link = _VERIFY_LINK.search(text)
        return link.group(0) if link else None

    def assess(self, title: str, text: str = "") -> LuxuryAssessment | None:
        """None when the listing is not a designer garment or leather good.

        Valuation matrix: Tier 1 verified (named authentication service / receipt) 100%, unverified 15% (scrap); Tier 2
        verified (a Certilogo/Aura code in the text) 100%, unverified 50%; low-margin brands 10%; other designer brands 70%;
        any counterfeit red flag 5%. A code is checked for FORMAT only: it is never looked up with the brand.
        """
        full = f"{title} {text}".strip()
        brand = self.detect_brand(full)
        if brand is None:
            return None
        brand_tier = _CANON.get(brand)
        flags = [name for name, pat in RED_FLAG_PATTERNS.items()
                 if re.search(pat, _COMMON_DENIAL.sub(" ", full), re.I)]
        concerns: list[str] = []
        low = full.lower()
        if self._misspelled(low) and detect_brand_and_tier(full)[0] is None:
            flags.append("misspelled brand name")
        if brand == "prada" and _LEATHER_ITEMS.search(full) and re.search(r"made\s+in\s+(?:china|vietnam|cambodia)", low):
            flags.append("origin tag (Made in China) on Prada leather")
        if re.search(r"\bdal1913\b|\bd\.a\.l\.?\s*1913\b", low):
            concerns.append("'DAL 1913' plaque spacing")

        certilogo = self.find_certilogo(full)
        qr = bool(_QR.search(full)) or bool(_VERIFY_LINK.search(full))
        if brand_tier == "TIER_2_STREETWEAR" and not certilogo:
            concerns.append("no Certilogo/Aura code shown")

        markers = [name for name, pat in _BRAND_MARKERS.get(brand, {}).items() if re.search(pat, full, re.I)]
        if brand == "moncler" and not any("size" in m for m in markers) and re.search(r"\bsize\s*(?:xs|s|m|l|xl)\b", low):
            concerns.append("lettered size (genuine tags use 0-7)")
        if brand == "prada" and not any(m in markers for m in ("factory QA tag", "NFC/RFID passport")):
            concerns.append("no factory QA tag / NFC passport mentioned")

        evidence = [name for name, pat in _BAG_EVIDENCE.items() if re.search(pat, full, re.I)]
        if certilogo:
            evidence.append("Certilogo/Aura code")
        third_party = bool(_THIRD_PARTY.search(full) or _RECEIPT.search(full))
        if third_party and not any("receipt" in e for e in evidence) and _THIRD_PARTY.search(full):
            evidence.append("named authentication service (" + _THIRD_PARTY.search(full).group(1).lower() + ")")
        if _SELLER_COA.search(full) and "certificate of authenticity" not in evidence:
            evidence.append("seller's certificate of authenticity")
        documented = len([e for e in evidence if not e.startswith("dust bag")]) >= 2
        if _SELLER_COA.search(full) and not third_party:
            concerns.append("seller's own COA is not independent verification")
        max_bid = None
        if flags:
            tier, factor = "counterfeit", FACTOR_COUNTERFEIT
        elif brand_tier == "LOW_RESALE_MARGIN":
            tier, factor = "low_margin", FACTOR_LOW_MARGIN
        elif brand_tier == "TIER_1_LUXURY":
            if third_party:
                tier, factor = "verified", FACTOR_VERIFIED
            elif documented:  # two or more things the seller can show (date code + card, serial + receipt...): still a claim, so partial credit
                tier, factor = "unverified", FACTOR_TIER1_DOCUMENTED
                max_bid = None
            else:
                tier, factor = "unverified", FACTOR_TIER1_UNVERIFIED
                max_bid = TIER1_SCRAP_BID[1]
        elif brand_tier == "TIER_2_STREETWEAR":
            tier, factor = ("verified", FACTOR_VERIFIED) if (certilogo or third_party) else ("unverified", FACTOR_TIER2_UNVERIFIED)
        else:
            tier, factor = "generic", FACTOR_GENERIC
        why = [f"listing says \"{_FLAG_TEXT.get(f, f)}\"" if f in _FLAG_TEXT else f for f in flags]
        if tier == "unverified" and brand_tier == "TIER_1_LUXURY":
            why.append(f"no independent authentication (Entrupy, receipt) mentioned, and {brand} is among the most counterfeited brands")
        elif tier == "unverified":
            why.append("no Certilogo/Aura code or other proof of authenticity in the listing")
        elif tier == "generic":
            why.append("no proof of authenticity given")
        elif tier == "low_margin":
            why.append("not a fake worry so much as a poor flip: this brand resells far below retail")
        why += [c for c in concerns if c not in why]
        level = ("third_party" if third_party or certilogo else "documented" if documented else "claimed" if evidence else "none")
        return LuxuryAssessment(brand, tier, factor, brand_tier, why=why, high_risk_counterfeit=bool(flags), certilogo=certilogo,
                                qr_code=qr, markers=markers, red_flags=flags, concerns=concerns, max_bid=max_bid,
                                evidence_level=level, evidence=evidence)
