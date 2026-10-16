"""Brand/model extraction, fluff stripping and fuzzy comp matching for messy seller titles."""
import re
from dataclasses import dataclass

from rapidfuzz import fuzz

from engine.matching import build_query as fallback_query
from engine.matching import is_relevant

BRANDS = {
    # audio / AV
    "marantz", "pioneer", "sansui", "kenwood", "yamaha", "technics", "onkyo", "denon",
    "harman", "jbl", "bose", "klipsch", "mcintosh", "nad", "akai", "teac", "tascam",
    "sony", "panasonic", "jvc", "sharp", "sennheiser", "shure", "akg", "behringer",
    "roland", "korg", "moog", "fender", "gibson", "ibanez", "peavey", "ampeg",
    # cameras
    "leica", "canon", "nikon", "olympus", "pentax", "minolta", "fujifilm", "hasselblad",
    "mamiya", "rollei", "contax", "sigma", "tamron", "zeiss", "polaroid", "kodak",
    # test / lab / industrial
    "fluke", "tektronix", "keysight", "agilent", "hp", "hewlett", "rigol", "siglent",
    "mettler", "ohaus", "eppendorf", "thermo", "olympus", "leeds", "metrohm",
    # network
    "cisco", "juniper", "aruba", "ubiquiti", "netgear", "dell", "arista", "extreme",
    "brocade", "mikrotik", "fortinet", "palo",
    # beverage / commercial
    "bunn", "nuova", "simonelli", "rancilio", "lamarzocco", "cimbali", "breville",
    "hobart", "vitamix", "taylor", "hoshizaki",
    # tools / misc
    "dewalt", "milwaukee", "makita", "snapon", "apple", "samsung", "lg", "bang", "nintendo",
}
# Designer clothing / bags / shoes. Authenticity is NOT verified anywhere in this scanner.
DESIGNER_SINGLE = {
    "gucci", "prada", "chanel", "hermes", "coach", "burberry", "fendi", "dior", "versace", "balenciaga", "celine",
    "givenchy", "valentino", "loewe", "moncler", "longchamp", "ferragamo", "armani", "zegna", "canali", "barbour",
    "louboutin", "manolo", "gabbana", "miumiu", "tumi", "birkenstock", "ugg", "jordan", "yeezy", "converse",
    "vuitton", "kors", "spade", "burch", "veneta", "laurent", "lauren", "marc", "choo", "edmonds", "haan",
    "weitzman", "cucinelli", "varvatos", "hilfiger", "boss",
    # luxury / streetwear / technical outerwear added for the authenticity screen
    "supreme", "bape", "kith", "stussy", "patagonia", "amiri", "lanvin", "goyard", "tods", "berluti", "brioni", "kiton",
    "mulberry", "margiela", "mcqueen", "balmain", "pucci", "missoni", "marni", "bally", "woolrich", "belstaff", "mackage",
    "rimowa", "visvim", "kapital", "arcteryx", "dsquared2",
}
DESIGNER_PHRASES = {
    "louis vuitton": "vuitton", "michael kors": "kors", "kate spade": "spade", "tory burch": "burch",
    "bottega veneta": "veneta", "saint laurent": "laurent", "ralph lauren": "lauren", "brooks brothers": "brooks brothers",
    "marc jacobs": "marc jacobs", "tom ford": "tom ford", "jimmy choo": "choo", "christian louboutin": "louboutin",
    "manolo blahnik": "manolo", "allen edmonds": "edmonds", "cole haan": "haan", "canada goose": "canada goose",
    "dolce & gabbana": "gabbana", "dolce gabbana": "gabbana", "stuart weitzman": "weitzman",
    "ermenegildo zegna": "zegna", "brunello cucinelli": "cucinelli", "john varvatos": "varvatos",
    "tommy hilfiger": "hilfiger", "hugo boss": "boss", "salvatore ferragamo": "ferragamo", "miu miu": "miumiu",
    "air jordan": "jordan", "stone island": "stoneisland", "arc'teryx": "arcteryx", "arc teryx": "arcteryx",
    "a bathing ape": "bape", "bathing ape": "bape", "fear of god": "fear of god", "rick owens": "rick owens",
    "acne studios": "acnestudios", "maison margiela": "margiela", "alexander mcqueen": "mcqueen", "thom browne": "thom browne",
    "the north face": "north face", "golden goose": "golden goose", "gallery dept": "gallery dept",
    "chrome hearts": "chrome hearts", "vivienne westwood": "westwood", "comme des garcons": "comme des garcons",
    "issey miyake": "miyake", "jil sander": "jil sander", "loro piana": "loro piana", "max mara": "max mara",
    "alexander wang": "alexander wang", "stella mccartney": "stella mccartney", "isabel marant": "marant",
    "c.p. company": "cp company", "cp company": "cp company", "palace skateboards": "palace",
    "virgil abloh": "offwhite", "off-white c/o": "offwhite", "off white c/o": "offwhite", "john galliano": "galliano", "dooney & bourke": "dooney", "dooney bourke": "dooney",
}
# Consumer electronics, cameras, instruments, tools, toys/collectibles: brand names the catalog and comp matching recognise.
EXTRA_BRANDS = {
    "lenovo", "asus", "acer", "microsoft", "gopro", "beats", "garmin", "fitbit", "dyson", "kitchenaid", "keurig",
    "cuisinart", "bissell", "irobot", "roomba", "philips", "toshiba", "hitachi", "vizio", "tcl", "hisense", "epson",
    "brother", "xerox", "logitech", "razer", "corsair", "seagate", "wd", "sandisk", "kingston", "google", "kindle",
    "oculus", "sega", "atari", "playstation", "xbox", "lego", "funko", "mattel", "hasbro", "pokemon", "barbie",
    "hotwheels", "starwars", "ryobi", "craftsman", "stanley", "bosch", "hilti", "husqvarna", "stihl", "kobalt",
    "ridgid", "skil", "epiphone", "martin", "squier", "gretsch", "rickenbacker", "prs", "casio", "alesis", "digitech",
    "mxr", "strymon", "electro-harmonix", "vox", "line6", "zoom", "boss", "focusrite", "presonus", "audio-technica",
    "sennheiser", "bowers", "wilkins", "polk", "sonos", "vizio", "insignia", "westinghouse", "magnavox", "zenith",
    "braun", "oral-b", "remington", "conair", "revlon", "shimano", "trek", "specialized", "giant", "cannondale",
}
EXTRA_PHRASES = {"western digital": "wd", "black & decker": "black decker", "black and decker": "black decker",
                 "hot wheels": "hotwheels", "star wars": "starwars", "audio technica": "audio-technica",
                 "bowers & wilkins": "bowers", "electro harmonix": "electro-harmonix", "line 6": "line6"}
