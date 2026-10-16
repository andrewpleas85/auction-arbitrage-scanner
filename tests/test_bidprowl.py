import json
import re
import sqlite3
from datetime import datetime, timedelta, timezone

import bidprowl_sync
import db
import webpage
from clients.bidprowl import to_lot

RAW = {"id": "abc", "title": "3 Drones, Yuneec H520, 4 chargers", "category": "electronics", "source_name": "GovDeals", "state": "IL",
       "city": "Chicago", "zip": "60607", "current_bid_cents": 63500, "buy_now_cents": None, "starting_bid_cents": 1000, "num_bids": 0,
       "end_date": "2026-10-15T13:56:00Z", "quantity": 3, "source_url": "https://www.govdeals.com/asset/708/10615",
       "bidprowl_url": "https://bidprowl.com/listing/abc"}


def test_to_lot_converts_cents_and_keeps_the_source_link():
    lot = to_lot(RAW)
    assert lot["bid"] == 635.0 and lot["start_bid"] == 10.0 and lot["buy_now"] is None and lot["bids"] == 0
    assert lot["url"] == "https://www.govdeals.com/asset/708/10615" and lot["source"] == "GovDeals"
    assert to_lot({"id": "x"}) is None


def test_topics():
    assert bidprowl_sync.topic_of("3 Drones, Yuneec H520") == "drone"
    assert bidprowl_sync.topic_of("Dell PowerEdge R730xd Server (Lot of 3)") == "enterprise IT"
    assert bidprowl_sync.topic_of("Milwaukee M18 drill set") == "tools"
    assert bidprowl_sync.topic_of("Wooden storage cabinet") is None


def test_watch_rows_keep_only_upcoming_topical_lots(tmp_path):
    conn = db.connect(str(tmp_path / "w.db"))
    soon = (datetime.now(timezone.utc) + timedelta(days=2)).isoformat()
    past = (datetime.now(timezone.utc) - timedelta(days=1)).isoformat()
    far = (datetime.now(timezone.utc) + timedelta(days=40)).isoformat()
    lots = [to_lot({**RAW, "id": "a", "end_date": soon}), to_lot({**RAW, "id": "b", "end_date": past}),
            to_lot({**RAW, "id": "c", "end_date": far}), to_lot({**RAW, "id": "d", "title": "Wooden cabinet", "category": "", "end_date": soon})]
    bidprowl_sync.save(conn, lots)
    rows = bidprowl_sync.watch_rows(conn)
    assert [r["id"] for r in rows] == ["a"] and rows[0]["topic"] == "drone"


def test_page_embeds_the_watch_list_with_safe_links():
    html = webpage.render([], watch=[{"id": "a", "title": "Evil </script> drone", "topic": "drone", "source": "GovDeals", "bid": 5.0,
                                      "bids": 0, "end": "2026-10-15T00:00:00+00:00", "city": "Chicago", "state": "IL",
                                      "url": "javascript:alert(1)", "quantity": None}])
    data = json.loads(re.search(r'<script id="data" type="application/json">(.*?)</script>', html, re.S).group(1))
    assert data["watch"][0]["title"].startswith("Evil </script>") and not (data["watch"][0]["url"] or "").startswith("javascript")
    assert html.count("</script>") == 2


def test_far_lots_show_only_when_worth_a_drive(tmp_path):
    conn = db.connect(str(tmp_path / "far.db"))
    soon = (datetime.now(timezone.utc) + timedelta(days=2)).isoformat()
    near = to_lot({**RAW, "id": "n1", "title": "Milwaukee M18 drill set", "end_date": soon, "current_bid_cents": 2000})
    far_drone = to_lot({**RAW, "id": "f1", "title": "Aeryon SkyRanger drone & camera", "end_date": soon, "current_bid_cents": 350000})
    far_toy = to_lot({**RAW, "id": "f2", "title": "Beginner drone with camera", "end_date": soon, "current_bid_cents": 1500})
    far_tool = to_lot({**RAW, "id": "f3", "title": "Milwaukee M18 drill set", "end_date": soon, "current_bid_cents": 2000})
    bidprowl_sync.save(conn, [near], "near")
    bidprowl_sync.save(conn, [far_drone, far_toy, far_tool], "far")
    ids = {r["id"]: r for r in bidprowl_sync.watch_rows(conn)}
    assert set(ids) == {"n1", "f1"}            # a $15 toy drone and an unpriced far tool are not worth a drive
    assert ids["f1"]["band"] == "far" and ids["n1"]["band"] == "near"
    # a lot seen both near and far stays 'near'
    bidprowl_sync.save(conn, [{**near, "bid": 25.0}], "far")
    assert {r["id"]: r for r in bidprowl_sync.watch_rows(conn)}["n1"]["band"] == "near"
