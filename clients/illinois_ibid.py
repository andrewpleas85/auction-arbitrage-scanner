"""Illinois iBid (ibid.illinois.gov): State of Illinois (CMS) surplus lots (a PHP auction site, server-rendered). The Treasurer's unclaimed-property jewelry sells on HiBid, not here.

Verified from the live pages on 2026-11-11 (robots.txt blocks only /admin/, /ckeditor/, /images/, /js/, /includes/, /themes/, /uploaded/):
  * buyer's fee: fees.php lists "Buyer Fee 2.0000%" (the 'Final Price Fee' rows are SELLER fees)
  * sales tax: Terms and Conditions item 9, "Sales Tax will not be added"
  * shipping: the State does not ship; pickup at 1924 S. 10 1/2 St., Springfield (12:30-3:45 pm weekdays) or a listed third-party shipper, arranged and paid by the buyer
  * item pages: lot number, title, seller location, "Auction Ends In ... (Nov 13, 2026 - 07:02 PM)" (Springfield local time), bids, current bid, description
Every request is sequential with a pause, identifies itself, and stays on the allowed paths. The premium and tax are READ from the site's own pages at each
sync and stored on the sale (lot_sources), never kept as a constant. Karat and gram weights in titles/descriptions go to the melt model.
"""
from __future__ import annotations

import asyncio
import html as htmllib
import re
from datetime import datetime
from zoneinfo import ZoneInfo

import httpx

BASE = "https://ibid.illinois.gov"
UA = "arbitrage-scanner/1.0 (personal price lookup)"
CATEGORIES = {"jewelry": 561, "coins": 691, "photographic": 621, "computers": 481, "communication": 471, "electrical": 501, "household": 541, "office": 611, "misc": 591}
SPRINGFIELD_MILES = 200.0   # approximate driving distance from Chicago (an estimate, not measured here)
CT = ZoneInfo("America/Chicago")
_ROW = re.compile(r'<tr align="center"[^>]*>(.*?)</tr>', re.S)
_ITEM_LINK = re.compile(r'(\d+)\s*-\s*<a href="[^"]*item\.php\?id=(\d+)">([^<]*)</a>', re.S)


def _text(fragment: str) -> str:
    t = re.sub(r"<script.*?</script>|<style.*?</style>", " ", fragment, flags=re.S | re.I)
    return re.sub(r"\s+", " ", htmllib.unescape(re.sub(r"<[^>]+>", " ", t))).strip()


def parse_browse(page: str) -> list[dict]:
    """[{'id', 'lot_label', 'title', 'current_bid', 'bids', 'ends_in'}] from a category listing page."""
    out = []
    for row in _ROW.findall(page):
        m = _ITEM_LINK.search(row)
        if not m:
            continue
        bid = re.search(r"<strong>\s*([\d,]+\.\d{2})\s*</strong>", row)
        cells = re.findall(r"<td[^>]*>(.*?)</td>", row, re.S)
        bids = _text(cells[3]) if len(cells) >= 5 else ""
        out.append({"id": m.group(2), "lot_label": m.group(1), "title": _text(m.group(3)), "current_bid": float(bid.group(1).replace(",", "")) if bid else 0.0,
                    "bids": int(re.sub(r"\D", "", bids) or 0), "ends_in": _text(cells[4]) if len(cells) >= 5 else ""})
    return out


def parse_item(page: str) -> dict:
    """Detail fields from an item page; the end time is converted from Springfield local time to UTC."""
    t = _text(re.sub(r"<head.*?</head>", " ", page, flags=re.S | re.I))  # the <title> repeats the lot title with the site name appended: read the body only
    g = lambda pat: (re.search(pat, t) or [None, None])[1]
    ends = None
    m = re.search(r"\((\w{3} \d{1,2}, \d{4}) - (\d{1,2}:\d{2} [AP]M)\)", t)
    if m:
        ends = datetime.strptime(f"{m.group(1)} {m.group(2)}", "%b %d, %Y %I:%M %p").replace(tzinfo=CT).astimezone(ZoneInfo("UTC")).isoformat()
    after = t.split("Item Description", 2)
    description = after[2].split("Guidelines")[0].strip() if len(after) > 2 else ""
    return {"category": g(r"Item Category: (.*?) Lot #"), "lot": g(r"Lot # (\S+)"), "title": (re.search(r"Item Category: .*? Lot # \S+ (.*?) Auction ID:", t) or [None, ""])[1].strip().lstrip("- ").strip(),
            "seller": (g(r"Seller: (.*?) Sellers auctions") or g(r"Seller: (.*?) auctions") or "").strip(" *"),
            "seller_location": g(r"Seller location: (.*?) \* Highest bidder|Seller location: (.*?) Auction Ends"), "ends": ends,
            "bids": int(g(r"# of bids: (\d+)") or 0), "current_bid": float((g(r"Current Bid: \$ ([\d,]+\.\d{2})") or "0").replace(",", "")),
            "shipping_fee": float(g(r"Shipping Fee: \$ ([\d,]+\.\d{2})") or 0), "description": description}


def parse_buyer_fee(page: str) -> float | None:
    """The buyer's fee as a fraction from fees.php ('Buyer Fee From $0.00 To $... 2.0000%'); None if it is not found."""
    t = _text(page)
    m = re.search(r"Buyer Fee(?: From \$ [\d,.]+ To \$ [\d,.]+)? ([\d.]+)%", t)
    return float(m.group(1)) / 100 if m else None