BRANDS |= EXTRA_BRANDS | set(EXTRA_PHRASES.values())
DESIGNER_BRANDS = DESIGNER_SINGLE | set(DESIGNER_PHRASES.values()) | {"dooney"}
BRANDS |= DESIGNER_BRANDS
# Apple product lines that appear without the word Apple (GovDeals: "iPad Magic Keyboard ...")
APPLE_LINES = {"ipad": "apple", "iphone": "apple", "ipod": "apple", "macbook": "apple", "airpods": "apple", "imac": "apple"}
BRAND_ALIASES = {**APPLE_LINES, "hewlett": "hp", "packard": "hp", "la marzocco": "lamarzocco", "snap-on": "snapon", **DESIGNER_PHRASES,
                 **EXTRA_PHRASES}

FLUFF = {
    "l@@k", "look", "lqqk", "vintage", "vtg", "untested", "estate", "sale", "rare", "old",
    "works", "work", "working", "wow", "nice", "great", "mint", "clean", "obo", "nr",
    "as-is", "as", "is", "wow!", "beautiful", "amazing", "huge", "cool", "classic",
    "antique", "authentic", "genuine", "original", "lot", "set", "used", "powers", "on",
    "tested", "see", "pics", "photos", "read", "description", "black", "silver", "box",
    "free", "shipping", "the", "a", "an", "and", "with", "for", "in", "of", "to", "w/", "&",
}
# --- Condition flags -------------------------------------------------------------------------
# Matching runs on the title AFTER _neutralize() strips negated phrases ("not locked", "no passcode",
# "tested - not for parts", "iCloud removed") and accessory-scoped phrases ("untested charger").
# Every term sits inside \b...\b so "unlocked", "passcodes"-style substrings and model numbers
# like "MDM-200" never match.
#
# Locked / for-parts: valued at cfg.locked_condition_factor (0.2).
LOCK_RE = re.compile(
    r"\b(?:icloud|activation[- ]lock(?:ed)?|locked|passcode|pass[- ]code|bad[- ]esn|for[- ]parts|parts[- ]only)\b|"
    r"(?<![\w-])mdm\b(?!-?\d)", re.I)
