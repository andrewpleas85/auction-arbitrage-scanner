"""Keep a local copy of nearby BidProwl lots in `bidprowl_lots` and describe what is worth watching.

These lots are NOT priced (no sold range yet). They feed the page's watch list so nothing interesting near Chicago is missed;
pricing happens later, one lookup at a time, for the ones that look worth it.
"""
import re
import sqlite3
import time
from datetime import datetime, timezone

from clients.bidprowl import BidProwlClient

# categories pulled in full (BidProwl category slugs) plus keyword searches for things categories hide
CATEGORIES = ("electronics", "tools-industrial", "medical-scientific", "sporting-outdoor")
KEYWORDS = ("drone", "quadcopter", "server", "switch", "laptop", "camera")
TOPICS = (
    ("drone", re.compile(r"\b(drones?|quadcopters?|hexacopters?|uav|multirotor|dji|yuneec|autel|mavic|phantom)\b", re.I)),
    ("enterprise IT", re.compile(r"\b(servers?|poweredge|proliant|switch(?:es)?|catalyst|cisco|aruba|juniper|netapp|powervault|md\d{4}|"
                                 r"storage (?:array|enclosure|system|expansion|shelf)|router|firewall|fortinet|palo alto|ups\b|rack|nas\b|ssd|gpu|nvidia|raid|laptops?|desktops?|"
                                 r"workstation|thinkpad|latitude|elitebook|macbook|ipad|chromebook)\b", re.I)),
    ("tools", re.compile(r"\b(milwaukee|dewalt|makita|snap[- ]?on|festool|hilti|crimper|welder|compressor|drill|saw|grinder|"
                         r"torque|impact|jack|lathe|mill|press|generator|pressure washer|tool ?box|socket|wrench)\b", re.I)),
    ("electronics", re.compile(r"\b(camera|projector|printer|scanner|monitor|tv\b|television|radio|phone|tablet|speaker|audio|"
                               r"amplifier|microscope|oscilloscope|multimeter|meter)\b", re.I)),
)


def topic_of(title: str, category: str = "") -> str | None:
    for name, rx in TOPICS:
        if rx.search(title):
            return name
    return {"electronics": "electronics", "tools-industrial": "tools"}.get(category)


def save(conn: sqlite3.Connection, lots: list[dict], band: str = "near") -> int:
    now = int(time.time())
    conn.executemany(
        """INSERT INTO bidprowl_lots (id, source, title, category, state, city, zip, bid, buy_now, start_bid, bids, end_date,
                                      quantity, url, bidprowl_url, condition, first_seen, last_seen, band)
           VALUES (:id,:source,:title,:category,:state,:city,:zip,:bid,:buy_now,:start_bid,:bids,:end_date,:quantity,:url,
                   :bidprowl_url,:condition,:now,:now,:band)
           ON CONFLICT(id) DO UPDATE SET bid=excluded.bid, bids=excluded.bids, end_date=excluded.end_date, title=excluded.title,
                                         last_seen=excluded.last_seen, buy_now=excluded.buy_now,
                                         band=CASE WHEN bidprowl_lots.band='near' THEN 'near' ELSE excluded.band END""",
        [{**lot, "now": now, "band": band} for lot in lots])
    conn.commit()
    return len(lots)


def last_sync(conn: sqlite3.Connection) -> int:
    r = conn.execute("SELECT MAX(last_seen) FROM bidprowl_lots").fetchone()
    return int(r[0] or 0)


async def _pull(client: BidProwlClient, zip_code: str, radius: int, pages: int) -> dict[str, dict]:
    seen: dict[str, dict] = {}
    for cat in CATEGORIES:
        for lot in await client.search(zip_code, radius, category=cat, pages=pages):
            seen[lot["id"]] = lot
    for kw in KEYWORDS:
        for lot in await client.search(zip_code, radius, q=kw, pages=1):
            seen[lot["id"]] = lot
    return seen


async def sync(conn: sqlite3.Connection, client: BidProwlClient, zip_code: str, radius: int = 25, pages: int = 3,
               far_radius: int | None = None) -> dict:
    """Pull lots within `radius` miles (band 'near'), and, when `far_radius` is larger, lots out to that distance (band 'far').

    The far pass keeps only the high-value families (drones, enterprise IT, tools): two bands cost about twice the calls."""
    near = await _pull(client, zip_code, radius, pages)
    save(conn, list(near.values()), "near")
    far_n = 0
    if far_radius and far_radius > radius:
        far = await _pull(client, zip_code, far_radius, 2)
        far_lots = [l for i, l in far.items() if i not in near and topic_of(l["title"], l["category"]) in ("drone", "enterprise IT", "tools")]
        save(conn, far_lots, "far")
        far_n = len(far_lots)
    return {"lots": len(near), "far": far_n, "calls": client.calls}


FAR_MIN_VALUE = 150.0  # a far lot needs a known resale value at least this high, or to be a drone / enterprise IT lot


def _worth_a_drive(conn: sqlite3.Connection, title: str, topic: str, bid: float) -> tuple[bool, float | None]:
    """(show a 25-100 mile lot?, known typical resale value). Known value >= FAR_MIN_VALUE, or a drone / enterprise IT lot with a
    real bid (>= $50: skips toys and single accessories). Heuristic, not a profit estimate."""
    import catalog
    rng = catalog.model_range(conn, title)
    value = rng.typical if rng is not None and rng.typical else None
    if value is not None and value >= FAR_MIN_VALUE:
        return True, value
    return (topic in ("drone", "enterprise IT") and bid >= 50), value


def watch_rows(conn: sqlite3.Connection, now: datetime | None = None, max_days: float = 10, limit: int = 250) -> list[dict]:
    """Unpriced lots closing within `max_days`, soonest first, with a topic tag. Near lots always; far lots only if worth a drive."""
    now = now or datetime.now(timezone.utc)
    rows = []
    for r in conn.execute("SELECT * FROM bidprowl_lots ORDER BY end_date").fetchall():
        try:
            end = datetime.fromisoformat((r["end_date"] or "").replace("Z", "+00:00"))
        except ValueError:
            continue
        if end.tzinfo is None:
            end = end.replace(tzinfo=timezone.utc)
        if end <= now or (end - now).total_seconds() > max_days * 86400:
            continue
        topic = topic_of(r["title"], r["category"])
        if topic is None:
            continue
        band = r["band"] if "band" in r.keys() and r["band"] else "near"
        value = None
        if band == "far":
            ok, value = _worth_a_drive(conn, r["title"], topic, r["bid"] or 0)
            if not ok:
                continue
        rows.append({"band": band, "value": value, "id": r["id"], "title": r["title"], "source": r["source"], "topic": topic, "category": r["category"],
                     "bid": r["bid"], "bids": r["bids"], "end": end.isoformat(), "city": r["city"], "state": r["state"],
                     "url": r["url"], "quantity": r["quantity"], "condition": r["condition"]})
        if len(rows) >= limit:
            break
    return rows