def parse_no_sales_tax(page: str) -> bool:
    return bool(re.search(r"Sales Tax will not be added", _text(page), re.I))


_KARAT = re.compile(r"\b(8|9|10|12|14|18|20|21|22|24)\s?-?\s?(?:k|kt|karat|carat)\b", re.I)
_WEIGHT = re.compile(r"(\d+(?:\.\d+)?)\s*(grams?|gms?|grs?|g|dwt|ozt|troy\s*oz)\b", re.I)


def parse_weight_karat(*texts: str) -> tuple[float | None, int | None]:
    """(grams, karat) from titles/descriptions: '14K', '12.4 grams', '3 dwt', '1.5 ozt'. None for what is not stated."""
    text = " ".join(t for t in texts if t)
    k = _KARAT.search(text)
    w = _WEIGHT.search(text)
    grams = None
    if w:
        unit = w.group(2).lower()
        grams = float(w.group(1)) * (1.55517 if unit == "dwt" else 31.1035 if unit.startswith(("ozt", "troy")) else 1.0)
    return (round(grams, 3) if grams else None), (int(k.group(1)) if k else None)


def disallowed(robots: str) -> list[str]:
    out, applies = [], False
    for line in robots.splitlines():
        line = line.split("#")[0].strip()
        if line.lower().startswith("user-agent:"):
            applies = line.split(":", 1)[1].strip() == "*"
        elif applies and line.lower().startswith("disallow:") and line.split(":", 1)[1].strip():
            out.append(line.split(":", 1)[1].strip())
    return out


def build_import(fees_page: str, terms_page: str, items: list[dict], read_on: str) -> dict:
    """Group the parsed items into one sale per seller, with the premium and tax READ from the site's own pages."""
    fee, no_tax = parse_buyer_fee(fees_page), parse_no_sales_tax(terms_page)
    sales: dict[str, dict] = {}
    for it in items:
        seller = it.get("seller") or "CMS iBid"
        sid = "ibid-" + re.sub(r"[^a-z0-9]+", "-", seller.lower()).strip("-")
        loc = it.get("seller_location") or ""
        sale = sales.setdefault(sid, {
            "source": "illinois_ibid", "sale_id": sid, "title": f"Illinois iBid: {seller}", "url": BASE + "/", "ends": None,
            "buyer_premium": fee, "premium_source": f"iBid fees page {BASE}/fees.php read {read_on}: 'Buyer Fee' " + (f"{fee * 100:.4g}%" if fee is not None else "NOT FOUND"),
            "sales_tax": 0.0 if no_tax else None, "tax_source": f"iBid terms item 9 ({BASE}/contents.php?show=terms) read {read_on}: 'Sales Tax will not be added'" if no_tax else None,
            "pickup_only": True, "pickup_location": loc, "ships": False,
            "distance_miles": SPRINGFIELD_MILES if "springfield" in loc.lower() else None,
            "shipping_note": "The State of Illinois does not ship: pick up in Springfield (12:30-3:45 pm weekdays) or arrange a listed third-party shipper yourself", "lots": []})
        weight, karat = parse_weight_karat(it.get("title"), it.get("description"))
        sale["lots"].append({"lot_id": it["id"], "title": it["title"], "description": it.get("description") or "", "current_bid": it.get("current_bid", 0.0), "bids": it.get("bids", 0),
                             "ends": it.get("ends"), "url": f"{BASE}/item.php?id={it['id']}", "category": it.get("category"), "weight_g": weight, "karat": karat})
    return {"sales": list(sales.values())}


async def sync(conn, http: httpx.AsyncClient, categories=("jewelry", "coins", "photographic", "computers", "communication", "electrical", "household", "misc"),
               delay: float = 2.0, max_items: int = 60, want=re.compile(r"gold|silver|jewel|ring|chain|necklace|bracelet|watch|coin|laptop|computer|ipad|tablet|camera|lens|iphone|phone|apple|dell|hp |lenovo|bag|purse", re.I)) -> dict:
    import lot_sources
    hdr = {"User-Agent": UA}
    stop = disallowed((await http.get(BASE + "/robots.txt", headers=hdr)).text)

    async def get(path: str) -> str:
        if any(path.startswith(p) for p in stop):
            raise PermissionError(f"{path} is disallowed by robots.txt")
        await asyncio.sleep(delay)
        r = await http.get(BASE + "/" + path.lstrip("/"), headers=hdr, timeout=30)
        r.raise_for_status()
        return r.text

    fees, terms = await get("fees.php"), await get("contents.php?show=terms")
    found, seen = [], set()
    for cat in categories:
        for it in parse_browse(await get(f"browse.php?id={CATEGORIES[cat]}")):
            if it["id"] not in seen:
                seen.add(it["id"])
                found.append((cat, it))
    items = []
    for cat, it in found:
        if len(items) >= max_items:
            break
        if cat not in ("jewelry", "coins") and not want.search(it["title"]):
            continue  # only fetch detail pages for lots that could be jewelry/metal or the tech we price
        d = parse_item(await get(f"item.php?id={it['id']}"))
        items.append({**it, **{k: v for k, v in d.items() if v not in (None, "")}, "title": d.get("title") or it["title"], "id": it["id"]})
    read_on = datetime.now(ZoneInfo("UTC")).strftime("%Y-%m-%d")
    data = build_import(fees, terms, items, read_on)
    res = lot_sources.import_data(conn, data)
    return {**res, "listed": len(found), "detail_pages": len(items), "buyer_fee": parse_buyer_fee(fees), "no_sales_tax": parse_no_sales_tax(terms)}