# Untested / as-is / possibly broken: valued at cfg.risky_condition_factor (0.6).
UNTESTED_RE = re.compile(
    r"\b(?:untested|as[- ]is|sold as is|unchecked|spares?|not working|non[- ]?working|broken|repair|salvage|"
    r"damaged|doesn'?t work|no[- ]power)\b", re.I)
# Seller-declared like-new condition: valued at cfg.refurbished_condition_factor (1.10).
REFURB_RE = re.compile(r"\b(?:refurbished|refurb|renewed|mint)\b", re.I)

_FLAG_TERMS = (r"icloud|activation[- ]lock(?:ed)?|locked|passcode|pass[- ]code|mdm|bad[- ]esn|for[- ]parts|"
               r"parts[- ]only|untested|as[- ]is|unchecked|spares?|refurbished|refurb|renewed|mint")
_LOCK_NOUNS = r"icloud|activation[- ]lock|passcode|pass[- ]code|mdm"
_ACCESSORY = (r"(?:charger|cable|cord|adapter|adaptor|case|box|accessor(?:y|ies)|remote|manual|stand|strap|"
              r"bag|cover|dock|stylus|pen|headphones?|earbuds?|stand|tray)s?")
_NEUTRALIZE = (
    # negation before a flag: "not locked", "no passcode", "not iCloud locked", "not for parts"
    re.compile(rf"\b(?:no longer|is not|isn'?t|never|without|not|non|no)[- ]+(?:currently[- ]+)?"
               rf"(?:{_FLAG_TERMS})(?:[- ]+(?:{_FLAG_TERMS}))?\b", re.I),  # chain: "not iCloud locked"
    # lock noun followed by a cleared state: "icloud removed/free/off", "mdm unlocked"
    re.compile(rf"\b(?:{_LOCK_NOUNS})[- ]+(?:free|clean|removed|off|disabled|cleared|unlocked|bypassed)\b", re.I),
    re.compile(rf"\b(?:removed|cleared|disabled|bypassed)[- ]+(?:{_LOCK_NOUNS})\b", re.I),
    # flag scoped to an accessory: "untested charger", "locked case", "charger untested" (not "locked with charger")
    re.compile(rf"\b(?:untested|unchecked|as[- ]is|spares?|locked|for[- ]parts|parts[- ]only)\s+"
               rf"(?:(?!with\b|and\b|w/|plus\b|incl\w*|&)\w+\s+)?{_ACCESSORY}\b", re.I),
    re.compile(rf"\b{_ACCESSORY}\s+(?:is\s+|are\s+)?(?:untested|unchecked|as[- ]is|locked)\b", re.I),
    # "no power adapter/cord" is a missing accessory, not a dead unit
    re.compile(r"\bno[- ]power[- ]+(?:adapter|adaptor|cord|supply|cable|brick|charger)\b", re.I),
)


def _neutralize(title: str) -> str:
    for rx in _NEUTRALIZE:
        title = rx.sub(" ", title)
    return title


# Comp listings that are not the working item itself.
COMP_EXCLUDE_RE = re.compile(
    r"\b(for parts|parts only|not working|broken|repair|case only|box only|manual only|"
    r"empty box|replacement|cover only|remote only)\b", re.I)
