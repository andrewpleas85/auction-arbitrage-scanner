"""Catalog of everything we scan: crawl all listings, group similar items, attach known price ranges, find room to profit.

`python main.py --catalog` crawls ShopGoodwill (clothing/bags/shoes, electronics, cameras, instruments, collectibles),
PropertyRoom (fashion, electronics, computers, tools) and the GSA feed, keeps listings whose title names a recognised
BRAND, stores them in the `catalog` table, and prints a report grouped by domain, brand and item type.
`--catalog-opps` cross-references every running auction against its known price range.

Grouping is a heuristic (brand + item type + model code or distinctive words): it groups roughly alike items, it does
not identify them. Price ranges come from the knowledge base (engine/knowledge.py): a style-specific range when one
matches, else a brand-level baseline (`<brand> baseline-<type>`). Authenticity and condition are NOT checked.
"""
import asyncio
import logging
import os
import re
import sqlite3
import statistics
import time
from datetime import datetime, timezone

import httpx
from dotenv import load_dotenv

from engine import knowledge as kb
from engine.timeleft import time_left
from engine.title_cleaner import DESIGNER_BRANDS, extract_brand, extract_models
from models import Listing

log = logging.getLogger("arbitrage.catalog")
ALL_DOMAINS = ("fashion", "electronics", "computers", "cameras", "instruments", "collectibles", "tools", "gsa", "municibid", "publicsurplus", "govdeals", "ebay", "illinois_ibid")
SGW_DOMAINS = {"7": "electronics", "170": "cameras", "13": "instruments", "4": "collectibles"}
PR_DOMAINS = {"electronics": "electronics", "computers": "computers", "tools-and-equipment": "tools"}

DISPLAY = {
    "vuitton": "Louis Vuitton", "kors": "Michael Kors", "spade": "Kate Spade", "burch": "Tory Burch",
    "lauren": "Ralph Lauren", "haan": "Cole Haan", "edmonds": "Allen Edmonds", "hilfiger": "Tommy Hilfiger",
    "laurent": "Saint Laurent", "veneta": "Bottega Veneta", "choo": "Jimmy Choo", "weitzman": "Stuart Weitzman",
    "gabbana": "Dolce & Gabbana", "dooney": "Dooney & Bourke", "manolo": "Manolo Blahnik", "marc": "Marc Jacobs",
    "boss": "Hugo Boss", "stoneisland": "Stone Island", "arcteryx": "Arc'teryx", "bape": "BAPE", "offwhite": "Off-White",
    "mcqueen": "Alexander McQueen", "margiela": "Maison Margiela", "westwood": "Vivienne Westwood",
    "north face": "The North Face", "acnestudios": "Acne Studios", "miyake": "Issey Miyake", "marant": "Isabel Marant",
    "tods": "Tod's", "galliano": "John Galliano", "cp company": "C.P. Company", "louboutin": "Christian Louboutin", "zegna": "Zegna", "cucinelli": "Brunello Cucinelli",
    "varvatos": "John Varvatos", "miumiu": "Miu Miu", "jordan": "Jordan", "ugg": "UGG", "tumi": "Tumi",
    "wd": "Western Digital", "hotwheels": "Hot Wheels", "starwars": "Star Wars", "gopro": "GoPro", "irobot": "iRobot",
    "kitchenaid": "KitchenAid", "black decker": "Black & Decker", "audio-technica": "Audio-Technica",
}
_UPPER = {"hp", "lg", "jbl", "jvc", "akg", "nad", "tcl", "prs", "mxr", "dewalt", "ibm", "msi", "asus"}


def display_brand(code: str | None) -> str | None:
    if not code:
        return None
    if code in DISPLAY:
        return DISPLAY[code]
    return code.upper() if code in _UPPER or len(code) <= 2 else code.title()


# resale potential of the brand name itself: A high, B medium, C low
TIER = {b: "A" for b in ("chanel hermes vuitton gucci prada dior fendi balenciaga celine laurent veneta louboutin "
                         "versace burberry valentino givenchy loewe moncler ferragamo miumiu zegna canali cucinelli "
                         "gabbana choo manolo weitzman armani supreme arcteryx bape amiri lanvin goyard berluti brioni kiton mcqueen "
                         "margiela balmain pucci missoni marni bally rimowa westwood miyake offwhite "
                         "apple sony nikon canon leica hasselblad nintendo bose "
                         "fluke tektronix keysight mcintosh marantz technics gibson fender moog korg roland dewalt "
                         "milwaukee makita samsung dell lenovo garmin dyson").split()}
TIER.update({b: "B" for b in ("kith stussy patagonia visvim kapital palace woolrich belstaff mackage mulberry "
                              "coach burch marc dooney tumi kors spade barbour jordan yeezy edmonds haan varvatos "
                              "longchamp hp microsoft lg jbl beats pioneer yamaha kenwood denon onkyo jvc panasonic "
                              "olympus pentax minolta fujifilm gopro logitech razer asus acer epson brother ryobi "
                              "craftsman stanley bosch lego funko pokemon hasbro mattel").split()})


TIER.update({"stoneisland": "A", "loro piana": "A", "north face": "B", "acnestudios": "A", "golden goose": "A", "rick owens": "A",
             "fear of god": "A", "chrome hearts": "A", "thom browne": "A", "comme des garcons": "A", "jil sander": "A",
             "max mara": "A", "alexander wang": "B", "stella mccartney": "A", "gallery dept": "B", "galliano": "A",
             "marant": "A", "cp company": "B"})


# Keyword searches run across ALL ShopGoodwill categories on top of the fashion category crawl: designer pieces are often filed
# under the wrong category. Ambiguous brand words (coach, boss, marc) are left to the category crawl.
LUXURY_SEARCH_TERMS = (
    "gucci", "prada", "chanel", "hermes", "louis vuitton", "dior", "fendi", "balenciaga", "celine", "givenchy", "valentino",
    "loewe", "moncler", "bottega veneta", "saint laurent", "burberry", "versace", "goyard", "lanvin", "berluti", "brioni",
    "kiton", "balmain", "pucci", "missoni", "marni", "bally", "mcqueen", "margiela", "supreme", "bape", "kith", "stussy",
    "patagonia", "amiri", "woolrich", "belstaff", "mackage", "mulberry", "rimowa", "visvim", "kapital", "stone island",
    "arc'teryx", "off-white", "virgil abloh", "fear of god", "rick owens", "acne studios", "thom browne", "the north face",
    "golden goose", "gallery dept", "chrome hearts", "vivienne westwood", "comme des garcons", "issey miyake", "jil sander",
    "loro piana", "max mara", "canada goose", "miu miu", "tom ford", "zegna", "cucinelli", "ferragamo", "louboutin",
    "stella mccartney", "isabel marant", "alexander wang", "cp company",
)


