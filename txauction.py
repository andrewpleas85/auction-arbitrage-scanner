"""Read public TXAuction (Gaston & Sheehan) auction pages for the page's scrap and watch sections.

Only /auctions/ pages are read: robots.txt disallows /api/ and /asset/, so neither is touched. Each page
embeds its first 50 lots (description, bid, bid count, end time) as Apollo state, so one request per
auction is enough. Lots beyond the first 50 are not read.
"""
import asyncio
import html
import json
import re
import time
from pathlib import Path
from datetime import datetime, timezone

UA = "arbitrage-scanner/1.0 (personal use)"
LOCATION = ("Pflugerville", "TX")
SOURCE = "TXAuction"


def _state(page: str) -> dict:
    m = re.search(r"__APOLLO_STATE__\s*=\s*", page)
    if not m:
        raise ValueError("no embedded auction data on the page")
    start = j = page.find("{", m.end())
    depth = 0
    for j in range(start, len(page)):
        depth += (page[j] == "{") - (page[j] == "}")
        if depth == 0:
            break
    return json.loads(page[start:j + 1])


def _plain(text: str | None) -> str:
    return re.sub(r"\s+", " ", html.unescape(re.sub(r"<[^>]+>", " ", text or ""))).strip()


def parse_page(page: str) -> tuple[dict, list[dict]]:
    """(auction, lots): auction has id/title/url/end/premium text flags; each lot has lot/bid/bids/start/category/desc/end."""
    objs = _state(page).values()
    auc = next(v for v in objs if isinstance(v, dict) and v.get("__typename") == "Auction")
    alert, terms = _plain(auc.get("alert_text")), _plain(auc.get("terms"))
    premium = re.search(r"(\d+(?:\.\d+)?)\s*%\s*buyer'?s premium", alert + " " + terms, re.I)
    auction = {"id": str(auc["auction_id"]), "title": auc.get("title"), "url": auc.get("public_url"), "end": auc.get("end_time"),
               "visible_lots": auc.get("front_visible_lot_count"),
               "no_premium": bool(re.search(r"not\s+a\s+buyer'?s premium", alert, re.I)),
               "premium": float(premium.group(1)) / 100 if premium else None,
               "no_deposit": "no deposit is required" in terms.lower()}
    lots = []
    for v in objs:
        if not (isinstance(v, dict) and v.get("__typename") == "AuctionLot"):
            continue
        bid = v.get("winning_bid_amount")
        lots.append({"lot": str(v["lot_number"]), "lot_id": str(v.get("auction_lot_id") or ""), "bid": float(bid) if bid is not None else None, "bids": int(v.get("bid_count") or 0),
                     "start": v.get("starting_bid"), "watching": v.get("watch_count"), "end": v.get("end_time"),
                     "category": (v.get("category") or {}).get("name"), "desc": _plain(v.get("description_plain"))})
    lots.sort(key=lambda l: (len(re.sub(r"\D", "", l["lot"])), l["lot"]))
    return auction, lots


def lot_url(auction_id: str, lot: dict) -> str | None:
    """Direct link to one lot (public /auctions/ path, same form the site's own search results use), else None."""
    lid, num = lot.get("lot_id"), re.sub(r"[^A-Za-z0-9]+", "-", str(lot.get("lot", ""))).strip("-").lower()
    return f"{BASE}/auctions/{auction_id}/lot/{lid}-lot-{num}" if lid and num else None


# What goes on the watch list: the owner's standing focus is tech and handbags, never shoes.
_BAG_RE = re.compile(r"\b(handbags?|luggage|satchel|tote|clutch|wallet|purse|backpack|briefcase|keepall|messenger)\b", re.I)
_DROP_RE = re.compile(r"^\s*(?:\[\d+\]\s*)?(?:shoes?|clothing|shirts?|vest|jacket|outerwear)\b", re.I)
_JEWEL_RE = re.compile(r"\b(\d{1,2}\s?k(?:t|arat)?|gold|sterling|platinum|diamonds?|jewelry|jewellery|necklaces?|bracelets?|earrings?|pendants?|rings?|chains?)\b", re.I)
_COIN_RE = re.compile(r"\b(coins?|krugerrand|bullion|ingots?|(?:troy|fine)\s+ounce|american (?:gold|silver) eagle|maple leaf|pesos?|sovereigns?|ducats?)\b", re.I)
_TRADING_CARD_RE = re.compile(r"(trading cards?|baseball cards?|football cards?|basketball cards?|hockey cards?|sports cards?|pok[eé]mon|magic cards?|yu-?gi-?oh|konami|psa|bgs|cgc|topps|panini|donruss|upper deck|fleer|wild card)", re.I)  # not in the owner's focus