_UNIT_RE = re.compile(
    r"^\d+(\.\d+)?(gb|tb|mb|kb|mp|mm|cm|in|inch|lb|lbs|oz|v|w|hz|khz|mhz|ghz|x|pc|pcs|pack|k|"
    r"ft|yd|mah|am|pm|st|nd|rd|th|ct|qt|gal)$")


def _norm(s: str) -> str:
    return re.sub(r"[^a-z0-9]", "", s.lower())


def _words(title: str) -> list[str]:
    t = title.lower().replace("’", "'")
    t = re.sub(r"[\(\)\[\]\{\}!*,;:|~\"]", " ", t)
    return [w.strip(".-_/'") for w in t.split() if w.strip(".-_/'")]


def extract_brand(title: str) -> str | None:
    """The brand named EARLIEST in the title ('Lego Star Wars ...' is Lego, not Star Wars)."""
    low = title.lower()
    best: tuple[int, str] | None = None
    for alias, canon in BRAND_ALIASES.items():
        i = low.find(alias)
        if i >= 0 and (best is None or i < best[0]):
            best = (i, canon)
    for w in _words(title):
        if w in BRANDS:
            i = low.find(w)
            if best is None or 0 <= i < best[0]:
                best = (i, w)
            break
    return best[1] if best else None


_LABELLED_MODEL_RE = re.compile(
    r"\b(?:model|style|sku|ref(?:erence)?|article|mpn|part|serial)\s*(?:no\.?|number|num|#)?\s*[:#]?\s*"
    r"([A-Z0-9][A-Z0-9./-]{3,17})\b", re.I)


def extract_labelled_model(text: str) -> str | None:
    """A model / style / SKU number the description labels as such ('Style #: AB1234', 'Model No. X3X088').

    Must contain a digit and not look like a date or price; returns the first such token, lower-cased."""
    for m in _LABELLED_MODEL_RE.finditer(text or ""):
        tok = m.group(1).strip(".-/").lower()
        if re.search(r"\d", tok) and not re.fullmatch(r"(19|20)\d\d", tok) and len(tok) >= 4 and not _UNIT_RE.match(tok):
            return tok
    return None


def extract_models(title: str) -> list[str]:
    """Candidate model numbers, best first (mixed letters+digits, then pure 3-5 digit numbers)."""
    scored: dict[str, int] = {}
    for w in _words(title):
        if not re.search(r"\d", w) or len(w) < 2 or len(w) > 16:
            continue
        if _UNIT_RE.match(w) or re.fullmatch(r"(19|20)\d\d|\d{2,4}0s", w) or w.startswith("$"):
            continue
        if re.fullmatch(r"\d{1,2}", w) or re.fullmatch(r"\d{2,3}[rlsxw]{1,2}", w):  # bare size / suit size (42R)
            continue
        has_alpha = bool(re.search(r"[a-z]", w))
        if has_alpha and re.fullmatch(r"[a-z0-9]+(-[a-z0-9]+)*", w):
            scored.setdefault(w, 3 if "-" in w or len(w) >= 4 else 2)
        elif re.fullmatch(r"\d{3,5}[a-z]?", w):
            scored.setdefault(w, 1)
        elif re.fullmatch(r"\d{6}", w):  # Gucci-style six-digit style numbers (353122); ranked last
            scored.setdefault(w, 0)
    return [m for m, _ in sorted(scored.items(), key=lambda kv: -kv[1])]


def strip_fluff(title: str) -> str:
    return " ".join(w for w in _words(title) if w not in FLUFF)


def is_untested(title: str) -> bool:
    return bool(UNTESTED_RE.search(_neutralize(title)))


def is_designer(title: str) -> bool:
    """True when the title names a designer clothing/bag/shoe brand (a name match only: NOT an authenticity check)."""
    return extract_brand(title) in DESIGNER_BRANDS


def is_risky(title: str) -> bool:
    """Untested, as-is, locked or for-parts: anything that is not a plain working unit."""
    return is_untested(title) or is_locked(title)