def tier_of(code: str | None) -> str:
    return TIER.get(code or "", "C")


# ---- item types per domain (first word in the title that matches wins)
KINDS = {  # fashion
    "bag": "tote satchel crossbody shoulder hobo clutch backpack bucket messenger duffle duffel handbag purse bag "
           "pouch bagpack".split(),
    "wallet": "wallet wristlet cardholder card cardcase billfold coin".split(),
    "shoes": "boots boot booties bootie pumps pump heels sandals sandal sneakers sneaker loafers loafer flats oxford "
             "oxfords slippers clogs wedges mules slides shoes shoe trainers".split(),
    "clothing": "suit blazer jacket coat dress trousers pants shirt sweater skirt vest parka cardigan jeans polo tee "
                "sportcoat topcoat overcoat tuxedo shorts hoodie pullover fleece sweatshirt jumpsuit romper windbreaker anorak "
                "puffer tunic blouse leggings joggers trench".split(),
    "accessory": "belt scarf tie gloves hat cap keychain sunglasses bracelet wrap shawl".split(),
}
ELECTRONICS = {
    "laptop": "laptop notebook chromebook macbook thinkpad latitude elitebook probook inspiron".split(),
    "desktop": "desktop optiplex imac tower all-in-one mini-pc".split(),
    "phone": "phone iphone smartphone galaxy pixel".split(),
    "tablet": "tablet ipad kindle tab".split(),
    "vr": "vr oculus quest".split(),
    "console": "console playstation xbox switch wii ps4 ps5 ps3 3ds gameboy gamecube".split(),
    "game": "game games".split(),
    "gamepad": "controller pad gamepad joycon joy-con".split(),
    "camera": "camera dslr camcorder gopro mirrorless polaroid".split(),
    "lens": "lens lenses".split(),
    "headphones": "headphones headphone earbuds airpods headset earphones".split(),
    "speaker": "speaker speakers soundbar subwoofer boombox".split(),
    "tv": "tv television".split(),
    "monitor": "monitor display".split(),
    "printer": "printer scanner".split(),
    "network": "router modem ethernet".split(),
    "drone": "drone".split(),
    "watch": "smartwatch fitbit watch".split(),
    "ipod": "ipod".split(),
    # accessories sort first when they appear before the product ("Carrying Case for Nintendo Switch")
    "accessory": "case cases cover covers protector protectors skin skins sleeve sleeves charger chargers adapter "
                 "adapters adaptor cable cables cord cords strap straps stand stands mount mounts holder holders dock "
                 "stylus remote battery grip accessories accessory compatible replacement folio".split(),
    "audio": "receiver amplifier amp preamp turntable cassette stereo".split(),
    "projector": "projector".split(),
    "storage": "drive hdd ssd".split(),
    "calculator": "calculator".split(),
    "microphone": "microphone mic".split(),
    "appliance": "vacuum blender mixer espresso keurig kettle toaster fryer".split(),
}
INSTRUMENTS = {
    "guitar": "guitar telecaster stratocaster".split(), "bass": "bass".split(), "drums": "drum drums cymbal".split(),
    "keys": "piano keyboard synth synthesizer organ".split(), "orchestral": "violin viola cello trumpet sax saxophone "
    "flute clarinet trombone".split(), "folk": "ukulele banjo mandolin harmonica".split(),
    "pedal": "pedal overdrive distortion delay reverb".split(), "amp": "amp amplifier combo".split(),
}
TOOLS = {
    "drill": "drill driver impact".split(), "saw": "saw jigsaw".split(), "sander": "sander grinder polisher".split(),
    "hand": "wrench ratchet socket pliers hammer".split(), "compressor": "compressor nailer".split(),
    "welder": "welder".split(), "meter": "multimeter oscilloscope analyzer".split(),
    "outdoor": "mower trimmer blower chainsaw".split(),
}
COLLECTIBLES = {
    "cards": "card cards pokemon".split(), "figure": "figure figurine doll statue".split(),
    "toy": "toy lego funko playset".split(), "coin": "coin coins".split(), "comic": "comic comics".split(),
}
DOMAIN_KINDS = {"fashion": KINDS, "electronics": ELECTRONICS, "computers": ELECTRONICS, "cameras": ELECTRONICS,
                "instruments": {**INSTRUMENTS}, "tools": {**TOOLS, **ELECTRONICS}, "collectibles": COLLECTIBLES,
                "gsa": {**ELECTRONICS, **TOOLS}}
_LOOKUPS = {d: {w: k for k, ws in m.items() for w in ws} for d, m in DOMAIN_KINDS.items()}
_CONSOLE_CUE = re.compile(r"\b(console|system|bundle|oled|lite|handheld|dock|joy-?con|slim|disc|digital edition|"
                          r"\d+\s?gb|ps[345]|playstation \d|xbox (?:one|series)|wii u?)\b", re.I)
PREFERRED = {"instruments": ("amp", "pedal"),
             # a 'Galaxy Tab' is a tablet, a 'Switch ... Game' is a game, a 'Gear VR' is VR: not the phone/console named first
             "electronics": ("gamepad", "game", "tablet", "vr", "watch"), "cameras": ("lens",), "computers": ("tablet",)}
HEAVY = {"tv", "monitor", "desktop", "printer", "audio", "speaker", "appliance", "compressor", "welder", "outdoor",
         "drums", "amp", "keys", "projector"}

