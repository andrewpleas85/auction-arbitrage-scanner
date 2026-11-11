"""CWS Marketing / Treasury auction CALENDAR (discovery only): the public marketing site cwsmarketing.com lists upcoming government sales.

bid.cwsmarketing.com (the catalog with lots and bids) answers automated clients with a CloudFront 403, and bots are detected there, so it is covered by
Muse's browser as a feeder source. The marketing site is plain WordPress HTML: each upcoming sale shows its date, a description and a 'View Auction' link
to the catalog id. This client reads that ONE page, politely, and turns the non-real-estate sales into discovery leads (never priced). Real-estate sales
are skipped. Checked 2026-11-11: robots.txt does not disallow the home page.
"""
from __future__ import annotations

import html as htmllib
import re

import httpx

HOME = "https://www.cwsmarketing.com/"
UA = "arbitrage-scanner/1.0 (personal price lookup)"
_ANCHOR = re.compile(r'<a[^>]+href="([^"]+/auctions/catalog/id/(\d+))"[^>]*>\s*View Auction\s*</a>', re.I)
_DATE = re.compile(r"\b((?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Sept|Oct|Nov|Dec)[a-z]* \d{1,2} \d{4})\b")
_REAL_ESTATE = re.compile(r"real estate|\bacre\b|sq\. ?ft|bedrooms?|\bhome with\b|vacant|\bcondo\b|\bparcel\b|\bland with\b", re.I)


def _text(fragment: str) -> str:
    t = re.sub(r"<script.*?</script>|<style.*?</style>", " ", fragment, flags=re.S | re.I)
    return re.sub(r"\s+", " ", htmllib.unescape(re.sub(r"<[^>]+>", " ", t))).strip()


def parse_home(page: str) -> list[dict]:
    """[{'catalog_id', 'url', 'date', 'description', 'real_estate'}] for every 'View Auction' card, in page order."""
    body = page[page.find("<body"):] if "<body" in page else page
    out, prev = [], 0
    for m in _ANCHOR.finditer(body):
        seg = _text(body[prev:m.start()])
        prev = m.end()
        dates = list(_DATE.finditer(seg))
        if not dates:
            continue
        last = dates[-1]
        desc = seg[last.end():].strip(" -–")
        out.append({"catalog_id": m.group(2), "url": m.group(1), "date": last.group(1), "description": desc, "real_estate": bool(_REAL_ESTATE.search(desc))})
    return out


def to_leads(cards: list[dict]) -> list[dict]:
    """Discovery leads for the NON-real-estate sales: the catalog stays Muse-fed; this only says what is coming and when."""
    leads = []
    for c in cards:
        if c["real_estate"]:
            continue
        first = re.split(r"(?<=[.!?]) ", c["description"], maxsplit=1)[0]
        leads.append({"source": "cws_treasury", "lead_id": f"cws-{c['catalog_id']}", "title": first[:140] or f"CWS sale {c['catalog_id']}", "url": c["url"], "dates": c["date"],
                      "company": "CWS Marketing (Treasury / government sale)", "notes": c["description"][:300]})
    return leads


async def sync(conn, http: httpx.AsyncClient) -> dict:
    import lot_sources
    r = await http.get(HOME, headers={"User-Agent": UA}, timeout=30, follow_redirects=True)
    r.raise_for_status()
    cards = parse_home(r.text)
    leads = to_leads(cards)
    res = lot_sources.import_data(conn, {"sales": [], "leads": leads})
    return {"cards": len(cards), "real_estate_skipped": sum(1 for c in cards if c["real_estate"]), "leads": res["leads"]}