def topic_of(lot: dict) -> str | None:
    d = lot["desc"]
    if _TRADING_CARD_RE.search(d[:200]):
        return None  # trading cards are outside the owner's focus
    if _DROP_RE.match(d) and not _BAG_RE.search(d[:60]):
        return None
    if _BAG_RE.search(d[:80]):
        return "handbag"
    if re.match(r"\s*ARTWORK\b", d) or "artwork" in (lot.get("category") or "").lower():
        return "art"
    if re.search(r"\bwatch(es)?\b", d[:80], re.I):
        return "watch"
    if _COIN_RE.search(d[:100]):
        return "coins"
    if _JEWEL_RE.search(d[:80]):
        return "jewelry"
    return None


def watch_rows(auction: dict, lots: list[dict], now: datetime | None = None) -> list[dict]:
    """Unpriced lots shaped like bidprowl_sync.watch_rows (band 'ships': buyer pays shipping from Texas)."""
    now = now or datetime.now(timezone.utc)
    rows = []
    for l in lots:
        topic = topic_of(l)
        if topic is None or not l.get("end"):
            continue
        try:
            end = datetime.fromisoformat(l["end"].replace("Z", "+00:00"))
        except ValueError:
            continue
        if end <= now:
            continue
        rows.append({"band": "ships", "value": None, "id": f"tx{auction['id']}-{l['lot']}", "title": f"Lot {l['lot']}: {l['desc'][:230]}",
                     "source": SOURCE, "topic": topic, "category": l.get("category"), "bid": l["bid"] if l["bid"] is not None else l.get("start"),
                     "bids": l["bids"], "end": end.isoformat(), "city": LOCATION[0], "state": LOCATION[1], "url": lot_url(auction["id"], l) or auction["url"],
                     "quantity": None, "condition": "as is" + ("; no buyer's premium" if auction["no_premium"] else ""), "_desc": l["desc"]})
    return rows


AUCTION_URLS = [
    "https://www.txauction.com/auctions/31543-austin-police-and-city-of-austin-jewelry-and-bullion-online-auction",
    "https://www.txauction.com/auctions/31520-us-marshals-service-designer-goods-auction",
    "https://www.txauction.com/auctions/31503-us-marshals-service-collectibles-auction",
    "https://www.txauction.com/auctions/31468-us-marshals-service-fine-jewelry-auction",
    "https://www.txauction.com/auctions/31534-us-marshals-service-art-auction",
]
SNAPSHOT = Path("txauction_snapshot.json")  # gitignored: bids go stale within hours
MAX_AGE = 6 * 3600


INDEX_URL = "https://www.txauction.com/auctions"
BASE = "https://www.txauction.com"
# Auction types worth reading automatically: the owner's focus is tech and handbags, plus jewelry/bullion for melt.
_WANT_SLUG = re.compile(r"designer|handbag|accessor|jewelry|bullion|watch|collectible|clothing|electronic|computer|technology|art\b|surplus", re.I)
_SKIP_SLUG = re.compile(r"towing|vehicle|impound|fleet|equipment|liquor|wine|firearm|crystal|mineral|geode|gem(?:stone)?s?\b|rock", re.I)


def index_auctions(page: str) -> list[tuple[str, str]]:
    """(id, slug) of every auction linked from the public /auctions index."""
    return sorted(set(re.findall(r"/auctions/(\d+)-([a-z0-9-]+)", page)))


def wanted(slug: str) -> bool:
    return bool(_WANT_SLUG.search(slug)) and not _SKIP_SLUG.search(slug)


def discover(page: str, known_ids: set[str]) -> list[str]:
    """URLs of wanted auctions on the index that are not already in `known_ids`."""
    return [f"{BASE}/auctions/{i}-{slug}" for i, slug in index_auctions(page) if i not in known_ids and wanted(slug)]


PAGE_SIZE = 50   # lots per page the site renders by default
MAX_PAGES = 8    # never walk more than 400 lots of one auction