SUBTYPES = set("""crossbody tote satchel shoulder hobo clutch backpack bucket messenger duffle duffel wristlet cardholder
flip-flop flip-flops sandals sandal boots booties sneakers loafers pumps heels flats oxford slippers clogs wedges mules
suit blazer jacket coat dress parka vest cardigan belt scarf""".split())
_NOISE = set("""black white red blue green brown tan beige cream gray grey pink purple orange yellow gold silver navy
ivory taupe burgundy khaki camel multi multicolor color leather canvas suede patent nylon wool cotton silk cashmere
linen denim faux fur pebbled pebble saffiano signature monogram womens women's mens men's ladies size sz small medium
large xl xs authentic genuine original new nwt vintage rare retro with and the for in of by from w coa certificate
dust box tags tag hardware gold-tone silver-tone strap chain zip zipper closure lot set pair pre owned used
tone dark light trim tassel adj adjustable print printed striped plaid floral heels heel knee-high high low top mid
matching combo fabric rubber metallic sparkle glitter
air nike york new logo women woman men girls boys kids kid retro og hi lows gym athletic basketball colored unisex
classic casual dress platform pointed round toe peep open bag bags
wireless bluetooth digital portable usb hdmi tested working untested power cord cable adapter charger remote battery
included accessories only parts repair powers turns works fully functional factory reset cleared wiped locked unlocked
tested/working complete pack bundle kit system series version edition model type generation gen
unknown carrier tracfone unlocked locked verizon tmobile t-mobile att at&t sprint cricket straight talk""".split())
_SIZE = re.compile(r"^(?:\d{1,3}[rlsxw]{0,2}|\d+(?:\.\d+)?[mwb]|sz\d*|size\d*|[xsml]{1,3})$", re.I)


def style_of(title: str, domain: str = "fashion") -> tuple[str | None, str, str]:
    """(brand display name, item type, style) for a title in the given domain."""
    from engine.luxury_authenticator import _BEAUTY_PROMO
    if _BEAUTY_PROMO.search(title):
        return None, "other", ""  # fragrance / cosmetic gift-with-purchase pieces are not the brand's handbags or garments
    code = extract_brand(title)
    brand = display_brand(code)
    words = re.findall(r"[A-Za-z0-9][A-Za-z0-9'&\-.]*", title)
    lookup = _LOOKUPS.get(domain, _LOOKUPS["electronics"])
    lows = [w.lower() for w in words]
    kinds = [lookup[w] for w in lows if w in lookup]
    if domain != "fashion" and "accessory" in kinds:
        # an accessory word anywhere makes it an accessory ('iPhone 11 Case'), unless it is bundled: 'iPhone 11 with Case'
        i = next(i for i, w in enumerate(lows) if lookup.get(w) == "accessory")
        if not any(w in ("with", "w", "w/", "incl", "includes", "including", "and") for w in lows[:i]):
            kinds = ["accessory"]
    # gear beats the instrument it plugs into: 'Guitar Amplifier' is an amp, 'Guitar Pedal' is a pedal
    kind = next((k for k in PREFERRED.get(domain, ()) if k in kinds), kinds[0] if kinds else "other")
    # 'Canon EF 50mm Lens for Camera' is a lens; 'Minolta XG7 SLR Camera w/ 28mm Lens' is a camera that comes with one
    _stripped = re.sub(r"\b(?:for|fits?|compatible with|designed for)\s+(?:\w+\s+){0,3}(?:cameras?|slr|dslr|bodies|body)\b", " ", title, flags=re.I)
    if kind == "lens" and re.search(r"\b(camera|cameras|slr|dslr|camcorder|polaroid|instax|point\s*(?:and|&)\s*shoot|body|film|35mm\s+slr|digital)\b", _stripped, re.I):
        kind = "camera"  # 'Minolta XG7 35mm SLR Camera w/ 28mm f/2.8 Lens' is a camera that comes with a lens
    if domain != "fashion" and "console" in kinds and re.search(r"\b(console|system|bundle|handheld)\b", title, re.I):
        kind = "console"  # 'Wii Console Bundle with Games' is the console
    if kind == "console" and domain != "fashion" and not _CONSOLE_CUE.search(title):
        kind = "game"  # 'Nintendo Switch 2 Cyberpunk 2077' is a game for that console, not the console
    brand_words = set((brand or "").lower().replace("&", "").split()) | {code or ""}
    style = []
    phone = re.search(r"\biphone\s*(\d{1,2}[a-z]?)(?:\s*(plus|pro max|pro|mini|max|se))?", title, re.I) if domain != "fashion" else None
    if phone:  # iPhones are priced by generation: '6s plus' and '15 pro' are different markets
        style.append(("IPHONE " + phone.group(1) + (" " + phone.group(2) if phone.group(2) else "")).upper())
    elif domain != "fashion":
        for m in extract_models(title)[:1]:
            if m not in brand_words and not _SIZE.match(m):
                style.append(m.upper())
    for w in ([] if phone else words):
        lw = w.lower().strip(".-'")
        if not lw or lw in brand_words or lw in _NOISE or lw in lookup or _SIZE.match(lw) or len(lw) < 3:
            continue
        has_digit, has_alpha = bool(re.search(r"\d", lw)), bool(re.search(r"[a-z]", lw))
        if has_digit and has_alpha and 4 <= len(lw) <= 14:
            if lw.upper() not in style:
                style.insert(0, lw.upper())  # a model code outranks descriptive words
        elif not has_digit and w[0].isupper() and len(style) < 2:
            style.append(lw)
    if domain == "fashion" and code == "jordan" and kind == "other":
        kind = "shoes"  # a Jordan is a sneaker even when the title never says so
    if domain == "fashion" and code == "jordan":
        # sneakers are priced by model number and cut: 'Air Jordan 1 Retro High OG' vs '11 Low'
        num = re.search(r"\b(?:air\s+)?jordan\s*(?:retro\s*)?(\d{1,2})\b|\bretro\s*(\d{1,2})\b|\bAJ\s*(\d{1,2})\b", title, re.I)
        cut = re.search(r"\b(high|mid|low)\b", title, re.I)
        style = [f"{(num.group(1) or num.group(2) or num.group(3))} {cut.group(1).lower() if cut else ''}".strip()] if num else style
    elif domain == "fashion" and len(style) < 2:
        sub = next((w for w in lows if w in SUBTYPES), None)  # crossbody / tote / flip-flop...: tells bags and shoes apart
        if sub:
            style.append(sub)
    return brand, kind, " ".join(style[:2])