def is_refurbished(title: str) -> bool:
    return bool(REFURB_RE.search(_neutralize(title)))


# ---- Government / police auction telecom boilerplate -------------------------------------------------------------
# Standard "as-is" legal text on GSA / PropertyRoom / GovDeals phone listings: "This item does not come with telecommunications
# service, such as Verizon, T-Mobile, AT&T... necessary to make the item useful. Telecommunications service may not be available
# for various reasons including obsolescence, identification of this item as stolen on a telecommunication carriers records, and
# termination of the relevant telecommunication carriers business." It is a liability shield: it does NOT mean the device is stolen
# or broken. But agencies do not test IMEIs or clear carrier locks, so it means the lock/blacklist status is UNVERIFIED.
TELECOM_DISCLAIMER_RE = re.compile(
    r"does\s+not\s+come\s+with\s+telecommunications?\s+service|"
    r"telecommunications?\s+service\s+(?:may|might|could)\s+not\s+be\s+available|"
    r"necessary\s+to\s+make\s+the\s+item\s+useful|"
    r"stolen\s+on\s+a\s+telecommunications?\s+carriers?|"
    r"identification\s+of\s+this\s+item\s+as\s+stolen|"
    r"telecommunications?\s+carriers?'?s?\s+records|carrier\s+records|"
    r"termination\s+of\s+the\s+relevant\s+telecommunications?\s+carriers?|"
    r"no\s+carrier\s+service|without\s+carrier\s+service", re.I)


def has_telecom_disclaimer(text: str) -> bool:
    """True when the text carries the government telecom boilerplate (or a close variant): unverified_telecom_status."""
    return bool(TELECOM_DISCLAIMER_RE.search(text or ""))


_PHONE_RE = re.compile(
    r"\b(?:iphone|smartphone|smart\s+phone|cell\s?phone|mobile\s+phone|android\s+phone|flip\s+phone|tracfone|straight\s+talk|"
    r"galaxy\s+(?:[sza]|note)\s?\d+|galaxy\s+(?:luna|j\d|core|prime)|pixel\s+\d+|moto\s+[egxz]|oneplus|razr)\b", re.I)
_CELLULAR_RE = re.compile(r"\b(?:cellular|lte|[45]g|verizon|t-?mobile|at&t|sprint|tracfone|boost|cricket|unknown\s+carrier)\b", re.I)
_BIG_SCREEN_RE = re.compile(r"\b(?:ipad|galaxy\s+tab|tablet|kindle\s+fire)\b", re.I)
_WATCH_RE = re.compile(r"\b(?:apple\s+watch|galaxy\s+watch|smart\s?watch)\b", re.I)
_NOT_A_DEVICE_RE = re.compile(
    r"\b(?:case|cases|cover|covers|protector|charger|cable|adapter|holder|stand|mount|skin|sleeve|strap|dock|battery|vr|"
    r"virtual\s+reality|headset|lens|tripod|stylus|screen\s+protector)\b", re.I)
_BUNDLED_RE = re.compile(r"\b(?:with|w/|includes?|including)\b", re.I)


def is_mobile_device(title: str) -> bool:
    """A phone, or a tablet / watch that is CELLULAR (has a carrier radio). Accessories for them are not."""
    if _NOT_A_DEVICE_RE.search(title) and not _BUNDLED_RE.search(title):
        return False
    if _PHONE_RE.search(title):
        return True
    return bool((_BIG_SCREEN_RE.search(title) or _WATCH_RE.search(title)) and _CELLULAR_RE.search(title))


CLEAN_STATUS_RE = re.compile(
    r"\b(?:clean\s+(?:imei|esn)|(?:imei|esn)\s+(?:is\s+)?(?:clean|clear|good)|good\s+(?:imei|esn)|factory\s+unlocked|"
    r"not\s+(?:blacklisted|blocked|reported)|blacklist\s+(?:free|clean|clear)|no\s+blacklist|"
    r"(?:imei|esn)\s+(?:verified|checked)|paid\s+off\s+and\s+(?:clean|unlocked))\b", re.I)