def more_pages(auction: dict, have: int) -> int:
    """How many extra pages (?page=2, 3, ...) are needed to read every visible lot."""
    total = auction.get("visible_lots") or have
    if have >= total:
        return 0
    return min(MAX_PAGES - 1, -(-(total - have) // PAGE_SIZE))


def merge_lots(*groups: list[dict]) -> list[dict]:
    seen, out = set(), []
    for g in groups:
        for l in g:
            if l["lot"] not in seen:
                seen.add(l["lot"])
                out.append(l)
    return sorted(out, key=lambda l: (len(re.sub(r"\D", "", l["lot"])), l["lot"]))


def load_snapshot(path: Path = SNAPSHOT) -> dict:
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {"fetched_at": 0, "auctions": []}


def is_stale(snap: dict, now: float | None = None) -> bool:
    return (now or time.time()) - snap.get("fetched_at", 0) > MAX_AGE


async def sync(http, urls: list[str] | None = None, path: Path = SNAPSHOT, pause: float = 2.0, find_new: bool = True) -> dict:
    """One request per auction page (about four), spaced out. A page that fails keeps its previous snapshot."""
    old = {a["auction"]["id"]: a for a in load_snapshot(path)["auctions"]}
    urls = list(urls or AUCTION_URLS)
    if urls_from_snapshot := [a["auction"]["url"] for a in old.values() if a["auction"].get("url")]:
        have = {re.search(r"/auctions/(\d+)-", u).group(1) for u in urls if re.search(r"/auctions/(\d+)-", u)}
        urls += [u for u in urls_from_snapshot if (m := re.search(r"/auctions/(\d+)-", u)) and m.group(1) not in have]  # discovered earlier: keep them fresh
    if find_new:
        try:
            idx = await http.get(INDEX_URL, headers={"User-Agent": UA}, timeout=30, follow_redirects=True)
            idx.raise_for_status()
            known = set(old) | {re.search(r"/auctions/(\d+)-", u).group(1) for u in urls}
            new = discover(idx.text, known)
            if new:
                print(f"TXAuction: {len(new)} new auction(s) found: " + ", ".join(u.rsplit("/", 1)[-1][:60] for u in new))
            urls += new
            await asyncio.sleep(pause)
        except Exception as e:
            print(f"TXAuction index skipped: {e!r}")
    for i, url in enumerate(urls):
        if i:
            await asyncio.sleep(pause)
        try:
            resp = await http.get(url, headers={"User-Agent": UA}, timeout=30, follow_redirects=True)
            resp.raise_for_status()
            auction, lots = parse_page(resp.text)
            for n in range(2, 2 + more_pages(auction, len(lots))):
                await asyncio.sleep(pause)
                more = await http.get(url, params={"page": n}, headers={"User-Agent": UA}, timeout=30, follow_redirects=True)
                more.raise_for_status()
                lots = merge_lots(lots, parse_page(more.text)[1])
        except Exception as e:  # network error, bad status, or a page without lot data: keep what we had
            print(f"TXAuction {url.rsplit('/', 1)[-1][:40]} skipped: {e!r}")
            continue
        old[auction["id"]] = {"auction": auction, "lots": lots}
    now = datetime.now(timezone.utc).isoformat()
    kept = [a for a in old.values() if (a["auction"].get("end") or "9") > now]  # ended auctions drop off (ISO strings sort by time)
    snap = {"fetched_at": time.time(), "auctions": kept}
    Path(path).write_text(json.dumps(snap), encoding="utf-8")
    return snap


VALUES = Path("txauction_values.json")
HIDDEN = Path("txauction_hidden.json")
TRACKED = Path("txauction_tracked.json")


def tracked_lots(path: Path = TRACKED) -> dict:
    """Lots on the owner's list: {'auction-lot': {'max_bid': float|None, 'note': str}}."""
    try:
        d = json.loads(Path(path).read_text(encoding="utf-8")).get("tracked", {})
        return d if isinstance(d, dict) else {}
    except (OSError, ValueError):
        return {}


def hidden_keys(path: Path = HIDDEN) -> set[str]:
    """Lots the owner removed from the page, as 'auction-lot' keys."""
    try:
        return set(json.loads(Path(path).read_text(encoding="utf-8")).get("hidden", []))
    except (OSError, ValueError):
        return set()


def snapshot_watch_rows(snap: dict, now: datetime | None = None, values_path: Path = VALUES, gold_spot: float | None = None) -> list[dict]:
    """Watch rows for every snapshot auction; lots carry value/low/high/basis from a hand-set estimate, else (jewelry) from
    gold melt plus diamonds at per-carat scenarios."""
    import jewelry
    try:
        values = json.loads(Path(values_path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        values = {}
    hidden = hidden_keys()
    rows = [r for a in snap.get("auctions", []) for r in watch_rows(a["auction"], a["lots"], now)
            if r["id"].removeprefix("tx") not in hidden]
    for r in rows:
        v = values.get(r["id"].removeprefix("tx"))
        if isinstance(v, dict):
            r.update(value=v["value"], low=v.get("low"), high=v.get("high"), basis=v.get("basis"))
        elif r["topic"] == "jewelry" and gold_spot:
            e = jewelry.estimate(r.get("_desc") or "", gold_spot)
            if e:
                r.update(value=e["mid"], low=e["low"], high=e["high"],
                         basis=f"{e['karat']}K {e['grams']:g} g, {e['cttw']:g} cttw (seller's estimate): gold melt ${e['gold']:.0f} (90% of melt) + diamonds at "
                               f"${jewelry.DIAMOND_PER_CT['low']:.0f} / ${jewelry.DIAMOND_PER_CT['mid']:.0f} / ${jewelry.DIAMOND_PER_CT['high']:.0f} per carat")
    for r in rows:
        r.pop("_desc", None)  # full description only needed for the estimate; keeps the page payload small
    return rows