def store(conn: sqlite3.Connection, listings: list[Listing], domain: str = "fashion") -> int:
    now = int(time.time())
    n = 0
    for l in listings:
        brand, kind, style = style_of(l.title, domain)
        if not brand:
            continue
        conn.execute(
            """INSERT INTO catalog (platform, listing_id, title, brand, kind, style, category, listing_type, current_bid,
                                    buy_now, shipping, bids, end_time, url, first_seen, last_seen, domain, description, location)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
               ON CONFLICT(platform, listing_id) DO UPDATE SET current_bid=excluded.current_bid, buy_now=excluded.buy_now,
                 bids=excluded.bids, end_time=excluded.end_time, last_seen=excluded.last_seen, title=excluded.title,
                 description=COALESCE(excluded.description, catalog.description), location=COALESCE(excluded.location, catalog.location),
                 brand=excluded.brand, kind=excluded.kind, style=excluded.style, domain=excluded.domain""",
            (l.platform, l.listing_id, l.title, brand, kind, style, l.category, l.listing_type, l.price,
             l.extra.get("buy_now_price"), l.shipping_cost, (l.extra.get("bids") if l.extra.get("bids") is not None else l.extra.get("bid_count")),
             l.end_time.isoformat() if l.end_time else None, l.url, now, now, domain, l.extra.get("description"), l.location))
        n += 1
    conn.commit()
    return n


def restyle(conn: sqlite3.Connection) -> int:
    """Re-derive brand / type / style for every stored row from its title with the CURRENT rules (after improving them)."""
    n = 0
    for r in conn.execute("SELECT platform, listing_id, title, COALESCE(domain,'fashion') AS domain FROM catalog").fetchall():
        brand, kind, style = style_of(r["title"], r["domain"])
        if brand:
            conn.execute("UPDATE catalog SET brand=?, kind=?, style=? WHERE platform=? AND listing_id=?",
                         (brand, kind, style, r["platform"], r["listing_id"]))
            n += 1
        else:
            conn.execute("DELETE FROM catalog WHERE platform=? AND listing_id=?", (r["platform"], r["listing_id"]))
    conn.commit()
    return n


GENERIC_STYLE = {"pro", "max", "plus", "mini", "ultra", "air", "se", "lite", "series", "gen", "edition", "sport", "slim",
                 "standard", "premium", "classic", "original", "basic", "compact", "portable", "wireless", "stereo"}


def known_range(conn: sqlite3.Connection, brand: str, kind: str, style: str) -> kb.ValueRange | None:
    """Stored value range for a catalog group. Strict: a group needs a distinctive style word (a model code or style
    name) and ALL of 'brand + style' must appear in a stored item, so a kids' Jordan never inherits one Jordan 11's range.
    Otherwise falls back to the brand-level baseline for that item type."""
    if style and re.fullmatch(r"\d+\s?(?:gb|tb)", style.strip(), re.I):
        style = ""  # '128GB' is a size, not a product style: it must never pick up another product's range
    if style and not set(style.lower().split()) <= GENERIC_STYLE:  # 'pro' / 'max' alone says nothing about WHICH product
        for key in kb.find_items(conn, f"{brand} {style}", strict=True):
            r = kb.compute_range(conn, key)
            if r and r.typical is not None:
                return r
    return baseline_range(conn, brand, kind)


def model_range(conn: sqlite3.Connection, title: str) -> kb.ValueRange | None:
    """Range for THIS model: the listing's model identity (brand + model words) must be contained in a stored item's name, so a
    price saved for 'canon eos rebel xti' never applies to 'canon eos rebel' or to a different Canon. None when nothing matches."""
    from engine.title_cleaner import model_identity, variant_of
    ident = model_identity(title)
    if not ident:
        return None
    storage, conn_kind = variant_of(title)
    # most specific first: this storage size + connectivity, then this storage size, then the whole model pooled
    wanted = [f"{ident} {storage} {conn_kind}"] if storage and conn_kind else []
    wanted += [f"{ident} {storage}"] if storage else []
    for q in wanted:
        for key in kb.find_items(conn, q, strict=True):
            r = kb.compute_range(conn, key)
            if r and r.typical is not None:
                r.notes.append(f"model-specific: {key}")
                return r
    base = kb.item_key(ident)
    for key in kb.find_items(conn, ident, strict=True):
        if key == base:  # exact: a variant key such as 'ident 128gb' also contains the identity but is a different market
            r = kb.compute_range(conn, key)
            if r and r.typical is not None:
                r.notes.append(f"model-specific: {key}" + ("; storage/connectivity not separated" if storage is None or conn_kind is None else "; no sold sales for this exact variant, all variants pooled"))
                return r
    return None


_PLURAL = {"bag": "bags", "wallet": "wallets", "shoes": "footwear", "clothing": "clothing", "accessory": "accessories",
           "other": "other"}


def baseline_key(brand: str, kind: str) -> str:
    """Key of a brand-level baseline ('michael kors baseline-bags'): a typical used price for the whole brand+type."""
    return kb.item_key(f"{brand} baseline-{_PLURAL.get(kind, kind)}")


def baseline_range(conn: sqlite3.Connection, brand: str, kind: str) -> kb.ValueRange | None:
    r = kb.compute_range(conn, baseline_key(brand, kind))
    if r and r.typical is not None:
        r.notes.append("brand-level baseline, not this exact style")
        return r
    return None


def _end(r: sqlite3.Row) -> datetime | None:
    if not r["end_time"]:
        return None
    e = datetime.fromisoformat(r["end_time"])
    return e if e.tzinfo else e.replace(tzinfo=timezone.utc)


def groups(conn: sqlite3.Connection, only_auctions: bool = True, now: datetime | None = None,
           domain: str | None = None) -> list[dict]:
    now = now or datetime.now(timezone.utc)
    sql = "SELECT * FROM catalog WHERE 1=1"
    args: list = []
    if only_auctions:
        sql += " AND listing_type IN ('auction','daily_auction')"
    if domain:
        sql += " AND COALESCE(domain,'fashion') = ?"
        args.append(domain)
    by: dict[tuple, list[sqlite3.Row]] = {}
    for r in conn.execute(sql, args).fetchall():
        end = _end(r)
        if end is not None and end <= now:
            continue  # ended
        by.setdefault((r["domain"] or "fashion", r["brand"], r["kind"], r["style"]), []).append(r)
    out = []
    for (dom, brand, kind, style), rs in by.items():
        bids = [r["current_bid"] for r in rs]
        code = next((c for c, d in DISPLAY.items() if d == brand), brand.lower())
        out.append({"domain": dom, "brand": brand, "kind": kind, "style": style, "n": len(rs), "bid_low": min(bids),
                    "bid_median": statistics.median(bids), "bid_high": max(bids),
                    "range": known_range(conn, brand, kind, style), "tier": tier_of(code),
                    "sample": min(rs, key=lambda r: r["current_bid"])})
    return out