SETUP_SCREEN_RE = re.compile(
    r"\b(?:hello\s+screen|setup\s+screen|set\s?up\s+(?:assistant|screen)|activation\s+screen|restored\s+to\s+factory|"
    r"factory\s+(?:reset|restored|settings)|reset\s+to\s+factory|wiped|erased)\b", re.I)


@dataclass
class TelecomStatus:
    """Telecom risk of a listing. status: 'clean' | 'moderate' | 'unverified', or None when it is not a mobile device."""
    mobile: bool
    status: str | None
    unverified_telecom_status: bool  # the government boilerplate is present
    reasons: list[str]


_NOT_UNLOCKED_RE = re.compile(r"\b(?:not|isn'?t|never|no|un)[\s-]+(?:factory\s+)?unlocked\b|\b(?:carrier|sim|network|activation|icloud|mdm)[\s-]+locked\b", re.I)


def says_unlocked(title: str, full: str) -> bool:
    """The listing says the device is Unlocked and nothing in it says otherwise (no 'not unlocked', no carrier/activation/iCloud lock)."""
    if not re.search(r"\bunlocked\b", full, re.I) or _NOT_UNLOCKED_RE.search(full):
        return False
    return not is_locked(title)


def telecom_status(title: str, text: str = "") -> TelecomStatus:
    """Risk tier for cellular devices (phones, cellular tablets/watches); the three tiers are valued in ValuationEngine.

      clean       the listing explicitly says Clean IMEI / Factory Unlocked / not blacklisted (a seller claim, not a check)
      unverified  the government boilerplate is present, or the listing says untested / unable to test: assume the worst case
                  (bad ESN, blacklisted or carrier-locked) and value as parts
      moderate    nothing alarming but nothing verified either: no IMEI info, or a factory-reset/setup screen with an unverified IMEI

    An IMEI NUMBER printed in a listing is not evidence it is clean, so it does not by itself raise the tier.
    """
    full = f"{title} {text or ''}"
    disclaimer = has_telecom_disclaimer(full)
    if not is_mobile_device(title):
        return TelecomStatus(False, None, disclaimer, [])
    if CLEAN_STATUS_RE.search(full):
        return TelecomStatus(True, "clean", disclaimer, ["listing states a clean IMEI / factory unlocked"])
    if says_unlocked(title, full):  # owner's decision (2026-10-10): a plain 'Unlocked' in the listing is trusted to work on any carrier
        return TelecomStatus(True, "clean", disclaimer, ["listing says Unlocked (trusted as carrier-unlocked; IMEI not checked)"])
    reasons = []
    if disclaimer:
        reasons.append("government telecom disclaimer: lock/blacklist status unverified")
    if is_untested(full) or re.search(r"\b(?:not\s+tested|unable\s+to\s+test)\b", full, re.I):
        reasons.append("listing says untested / unable to test")
    if is_locked(title):
        reasons.append("title signals a lock")
    if reasons:
        return TelecomStatus(True, "unverified", disclaimer, reasons)
    if SETUP_SCREEN_RE.search(full):
        return TelecomStatus(True, "moderate", disclaimer, ["reset / setup screen, IMEI still unverified"])
    return TelecomStatus(True, "moderate", disclaimer, ["no IMEI or lock information"])


def condition(title: str) -> str:
    """'locked' > 'untested' > 'refurbished' > 'working' (the worst signal wins)."""
    if is_locked(title):
        return "locked"
    if is_untested(title):
        return "untested"
    return "refurbished" if is_refurbished(title) else "working"


def is_locked(title: str) -> bool:
    """True when the title signals a lock or for-parts state (iCloud, Activation Lock, passcode, MDM,
    bad ESN, for parts / parts only). Negations like 'no passcode' don't count."""
    return bool(LOCK_RE.search(_neutralize(title)))


