"""Where is the money? One table across ALL running auctions (not just the top rows), so we do not over-focus on one product line.

For every item kind (and, within it, brand) among auctions closing inside the page window it reports how many auctions exist, how
many have a price at all, how many rest on a real SOLD price (not a guess), how many clear the page rules at the expected closing
price, and the profit they would add up to. A kind with many auctions but little sold-price coverage is a research gap, not a verdict.
"""
import sqlite3
import statistics
from collections import defaultdict
from datetime import datetime, timezone

import catalog
import webpage


def build(conn: sqlite3.Connection, now: datetime | None = None, max_days: float | None = None) -> list[dict]:
    now = now or datetime.now(timezone.utc)
    horizon = (max_days if max_days is not None else webpage.MAX_DAYS_LEFT) * 86400
    stat: dict[tuple, dict] = defaultdict(lambda: {"running": 0, "priced": 0, "sold": 0, "pass": 0, "profits": [], "total": 0.0})
    total_running = defaultdict(int)
    for g in catalog.groups(conn, now=now):  # every running auction group (priced or not)
        pass
    rows = conn.execute("SELECT * FROM catalog WHERE listing_type IN ('auction','daily_auction')").fetchall()
    for r in rows:
        end = catalog._end(r)
        if end is not None and (end <= now or (end - now).total_seconds() > horizon):
            continue
        if r["platform"] in webpage.LOCAL_PICKUP_PLATFORMS and not webpage._gsa_near_chicago(r):
            continue
        if r["kind"] in ("accessory", "other"):
            continue
        total_running[(r["kind"], r["brand"])] += 1
    for o in catalog.opportunities(conn, now=now):
        r = o["row"]
        end = o["end"]
        if end is not None and (end - now).total_seconds() > horizon:
            continue
        if r["platform"] in webpage.LOCAL_PICKUP_PLATFORMS and not webpage._gsa_near_chicago(r):
            continue
        if r["kind"] in ("accessory", "other"):
            continue
        row = webpage.to_row(conn, o, now)
        s = stat[(r["kind"], r["brand"])]
        s["priced"] += 1
        if any("model-specific" in n for n in row["range_notes"]):
            s["sold"] += 1
        if row["profit_end"] >= webpage.MIN_PROFIT and row["roi_end"] >= webpage.MIN_ROI:
            s["pass"] += 1
            s["profits"].append(row["profit_end"])
            s["total"] += row["profit_end"]
    out = []
    for key, n in total_running.items():
        s = stat.get(key, {"priced": 0, "sold": 0, "pass": 0, "profits": [], "total": 0.0})
        out.append({"kind": key[0], "brand": key[1], "running": n, "priced": s["priced"], "sold": s["sold"], "pass": s["pass"],
                    "median_profit": statistics.median(s["profits"]) if s["profits"] else 0.0, "total": s["total"]})
    return sorted(out, key=lambda x: (-x["total"], -x["running"]))


def by_kind(rows: list[dict]) -> list[dict]:
    agg: dict[str, dict] = {}
    for r in rows:
        a = agg.setdefault(r["kind"], {"kind": r["kind"], "running": 0, "priced": 0, "sold": 0, "pass": 0, "total": 0.0, "brands": 0})
        for k in ("running", "priced", "sold", "pass", "total"):
            a[k] += r[k]
        a["brands"] += 1
    return sorted(agg.values(), key=lambda a: (-a["total"], -a["running"]))


def format_table(rows: list[dict], title: str, label) -> str:
    lines = [title, f"{'':<22}{'RUNNING':>8}{'PRICED':>8}{'SOLD-BACKED':>12}{'PASS 30%':>9}{'PROFIT SUM':>11}"]
    for r in rows:
        lines.append(f"{label(r):<22}{r['running']:>8}{r['priced']:>8}{r['sold']:>12}{r['pass']:>9}{r['total']:>10.0f}$")
    return "\n".join(lines)