def priority(g: dict) -> float:
    """Which unknown groups to research first: valuable names with low bids and several listings."""
    tier = {"A": 3.0, "B": 1.5, "C": 0.5}[g["tier"]]
    cheap = 1.0 if g["bid_low"] < 100 else 0.6 if g["bid_low"] < 300 else 0.3
    return tier * cheap * (1 + min(g["n"], 8) / 4) * (1.3 if g["style"] and re.search(r"\d", g["style"]) else 1.0)


def baseline_queue(conn: sqlite3.Connection, domain: str | None = None, top: int = 30) -> list[dict]:
    """(brand, type) pairs with NO baseline yet, most numerous/valuable first: what to research to cover the catalog."""
    agg: dict[tuple, dict] = {}
    for g in groups(conn, True, None, domain):
        k = (g["domain"], g["brand"], g["kind"])
        a = agg.setdefault(k, {"domain": g["domain"], "brand": g["brand"], "kind": g["kind"], "n": 0, "bids": [],
                               "tier": g["tier"], "has": baseline_range(conn, g["brand"], g["kind"]) is not None})
        a["n"] += g["n"]
        a["bids"] += [g["bid_median"]] * g["n"]
    rows = [a for a in agg.values() if not a["has"] and a["kind"] != "other"]
    w = {"A": 3.0, "B": 1.5, "C": 0.6}
    for a in rows:
        a["median_bid"] = statistics.median(a["bids"])
        a["score"] = a["n"] * w[a["tier"]] * (1.0 if a["median_bid"] < 150 else 0.6)
    return sorted(rows, key=lambda a: -a["score"])[:top]


def condition_factor(title: str, cfg=None) -> tuple[float, str]:
    """(value multiplier, label) from the title: locked / for-parts 0.2, untested 0.6, refurbished 1.1, else 1.0."""
    from config import Config
    from engine.title_cleaner import condition
    cfg = cfg or Config()
    from engine.title_cleaner import telecom_status
    c = condition(title)
    base = {"locked": cfg.locked_condition_factor, "untested": cfg.risky_condition_factor,
            "refurbished": cfg.refurbished_condition_factor}.get(c, 1.0)
    ts = telecom_status(title)  # a phone/cellular device with nothing verified is 'moderate' (50% of a clean comp)
    if ts.mobile and ts.status != "clean":
        base = min(base, {"moderate": cfg.telecom_factor_moderate, "unverified": cfg.telecom_factor_unverified}[ts.status])
    return base, c


# Where auctions tend to END relative to the typical resale value (HEURISTIC from a handful of observations: PropertyRoom phones that
# opened at $1 reached roughly 35-45% of resale value; ShopGoodwill fashion/shoes bids stayed far lower). Used for a "likely" scenario only.
CLIMB = {"propertyroom": 0.40, "shopgoodwill": 0.25, "gsa": 0.30, "municibid": 0.35, "publicsurplus": 0.35, "govdeals": 0.35, "ebay": 0.80}


load_dotenv()  # this module may be imported before config.py has read .env
# eBay's published fee (checked 2026-10-10): 13.6% of the TOTAL sale (item + shipping + sales tax collected) + $0.40 per order over $10 ($0.30 at $10 or less).
# Sneakers sell at 8% when the sale is $150+ and handbags cost more; neither is modelled. RETURN_RESERVE is an assumption (returns / not-as-described losses).
EBAY_FEE = float(os.getenv("EBAY_FEE_PCT", "13.6")) / 100
EBAY_ORDER_FEE = float(os.getenv("EBAY_ORDER_FEE", "0.40"))
RETURN_RESERVE = float(os.getenv("RETURN_RESERVE_PCT", "3")) / 100
SALES_TAX = float(os.getenv("SALES_TAX_PCT", "8")) / 100  # on bid + buyer fee; the page lets you change it
TABLET_SHIPPING = 16.0  # typical inbound shipping for a tablet when the listing does not quote one


MEASURED: dict[tuple[str, str], float] = {}  # (platform, kind) -> median closing price / sold value, from closed_auctions.measure()


def load_measured(conn: sqlite3.Connection, min_samples: int = 15) -> int:
    """Replace guessed CLIMB values with measured ones where there are enough closed auctions (see closed_auctions.py)."""
    MEASURED.clear()
    try:
        for platform, kind, n, median in conn.execute("SELECT platform, kind, n, median FROM climb_stats"):
            if n >= min_samples and median:
                MEASURED[(platform, kind)] = float(median)
    except sqlite3.Error:
        pass
    return len(MEASURED)


_SMART_WATCH = re.compile(r"smart|fitbit|apple watch|garmin|galaxy watch|wearable", re.I)


def ebay_fee_terms(r) -> dict:
    """eBay's published NON-store final value fee for this item's category (ebay.com selling-fees page, checked 2026-10-10).

    Most categories pay EBAY_FEE (13.6%). Guitars & basses 6.7%; handbags 15%; watches (not smartwatches) 15%; trading cards,
    comics and coins 13.25%; athletic shoes pay 8% with NO per-order fee once the sale is $150 or more. Store-subscription
    discounts are not modelled (their rates could not be verified). Rates above $7,500 per item drop and are ignored."""
    kind = r["kind"]
    title = r["title"] if "title" in r.keys() else ""
    t = {"fee": EBAY_FEE, "order_fee": EBAY_ORDER_FEE, "fee_hi": None, "hi_at": None}
    if kind == "guitar":
        t["fee"] = 0.067
    elif kind == "bag":
        t["fee"] = 0.15
    elif kind == "shoes":
        t["fee_hi"], t["hi_at"] = 0.08, 150.0
    elif kind in ("cards", "coin", "comic"):
        t["fee"] = 0.1325
    elif kind == "watch" and not _SMART_WATCH.search(title):
        t["fee"] = 0.15
    return t


def fees_for(e: dict, sale: float) -> tuple[float, float]:
    """(percentage fee, per-order fee) at an expected sale price: shoes switch to 8% and no order fee at $150."""
    if e.get("hi_at") and sale >= e["hi_at"]:
        return e["fee_hi"], 0.0
    return e["fee"], e.get("order_fee", 0.30)


SOURCE_PREMIUM = {"propertyroom": 0.15, "gsa": 0.0, "municibid": 0.09, "publicsurplus": 0.105, "govdeals": 0.10}  # per-SOURCE guesses/typical values


