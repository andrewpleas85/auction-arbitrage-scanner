"""A hand-picked list of auctions to follow: add by URL, see them pinned on the site with a live countdown and your max bid."""
import re
import sqlite3
import time
from datetime import datetime, timezone

import catalog

_URL_RULES = (
    ("propertyroom", re.compile(r"propertyroom\.com/l/[^?#\s]*?/(\d+)(?:[/?#]|$)")),
    ("shopgoodwill", re.compile(r"shopgoodwill\.com/item/(\d+)")),
    ("municibid", re.compile(r"municibid\.com/listing/(\d+)")),
    ("publicsurplus", re.compile(r"publicsurplus\.com/sms/auction/view\?auc=(\d+)")),
    ("gsa", re.compile(r"gsaauctions\.gov/\S*?(\d{5,})")),
)


def parse_listing_url(url: str) -> tuple[str, str] | None:
    """(platform, listing_id) for a PropertyRoom / ShopGoodwill / GSA listing link, else None."""
    for platform, rx in _URL_RULES:
        m = rx.search(url.strip())
        if m:
            return platform, m.group(1)
    return None


def track(conn: sqlite3.Connection, url: str, max_bid: float | None = None, note: str = "") -> tuple[str, str]:
    key = parse_listing_url(url)
    if key is None:
        raise ValueError(f"not a recognised PropertyRoom / ShopGoodwill / GSA listing link: {url}")
    conn.execute("INSERT INTO tracked(platform, listing_id, url, max_bid, note, added_at) VALUES (?,?,?,?,?,?) "
                 "ON CONFLICT(platform, listing_id) DO UPDATE SET url=excluded.url, "
                 "max_bid=COALESCE(excluded.max_bid, tracked.max_bid), note=CASE WHEN excluded.note='' THEN tracked.note ELSE excluded.note END",
                 (*key, url.strip(), max_bid, note, int(time.time())))
    conn.commit()
    return key


def untrack(conn: sqlite3.Connection, url_or_id: str) -> int:
    key = parse_listing_url(url_or_id)
    lid = key[1] if key else url_or_id.strip()
    n = conn.execute("DELETE FROM tracked WHERE listing_id = ?", (lid,)).rowcount
    conn.commit()
    return n


def tracked_rows(conn: sqlite3.Connection, now: datetime | None = None) -> tuple[list[dict], list[sqlite3.Row]]:
    """(site rows for tracked auctions still running with a known price range, tracked entries that could not be shown)."""
    import webpage  # late: webpage imports catalog, not this module
    now = now or datetime.now(timezone.utc)
    want = {(r["platform"], r["listing_id"]): r for r in conn.execute("SELECT * FROM tracked")}
    rows, found = [], set()
    for o in catalog.opportunities(conn, now=now):
        key = (o["row"]["platform"], o["row"]["listing_id"])
        if key not in want:
            continue
        row = webpage.to_row(conn, o, now)
        entry = want[key]
        row.update({"tracked": True, "my_max": entry["max_bid"], "track_note": entry["note"] or ""})
        rows.append(row)
        found.add(key)
    return rows, [e for k, e in want.items() if k not in found]


def merge(rows: list[dict], mine: list[dict]) -> list[dict]:
    """Tracked rows replace their own entry in the main list (keeping their rank) and are added when absent."""
    by_id = {(m["platform"], m["listing_id"]): m for m in mine}
    out = []
    for r in rows:
        m = by_id.pop((r["platform"], r["listing_id"]), None)
        if m is not None:
            m["rank"] = r["rank"]
        out.append(m or r)
    last = max((r.get("rank", 0) for r in rows), default=0)
    for i, m in enumerate(by_id.values(), last + 1):  # not in the profitable list: numbered after it
        m["rank"] = i
        out.append(m)
    return out


def track_kind(conn: sqlite3.Connection, kind: str, now: datetime | None = None) -> int:
    """Track every running priced auction of one item type (e.g. 'tablet'); existing entries keep their max bid and note."""
    n = 0
    for o in catalog.opportunities(conn, now=now):
        r = o["row"]
        if r["kind"] == kind and r["url"]:
            key = parse_listing_url(r["url"])
            if key:
                conn.execute("INSERT OR IGNORE INTO tracked(platform, listing_id, url, max_bid, note, added_at) VALUES (?,?,?,?,?,?)",
                             (*key, r["url"], None, f"all {kind}s", int(time.time())))
                n += 1
    conn.commit()
    return n
