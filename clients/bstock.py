"""B-Stock / liquidation lot loader.

B-Stock has no public API and its manifests sit behind a buyer login, so this loads
manifests you export yourself into BSTOCK_MANIFEST_DIR (non-recursive):

  <lot>.json  {"lot_id","title","opening_bid","shipping","url","location","end_time",
               "image_url","items":[{"title":"...","qty":3}, ...]}
  <lot>.csv   columns: title (or description/item/product), qty (or quantity/units)
              + sibling <lot>.meta.json with the lot-level fields above (no "items")
"""
import csv
import json
import logging
from datetime import datetime, timezone
from pathlib import Path

from models import Listing

log = logging.getLogger("arbitrage.bstock")
_TITLE_COLS = ("title", "description", "item", "product", "item description")
_QTY_COLS = ("qty", "quantity", "units")


def _read_csv(path: Path) -> list[dict]:
    items = []
    with path.open(newline="", encoding="utf-8-sig") as f:
        for row in csv.DictReader(f):
            row = {(k or "").strip().lower(): (v or "").strip() for k, v in row.items()}
            title = next((row[c] for c in _TITLE_COLS if row.get(c)), "")
            qty = next((row[c] for c in _QTY_COLS if row.get(c)), "1")
            if title:
                items.append({"title": title, "qty": max(1, int(float(qty or 1)))})
    return items


def _to_listing(meta: dict, items: list[dict], premium_pct: float, stem: str) -> Listing:
    end = None
    if meta.get("end_time"):
        end = datetime.fromisoformat(meta["end_time"])
        end = end if end.tzinfo else end.replace(tzinfo=timezone.utc)
    units = sum(i["qty"] for i in items)
    return Listing(
        platform="bstock",
        listing_id=str(meta.get("lot_id") or stem),
        title=f"{meta.get('title', stem)} ({units} units)",
        price=float(meta["opening_bid"]),
        shipping_cost=float(meta.get("shipping", 0)),
        url=meta.get("url", ""),
        image_url=meta.get("image_url"),
        category="liquidation lot",
        listing_type="auction",
        end_time=end,
        location=meta.get("location"),
        lot_items=items,
        extra_cost_pct=premium_pct,
    )


def load_lots(manifest_dir: str, premium_pct: float = 10) -> list[Listing]:
    root = Path(manifest_dir)
    if not root.is_dir():
        return []
    lots: list[Listing] = []
    for path in sorted(root.glob("*.json")):
        if path.name.endswith(".meta.json"):
            continue
        try:
            meta = json.loads(path.read_text())
            lots.append(_to_listing(meta, meta["items"], premium_pct, path.stem))
        except (KeyError, ValueError, OSError) as e:
            log.warning("skipping bad manifest %s: %s", path.name, e)
    for path in sorted(root.glob("*.csv")):
        meta_path = path.with_suffix(".meta.json")
        try:
            meta = json.loads(meta_path.read_text())
            lots.append(_to_listing(meta, _read_csv(path), premium_pct, path.stem))
        except (KeyError, ValueError, OSError) as e:
            log.warning("skipping CSV manifest %s (needs %s): %s", path.name, meta_path.name, e)
    return lots