def listing_premium(r) -> float:
    """Buyer's premium for ONE listing: the value read from its own sale terms (catalog.buyer_premium) when present, else the per-source constant."""
    keys = r.keys() if hasattr(r, "keys") else r
    own = r["buyer_premium"] if "buyer_premium" in keys else None
    return float(own) if own is not None else SOURCE_PREMIUM.get(r["platform"], 0.0)


def set_listing_premium(conn: sqlite3.Connection, platform: str, listing_id: str, pct: float, source: str) -> None:
    """Record a premium read from this listing's own terms (pct as a fraction, 0.15 = 15%). It overrides the per-source constant in economics()."""
    conn.execute("UPDATE catalog SET buyer_premium=?, premium_source=? WHERE platform=? AND listing_id=?", (pct, source, platform, str(listing_id)))
    conn.commit()


def economics(r, outbound: float = 15.0) -> dict:
    """Per-listing selling/buying assumptions: buyer premium, inbound shipping, resale fee rate, outbound shipping."""
    heavy = r["kind"] in HEAVY
    shipping = r["shipping"] if "shipping" in r.keys() else 0
    if r["platform"] == "ebay" and shipping == 0:
        inbound = 0.0  # a stored 0.0 is FREE shipping; unknown (calculated) shipping is stored as NULL and gets the default below
    else:
        inbound = None
    return {"prem": listing_premium(r),
            "inbound": inbound if inbound is not None else shipping if (shipping or 0) > 0 else (TABLET_SHIPPING if r["kind"] == "tablet" else 15.0 if r["platform"] == "propertyroom"
                                                    else 20.0 if heavy else 10.0), "tax": SALES_TAX,
            **ebay_fee_terms(r), "returns": RETURN_RESERVE, "ship_out": outbound * (2 if heavy else 1),
            "climb": MEASURED.get((r["platform"], r["kind"]), CLIMB.get(r["platform"], 0.30))}


def net_profit(e: dict, value: float, bid: float, factor: float = 1.0, fix: float = 0.0) -> float:
    """Profit if it resells at `value` and you win at `bid`: resale after fees and shipping, minus what you pay all-in."""
    fee, order = fees_for(e, value * factor)
    return value * factor * (1 - fee - e.get("returns", 0.0)) - order - e["ship_out"] - (bid * (1 + e["prem"]) * (1 + e.get("tax", 0.0)) + e["inbound"]) - fix


def max_bid(e: dict, value: float, factor: float = 1.0, fix: float = 0.0, min_profit: float = 15.0) -> float:
    """The most you can bid and still keep `min_profit` if it resells at `value`."""
    fee, order = fees_for(e, value * factor)
    return (value * factor * (1 - fee - e.get("returns", 0.0)) - order - e["ship_out"] - e["inbound"] - fix - min_profit) / ((1 + e["prem"]) * (1 + e.get("tax", 0.0)))


def _lot_opportunity(r, end, outbound: float) -> dict | None:
    """A GSA laptop lot valued from its description: units x per-model used price, untested haircut, drives to replace."""
    from engine.lots import lot_value
    desc = r["description"] if "description" in r.keys() else None
    lv = lot_value(desc) if desc else None  # the title's '(1) LOT OF' says nothing about the unit count
    if lv is None or lv["qty"] < 2:
        return None
    from config import Config
    cfg = Config()
    q = lv["qty"]
    e = economics(r, outbound)
    e = {**e, "ship_out": e["ship_out"] * q}
    rng = kb.ValueRange(kb.item_key(f"gsa lot {' + '.join(lv['models'])} x{q}"), lv["low"], lv["typ"], lv["high"], "C", "lot estimate", 0, None,
                        [f"{q} units ({', '.join(lv['models'])}) at used-market prices, from the lot description"
                         + ("; hard drives removed" if lv["drives_removed"] else "") + "; condition untested"])
    factor = cfg.risky_condition_factor  # government lots are sold untested
    fix = q * (35.0 if lv["drives_removed"] else 0.0)  # an SSD + OS install per unit
    net = lambda v: net_profit(e, v, r["current_bid"], factor, fix)
    return {"row": r, "range": rng, "net_typ": net(rng.typical), "net_low": net(rng.low),
            "headroom": max_bid(e, rng.typical, factor, fix), "end": end, "bid": r["current_bid"],
            "profitable": net(rng.typical) >= 15.0, "condition": "untested", "coarse": False, "econ": e,
            "factor": factor, "fix": fix, "lot": lv}


def opportunities(conn: sqlite3.Connection, domain: str | None = None, min_profit: float = 15.0,
                  now: datetime | None = None, outbound: float = 15.0) -> list[dict]:
    """Every running auction with a known range: profit at the current bid if it resells at the typical / low value.

    Assumptions (rough): eBay fees 13.25% (15% bags) + $0.30; inbound shipping $10 (ShopGoodwill; $20 for heavy items),
    or $15 + 15% premium (PropertyRoom), or the stored freight (GSA); outbound shipping `outbound` ($30 for heavy)."""
    now = now or datetime.now(timezone.utc)
    load_measured(conn)
    out = []
    cache: dict[tuple, kb.ValueRange | None] = {}
    sql = "SELECT * FROM catalog WHERE listing_type IN ('auction','daily_auction')"
    args: list = []
    if domain:
        sql += " AND COALESCE(domain,'fashion') = ?"
        args.append(domain)
    for r in conn.execute(sql, args).fetchall():
        end = _end(r)
        if end is not None and end <= now:
            continue
        k = (r["brand"], r["kind"], r["style"])
        if r["kind"] == "accessory":
            continue  # cases, chargers and cables aren't the product the range describes
        lot = _lot_opportunity(r, end, outbound) if r["platform"] == "gsa" else None
        if lot is not None:
            out.append(lot)
            continue
        ident_key = ("model", r["title"])
        if ident_key not in cache:
            cache[ident_key] = model_range(conn, r["title"])
        rng = cache[ident_key]  # a sold price for this exact model beats any group or brand-level range
        if rng is None:
            if k not in cache:
                cache[k] = known_range(conn, *k)
            rng = cache[k]
        if rng is None or rng.typical is None:
            continue
        factor, cond = condition_factor(r["title"])  # a range describes a WORKING unit
        e = economics(r, outbound)
        net = lambda v: net_profit(e, v, r["current_bid"], factor)
        coarse = any("brand-level baseline" in n for n in rng.notes)
        out.append({"row": r, "range": rng, "net_typ": net(rng.typical), "net_low": net(rng.low),
                    "headroom": max_bid(e, rng.typical, factor, 0.0, min_profit),
                    "end": end, "bid": r["current_bid"], "profitable": net(rng.typical) >= min_profit,
                    "condition": cond, "coarse": coarse, "econ": e, "factor": factor})
    return sorted(out, key=lambda x: -x["net_typ"])