def build_search_query(title: str) -> str:
    """'Old Vintage Black Box Receiver Marantz 2230 Untested' -> 'marantz 2230'."""
    brand, models = extract_brand(title), extract_models(title)
    if models:
        parts = ([brand] if brand else []) + [models[0]]
        if not brand:  # no brand: keep a couple of descriptive words for context
            ctx = [w for w in strip_fluff(title).split() if w != models[0] and not re.search(r"\d", w)]
            parts = ctx[:2] + parts
        return " ".join(parts)
    return fallback_query(strip_fluff(title))


def comp_matches(title: str, comp_title: str, model_threshold: int = 90) -> bool:
    """True only when the comp is plausibly the same make/model as the listing."""
    if COMP_EXCLUDE_RE.search(comp_title) and not COMP_EXCLUDE_RE.search(title):
        return False
    models = extract_models(title)
    if not models:
        return is_relevant(fallback_query(strip_fluff(title)), comp_title)
    brand = extract_brand(title)
    comp_low = comp_title.lower()
    if brand and brand not in comp_low and fuzz.partial_ratio(brand, comp_low) < 90:
        return False
    comp_tokens = {_norm(w) for w in _words(comp_title)} | {_norm(comp_low)}
    for m in models[:2]:
        nm = _norm(m)
        if nm in comp_tokens:
            return True
        # Pure-digit models must match exactly (2230 vs 2235 are different units).
        if not nm.isdigit() and len(nm) >= 4:
            if any(fuzz.ratio(nm, t) >= model_threshold for t in comp_tokens if abs(len(t) - len(nm)) <= 2):
                return True
            if nm in _norm(comp_low):
                return True
    return False


# ---- IMEI extraction ---------------------------------------------------------------------------------------------
# Any 15-digit number that passes the Luhn checksum (IMEIs are not limited to the 35/86/99 TAC prefixes). Single spaces or
# dashes between digits are tolerated. A Luhn pass is only a 1-in-10 filter on random digits, so other 15-digit IDs in a
# listing can still pass: the $100 value gate in clients.imei.should_check is what limits wasted spend.
_IMEI_CANDIDATE_RE = re.compile(r"(?<!\d)\d(?:[ -]?\d){14}(?!\d)")


def is_valid_luhn(imei_str: str) -> bool:
    """Luhn checksum: double every second digit from the right (the check digit is not doubled)."""
    if not imei_str or not imei_str.isdigit():
        return False
    total = 0
    for i, ch in enumerate(reversed(imei_str)):
        d = int(ch)
        if i % 2 == 1:
            d *= 2
            if d > 9:
                d -= 9
        total += d
    return total % 10 == 0


def extract_imei(text: str) -> str | None:
    """The first Luhn-valid 15-digit candidate in `text`, or None. A printed IMEI is only a lookup key, not proof it is clean."""
    for m in _IMEI_CANDIDATE_RE.finditer(text or ""):
        digits = re.sub(r"\D", "", m.group())
        if is_valid_luhn(digits):
            return digits
    return None


# ---- Model identity: what a sold price actually applies to ----------------------------------------------------------------
_IDENT_GENERIC = {"camera", "cameras", "speaker", "speakers", "system", "systems", "bluetooth", "wireless", "portable", "film", "slr", "dslr",
                  "digital", "body", "kit", "multimedia", "series", "lot", "set", "with", "and", "the", "for", "black", "white", "silver",
                  "gray", "grey", "blue", "red", "model", "ii", "iii", "mark", "35mm", "player", "headphones", "headphone", "guitar",
                  "amplifier", "amp", "tablet", "phone", "laptop", "computer", "unlocked", "refurbished", "used", "new", "gen", "generation", "pair", "pairs", "bundle", "combo", "tested", "working", "activation", "ios", "locked", "icloud", "mdm", "unknown", "carrier", "wifi", "wi-fi", "cellular", "lte", "ram",
                  "space", "rose", "gold", "midnight", "starlight", "green", "pink", "purple", "graphite", "ssd", "hdd"}