def model_queue(opps: list[dict], top: int = 30) -> list[dict]:
    """Specific models that LOOK profitable only on a brand-wide baseline: research these first (most numerous x best margin)."""
    agg: dict[tuple, dict] = {}
    for o in opps:
        if not o.get("coarse") or o["net_typ"] < 15:
            continue
        r = o["row"]
        a = agg.setdefault((r["brand"], r["kind"], r["style"]), {"brand": r["brand"], "kind": r["kind"], "style": r["style"],
                                                                   "n": 0, "best": 0.0, "sample": r["title"], "url": r["url"]})
        a["n"] += 1
        a["best"] = max(a["best"], o["net_typ"])
    return sorted(agg.values(), key=lambda a: -(a["n"] * a["best"]))[:top]


def format_opportunities(opps: list[dict], top: int = 25, now: datetime | None = None) -> str:
    if not opps:
        return "No running auctions match a known price range yet. Add ranges (research) or run: python main.py --catalog"
    by: dict[tuple, list[dict]] = {}
    for o in opps:
        r = o["row"]
        by.setdefault((r["domain"] or "fashion", r["brand"], r["kind"]), []).append(o)
    lines = [f"{'DOMAIN':12} {'BRAND / TYPE':26} {'LISTED':>6} {'RANGE (typical)':>18} {'MEDIAN BID':>10} {'MAX BID':>8} "
             f"{'% PROFIT@TYP':>12} {'% SAFE@LOW':>10}", "-" * 112]
    for (dom, b, k), v in sorted(by.items(), key=lambda kv: -sum(x["profitable"] for x in kv[1])):
        rng = v[0]["range"]
        lines.append(f"{dom:12} {(b[:15] + ' ' + k)[:26]:26} {len(v):>6} ${rng.low:>5,.0f}-${rng.high:<6,.0f}({rng.typical:>4,.0f}) "
                     f"{statistics.median(x['bid'] for x in v):>10,.0f} {statistics.median(x['headroom'] for x in v):>8,.0f} "
                     f"{100 * sum(x['profitable'] for x in v) / len(v):>11.0f}% "
                     f"{100 * sum(x['net_low'] >= 0 for x in v) / len(v):>9.0f}%")
    specific = [o for o in opps if not o.get("coarse")]
    coarse = [o for o in opps if o.get("coarse")]
    lines += ["", f"BEST CURRENT AUCTIONS with a MODEL-SPECIFIC price range ({len(specific)}; profit at typical resale; bids will rise; "
                  f"verify condition/size/authenticity):"]
    shown = 0
    for o in specific + [dict(o, _header=True) for o in coarse[:1]] + coarse[1:]:
        if o.get("_header"):
            lines += ["", f"BRAND-LEVEL ESTIMATES ({len(coarse)}; the range is a brand-wide median, NOT this model: treat as unreliable until "
                          f"researched):"]
            shown = 0
        if shown >= top:
            continue
        if o["end"] is not None and (o["end"] - (now or datetime.now(timezone.utc))).total_seconds() < 1800:
            continue
        r = o["row"]
        flag = "" if o.get("condition", "working") in ("working", "refurbished") else f" <{o['condition'].upper()}>"
        lines.append(f"  +${o['net_typ']:>5,.0f} (low {o['net_low']:+5,.0f}) bid ${o['bid']:>7,.2f} max ${o['headroom']:>5,.0f} "
                     f"{time_left(o['end']) if o['end'] else '?':>7} [{o['range'].confidence}] {r['title'][:50]}{flag}\n        {r['url']}")
        shown += 1
    return "\n".join(lines)


def format_report(conn: sqlite3.Connection, top: int = 25, now: datetime | None = None, domain: str | None = None) -> str:
    gs = groups(conn, True, now, domain)
    if not gs:
        return "The catalog is empty. Run: python main.py --catalog"
    total = sum(g["n"] for g in gs)
    lines = [f"CATALOG: {total} auctions still running, {len(gs)} groups" + (f" ({domain})" if domain else ""), ""]
    doms: dict[str, int] = {}
    for g in gs:
        doms[g["domain"]] = doms.get(g["domain"], 0) + g["n"]
    lines.append("BY DOMAIN: " + ", ".join(f"{d} {n}" for d, n in sorted(doms.items(), key=lambda kv: -kv[1])))
    lines.append("")
    brands: dict[tuple, list[dict]] = {}
    for g in gs:
        brands.setdefault((g["domain"], g["brand"]), []).append(g)
    lines += [f"{'DOMAIN':12} {'BRAND':20} {'TIER':>4} {'ITEMS':>5} {'BID MEDIAN':>10} {'GROUPS W/ RANGE':>16}", "-" * 74]
    for (d, b), bgs in sorted(brands.items(), key=lambda kv: -sum(g["n"] for g in kv[1]))[:25]:
        n = sum(g["n"] for g in bgs)
        med = statistics.median([g["bid_median"] for g in bgs for _ in range(g["n"])])
        lines.append(f"{d:12} {b[:20]:20} {bgs[0]['tier']:>4} {n:>5} {med:>10,.0f} {sum(1 for g in bgs if g['range']):>10}/{len(bgs)}")
    q = baseline_queue(conn, domain, top)
    if q:
        lines += ["", "BASELINES STILL TO RESEARCH (brand + type with no typical price yet, most listings x value first):", "-" * 90]
        for a in q:
            lines.append(f"{a['domain']:12} {a['brand'][:22]:22} {a['kind']:11} x{a['n']:<4} median bid ${a['median_bid']:>5,.0f}  tier {a['tier']}")
    lines += ["", "RESEARCH QUEUE (specific styles with no value range yet, best first):", "-" * 100]
    for g in sorted([g for g in gs if not g["range"]], key=priority, reverse=True)[:top]:
        s = g["sample"]
        end = _end(s)
        lines.append(f"{g['brand'][:16]:16} {g['kind']:9} {g['style'][:20]:20} x{g['n']:<3} bids ${g['bid_low']:,.0f}-{g['bid_high']:,.0f}"
                     f"  e.g. {time_left(end) if end else '?':>7} {s['title'][:44]}")
    known = [g for g in gs if g["range"]]
    if known:
        lines += ["", "WITH A KNOWN VALUE RANGE (resale range vs. cheapest current bid):", "-" * 100]
        for g in sorted(known, key=lambda g: g["range"].typical - g["bid_low"], reverse=True)[:top]:
            r, s = g["range"], g["sample"]
            lines.append(f"{g['brand'][:16]:16} {g['kind']:8} {g['style'][:16]:16} bid ${g['bid_low']:>6,.0f} vs worth "
                         f"${r.low:,.0f}-${r.high:,.0f} (typ ${r.typical:,.0f}, {r.confidence})  {s['url']}")
    return "\n".join(lines)


def _branded(l: Listing, designer_only: bool) -> bool:
    code = extract_brand(l.title)
    return code in DESIGNER_BRANDS if designer_only else code is not None


async def crawl(conn: sqlite3.Connection, http: httpx.AsyncClient, cfg, max_pages: int | None = None,
                domains: list[str] | None = None) -> dict:
    """Crawl the requested domains (default: all) into the catalog. Returns {domain: stored count}."""
    from clients.gsa import GSAClient
    from clients.propertyroom import PropertyRoomClient
    from clients.shopgoodwill import ShopGoodwillClient
    want = set(domains or ALL_DOMAINS)
    sgw = ShopGoodwillClient(http)
    stats: dict[str, int] = {}

    def add(domain: str, listings: list[Listing], designer_only: bool = False) -> None:
        kept = [l for l in listings if _branded(l, designer_only)]
        stats[domain] = stats.get(domain, 0) + store(conn, kept, domain)
        stats[f"{domain}_scanned"] = stats.get(f"{domain}_scanned", 0) + len(listings)

    if "fashion" in want:
        add("fashion", await sgw.fetch_all(cfg.sgw_fashion_category_ids, max_pages), designer_only=True)
        add("fashion", await sgw.search(list(LUXURY_SEARCH_TERMS), max_pages or 5), designer_only=True)
        pr = PropertyRoomClient(None, ["fashion"], pages=max_pages or 40, premium_pct=cfg.propertyroom_premium_pct,
                                shipping=cfg.propertyroom_shipping_usd)
        try:
            add("fashion", await pr.fetch(ttl=0), designer_only=True)
        finally:
            await pr.aclose()
    for cid, dom in SGW_DOMAINS.items():
        if dom in want:
            add(dom, await sgw.fetch_all([cid], max_pages))
    for cat, dom in PR_DOMAINS.items():
        if dom in want:
            pr = PropertyRoomClient(None, [cat], pages=max_pages or 40, premium_pct=cfg.propertyroom_premium_pct,
                                    shipping=cfg.propertyroom_shipping_usd)
            try:
                add(dom, await pr.fetch(ttl=0))
            finally:
                await pr.aclose()
    if "municibid" in want:
        from clients.municibid import MunicibidClient
        for domain, listings in (await MunicibidClient(http).fetch_all(max_pages)).items():
            add(domain, listings)
    if "publicsurplus" in want:
        from clients.publicsurplus import PublicSurplusClient
        for domain, listings in (await PublicSurplusClient(http).fetch_all(max_pages)).items():
            add(domain, listings, designer_only=domain == "fashion")
    if "govdeals" in want:
        from clients.govdeals_api import GovDealsApiClient
        for domain, listings in (await GovDealsApiClient(http).fetch_all(max_pages)).items():
            add(domain, listings, designer_only=domain == "fashion")
    if "illinois_ibid" in want:  # state surplus + Treasurer lots: fetched automatically with the rest of the catalog; stored with their premium/tax read from the site
        from clients import illinois_ibid
        try:
            res = await illinois_ibid.sync(conn, http)
            stats["illinois_ibid"], stats["illinois_ibid_scanned"] = res["lots"], res["listed"]
        except (httpx.HTTPError, PermissionError) as e:
            stats["illinois_ibid_error"] = str(e)[:80]
    if "ebay" in want and cfg.ebay_client_id and cfg.ebay_client_secret:
        add_ebay = await _crawl_ebay(conn, http, cfg)
        stats["ebay"] = add_ebay
    if "gsa" in want:
        gsa = GSAClient(http, cfg.gsa_api_key, cfg.gsa_min_interval, cfg.gsa_premium_pct, cfg.gsa_freight_usd)
        try:
            add("gsa", await gsa.fetch())
        except httpx.HTTPError as e:  # DEMO_KEY allows only 10 requests/hour
            stats["gsa_error"] = str(e)[:80]
    return stats


_EBAY_DOMAINS = ("electronics", "computers", "cameras", "instruments", "collectibles", "tools")


def ebay_queries(conn: sqlite3.Connection) -> list[str]:
    """Models worth searching eBay for: the ones that already have a real sold price (so a find can be valued at once)."""
    rows = conn.execute("SELECT DISTINCT item_key FROM price_observations WHERE kind='sold' AND source='soldcomps-fill'").fetchall()
    return sorted(r[0] for r in rows)


def _guess_domain(title: str) -> str | None:
    for dom in _EBAY_DOMAINS:
        brand, kind, _ = style_of(title, dom)
        if brand and kind not in ("other", "accessory"):
            return dom
    return None


async def _crawl_ebay(conn: sqlite3.Connection, http: httpx.AsyncClient, cfg) -> int:
    """eBay AUCTIONS ending soonest for each priced model, stored like every other site (no buyer fee; eBay collects sales tax)."""
    from clients.ebay import EbayClient
    client = EbayClient(http, cfg.ebay_client_id, cfg.ebay_client_secret, False, False)
    n = 0
    for q in ebay_queries(conn):
        try:
            found = await client.new_listings(q, auction=True, limit=50, ttl=0)
        except httpx.HTTPError as e:
            log.warning("eBay search %r failed: %s", q, e)
            continue
        by: dict[str, list[Listing]] = {}
        for l in found:
            if l.extra.get("shipping_cost_type") == "CALCULATED":
                l.shipping_cost = None  # unknown: the default shipping estimate applies
            dom = _guess_domain(l.title)
            if dom:
                by.setdefault(dom, []).append(l)
        for dom, ls in by.items():
            n += store(conn, ls, dom)
        await asyncio.sleep(0.4)
    return n