_IDENT_DROP = re.compile(r"^(?:i[3579]-\d{4,5}[a-z]{0,2}|\d+(?:\.\d+)?-?(?:inch|in|watt|w)|i[3579]|\d{4,5}[uhgk]|activation|ios|locked|lock|icloud|mdm|unlocked|only|and|w|x|tested|read|description|as|is|untested|parts|repair|bundle|charger|case|\d+gb/\d+gb|\d+(?:\.\d+)?(?:gb|tb|in|inch|\"|mm)|[45]g|\d+gb/\d+gb)$", re.I)  # sizes: variants/specs, not the model
_HP_LAPTOP = re.compile(r"\b(elitebook|zbook|probook|envy|spectre|pavilion|omen)\b(?:\s+(fury|studio|firefly|power|dragonfly))?(?:\s+(x360))?\s*(\d{3,4})?(?:\s*(g\d{1,2}))?", re.I)
_LENS_MM = re.compile(r"^\d+(?:-\d+)?mm$")


def model_identity(title: str) -> str | None:
    """'brand + the words that name the model' ('canon eos rebel xti', 'bose companion 2'), or None when the title names no brand.

    A sold price is only reused for a listing with the SAME identity: the identity must be contained in the stored one, so
    'canon eos rebel' never prices a 'canon eos rebel 2000'. Lens sizes (18-55mm) are not models; generic words are dropped."""
    title = re.sub("[\u2010-\u2015\u2212]", "-", title or "")  # non-breaking hyphens ('Wi‑Fi') otherwise become part of the model
    brand = extract_brand(title)
    if not brand:
        return None
    if brand.lower() == "hp":
        m = _HP_LAPTOP.search(re.sub(r"\b\d{2}\.\d\b", " ", title))  # screen size (15.6) is not the model
        if m:  # 'EliteBook x360 1030 G8' and '... G4' are different markets: keep series + number + generation
            return "hp " + " ".join(p.lower() for p in m.groups() if p)
    models = [m for m in extract_models(title) if not _LENS_MM.match(m.lower()) and not _IDENT_DROP.match(m)]
    if models:
        return f"{brand} {models[0]}".lower()
    title = re.sub(r"\b\d{1,4}\s*(?:gb|tb)\b", " ", title, flags=re.I)  # '64 GB' is a variant, not part of the model
    title = re.sub(r"\(?#\s*\d+\)?", " ", title)  # '(#2)' / '#3' are lot numbers, not model words
    rest = [w for w in strip_fluff(title).lower().split() if w != brand.lower() and w not in _IDENT_GENERIC and not _LENS_MM.match(w) and not _IDENT_DROP.match(w)]
    return " ".join([brand.lower()] + rest[:3]) if rest else None


# ---- Variants: what splits one model's price into several markets ------------------------------------------------------
_STORAGE_RE = re.compile(r"\b(\d{1,4})\s?(gb|tb)\b", re.I)
_CELL_RE = re.compile(r"\bcellular\b|\blte\b|\b[45]g\b|\bunlocked\b|\bverizon\b|\bat&t\b|\bt-mobile\b|\bsprint\b", re.I)
_WIFI_RE = re.compile(r"\bwi-?fi\b|\bwlan\b", re.I)


def variant_of(title: str) -> tuple[str | None, str | None]:
    """(storage like '128gb' or None, 'cellular' | 'wifi' or None). The largest GB/TB figure is the storage (RAM is smaller).
    'Wi-Fi + 4G' and 'cellular' are cellular; 'Wi-Fi' alone is wifi; no mention is None."""
    title = re.sub("[\u2010-\u2015\u2212]", "-", title or "")  # sold titles use non-breaking hyphens: 'Wi‑Fi'
    sizes = []
    for n, unit in _STORAGE_RE.findall(title):
        gb = int(n) * (1024 if unit.lower() == "tb" else 1)
        if 16 <= gb <= 4096:
            sizes.append(gb)
    storage = None
    if sizes:
        gb = max(sizes)
        storage = f"{gb // 1024}tb" if gb >= 1024 and gb % 1024 == 0 else f"{gb}gb"
    conn = "cellular" if _CELL_RE.search(title) else "wifi" if _WIFI_RE.search(title) else None
    return storage, conn
