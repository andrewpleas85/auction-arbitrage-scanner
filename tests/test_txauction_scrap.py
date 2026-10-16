import json
from datetime import datetime, timezone

import scrap
import txauction

NOW = datetime(2026, 10, 12, tzinfo=timezone.utc)


def _page(lots, alert="", end="2026-10-15T15:00:00.000Z"):
    st = {"A": {"__typename": "Auction", "auction_id": "99", "title": "T", "public_url": "https://www.txauction.com/auctions/99-t",
                "end_time": end, "front_visible_lots": 1, "alert_text": alert, "terms": "No deposit is required to bid at this auction."}}
    for i, (num, desc, bid, bids) in enumerate(lots):
        st[f"L{i}"] = {"__typename": "AuctionLot", "lot_number": num, "winning_bid_amount": bid, "bid_count": bids, "starting_bid": 5,
                       "end_time": end, "category": {"name": "c"}, "description_plain": desc}
    return "<script>window.__APOLLO_STATE__ = " + json.dumps(st) + ";</script>"


def test_parse_page_reads_lots_and_premium_flags():
    page = _page([("10", "HANDBAG: [1] Gucci tote", 200.0, 3), ("9", "SHOES: [1] Nike", None, 0)],
                 alert="<p>There is <u>not</u> a buyer's premium on this auction.</p>")
    auction, lots = txauction.parse_page(page)
    assert auction["id"] == "99" and auction["no_premium"] and auction["no_deposit"]
    assert [l["lot"] for l in lots] == ["9", "10"]  # numeric order
    assert lots[1]["bid"] == 200.0 and lots[0]["bid"] is None


def test_watch_rows_skip_shoes_and_ended_keep_bags_and_use_start_bid_when_unbid():
    page = _page([("1", "HANDBAG: [1] Chanel flap", None, 0), ("2", "[6] SHOES: (1) Pair of Nike Air Jordan", 285.0, 1),
                  ("3", "LUGGAGE: [1] Louis Vuitton Pegase", 180.0, 6)])
    auction, lots = txauction.parse_page(page)
    rows = txauction.watch_rows(auction, lots, NOW)
    assert [r["topic"] for r in rows] == ["handbag", "handbag"]
    assert rows[0]["bid"] == 5 and rows[0]["band"] == "ships" and rows[0]["state"] == "TX"
    assert txauction.watch_rows(auction, lots, datetime(2026, 10, 16, tzinfo=timezone.utc)) == []


def test_scrap_economics_matches_hand_math():
    auc = {"buyer_premium": 0.10, "ship_in": 15.0}
    lot = {"lot": 1, "bid": 100.0, "bids": 0, "gold": [[14, 10.0]]}
    e = scrap.economics(lot, auc, {"gold": 3110.35, "silver": 30}, payout=0.85, metal_share=1.0, refiner_ship=10)
    assert e["melt"] == round(10 * 0.585 * 100, 2) == 585.0
    assert e["proceeds"] == round(585 * 0.85 - 10, 2)
    assert e["cost"] == 125.0 and e["passes"]
    at_max = scrap.economics(lot, auc, {"gold": 3110.35, "silver": 30}, 0.85, 1.0, 10, bid=e["max_bid"])
    assert abs(at_max["roi"] - 0.30) < 0.01  # max bid lands on the 30% rule


def test_apply_snapshot_updates_bids_and_end():
    auctions = scrap.load("scrap_lots.json")
    snap = {"fetched_at": 1.0, "auctions": [{"auction": {"id": "31543", "end": "2026-10-13T18:30:00.000Z"},
                                             "lots": [{"lot": "308", "bid": 120.0, "bids": 2, "start": 5}]}]}
    out = scrap.apply_snapshot(auctions, snap)
    lot = next(l for l in out[0]["lots"] if l["lot"] == 308)
    assert lot["bid"] == 120.0 and lot["bids"] == 2 and out[0]["end"].startswith("2026-10-13")
    assert next(l for l in out[0]["lots"] if l["lot"] == 300)["bid"] == 650  # not in the snapshot: unchanged


def test_artwork_lots_get_an_art_topic():
    auction, lots = txauction.parse_page(_page([("1", "ARTWORK: [1] Salvador Dali lithograph, signed", 900.0, 4)]))
    assert [r["topic"] for r in txauction.watch_rows(auction, lots, NOW)] == ["art"]


def test_discover_finds_new_wanted_auctions_only():
    page = ('<a href="/auctions/31520-us-marshals-service-designer-goods-auction">a</a>'
            '<a href="/auctions/31600-us-marshals-service-handbag-auction">b</a>'
            '<a href="/auctions/31601-eagle-towing-online-vehicle-auction">c</a>'
            '<a href="/auctions/31602-us-marshals-service-wine-auction">d</a>')
    assert txauction.discover(page, {"31520"}) == ["https://www.txauction.com/auctions/31600-us-marshals-service-handbag-auction"]


def test_more_pages_and_merge():
    assert txauction.more_pages({"visible_lots": 84}, 50) == 1
    assert txauction.more_pages({"visible_lots": 195}, 50) == 3
    assert txauction.more_pages({"visible_lots": 41}, 41) == 0
    assert txauction.more_pages({"visible_lots": 5000}, 50) == txauction.MAX_PAGES - 1
    a = [{"lot": "2"}, {"lot": "10"}]
    b = [{"lot": "10"}, {"lot": "9"}]
    assert [l["lot"] for l in txauction.merge_lots(a, b)] == ["2", "9", "10"]


def test_rocks_are_not_watched_or_discovered():
    auction, lots = txauction.parse_page(_page([("1", "SPECIMEN: [1] Pink amethyst quartz geode specimen. 177.6 grams", 135.0, 1)]))
    assert txauction.watch_rows(auction, lots, NOW) == []
    assert txauction.discover('<a href="/auctions/31522-us-marshals-service-crystals-auction">x</a>', set()) == []


def test_jewelry_estimate_gold_plus_diamonds():
    import jewelry
    desc = "RING: 14k yellow gold ring, size 7; (8) round diamonds, estimated 1.00 carats, I-J/SI2; 10.0 grams."
    e = jewelry.estimate(desc, 3110.35)  # $100 per gram of fine gold
    assert e["karat"] == 14 and e["cttw"] == 1.0
    gold = (10.0 - 0.2) * 0.9 * 0.585 * 100 * 0.9
    assert abs(e["gold"] - gold) < 0.6 and abs(e["mid"] - (gold + 120)) < 0.6
    assert jewelry.estimate("NECKLACE: 10k gold. 5 grams. RING: 14k gold. 3 grams", 4000) is None  # two items


def test_snapshot_watch_rows_attach_jewelry_value():
    page = _page([("5", "RING: 14k yellow gold ring, estimated 1.00 carats total weight; 10.0 grams.", 100.0, 1)])
    auction, lots = txauction.parse_page(page)
    snap = {"auctions": [{"auction": auction, "lots": lots}]}
    rows = txauction.snapshot_watch_rows(snap, NOW, values_path="/nonexistent.json", gold_spot=3110.35)
    assert rows and rows[0]["topic"] == "jewelry" and rows[0]["value"] > rows[0]["low"] and rows[0]["high"] > rows[0]["value"]


def test_coins_are_not_labelled_jewelry():
    auction, lots = txauction.parse_page(_page([("1", "COIN: [1] 1978 South African Krugerrand 1 ounce fine gold coin.", 4140.0, 31),
                                                ("2", "NECKLACE: [1] 10k gold necklace. 5 grams.", 100.0, 1)]))
    assert [r["topic"] for r in txauction.watch_rows(auction, lots, NOW)] == ["coins", "jewelry"]


def test_tx_rows_build_main_table_rows_with_the_standard_profit_model(tmp_path):
    import webpage
    vals = tmp_path / "v.json"
    vals.write_text(json.dumps({"99-5": {"value": 3000, "low": 2400, "high": 3600, "basis": "test"}}))
    page = _page([("5", "HANDBAG: [1] Chanel Classic flap", 800.0, 10), ("6", "RING: 14k yellow gold ring; (8) round diamonds, estimated 1.00 carats; 10.0 grams.", 200.0, 1)])
    auction, lots = txauction.parse_page(page)
    rows = webpage.tx_rows({"auctions": [{"auction": auction, "lots": lots}]}, None, 3110.35, NOW, vals)
    assert {r["kind"] for r in rows} == {"bag", "jewelry"}
    bag = next(r for r in rows if r["kind"] == "bag")
    assert bag["platform"] == "txauction" and bag["value"]["typ"] == 3000 and bag["econ"]["prem"] == 0.0
    assert bag["ends_at"] == round(0.80 * 3000, 2) and "max_bid" in bag and "profit_end" in bag


def test_tx_rows_scrap_uses_melt_and_no_ebay_fee():
    import webpage
    a = {"id": "txauction-99", "premium": 0.10, "url": "https://www.txauction.com/auctions/99-t", "end": "2026-10-15T15:00:00.000Z",
         "spot": {"gold": 3110.35, "silver": 30.0}, "lots": [{"lot": 1, "title": "14KT RING, 10 GRAMS", "bid": 100.0, "bids": 0,
                                                              "fine_gold": 5.85, "fine_silver": 0.0, "bullion": False}]}
    rows = webpage.tx_rows({"auctions": []}, [a], 3110.35, NOW)
    r = rows[0]
    assert r["kind"] == "scrap" and r["econ"]["fee"] == 0.0 and r["econ"]["prem"] == 0.10
    assert abs(r["value"]["typ"] - 5.85 * 100 * 0.9 * 0.85) < 0.1


def test_lot_links_point_at_the_lot_not_the_auction():
    st = {"A": {"__typename": "Auction", "auction_id": "99", "title": "T", "public_url": "https://www.txauction.com/auctions/99-t",
                "end_time": "2026-10-15T15:00:00.000Z", "alert_text": "", "terms": ""},
          "L": {"__typename": "AuctionLot", "auction_lot_id": "5001", "lot_number": "31", "winning_bid_amount": 50, "bid_count": 1,
                "end_time": "2026-10-15T15:00:00.000Z", "category": {"name": "c"}, "description_plain": "HANDBAG: [1] Gucci tote"}}
    auction, lots = txauction.parse_page("<script>window.__APOLLO_STATE__ = " + json.dumps(st) + ";</script>")
    assert txauction.lot_url("99", lots[0]) == "https://www.txauction.com/auctions/99/lot/5001-lot-31"
    assert txauction.watch_rows(auction, lots, NOW)[0]["url"] == "https://www.txauction.com/auctions/99/lot/5001-lot-31"


def test_sync_refreshes_auctions_found_by_discovery(tmp_path):
    import asyncio
    old_url = "https://www.txauction.com/auctions/777-us-marshals-service-handbag-auction"
    snap = tmp_path / "snap.json"
    snap.write_text(json.dumps({"fetched_at": 0, "auctions": [{"auction": {"id": "777", "url": old_url, "end": "2999-01-01T00:00:00Z"}, "lots": []}]}))
    fetched = []

    class FakeResp:
        status_code = 200
        text = _page([("1", "HANDBAG: [1] Gucci tote", 10.0, 1)], end="2999-01-01T00:00:00.000Z").replace('"auction_id": "99"', '"auction_id": "777"')
        def raise_for_status(self): pass

    class FakeHttp:
        async def get(self, url, **kw):
            fetched.append(url)
            return FakeResp()

    asyncio.run(txauction.sync(FakeHttp(), urls=[], path=snap, pause=0, find_new=False))
    assert old_url in fetched


def test_unvalued_tx_lots_get_a_main_table_row_marked_noval():
    import webpage
    page = _page([("7", "WATCH: [1] Stainless steel Omega Speedmaster watch, automatic", 2000.0, 12),
                  ("8", "ARTWORK: [1] Salvador Dali lithograph, signed", 900.0, 4)])
    auction, lots = txauction.parse_page(page)
    rows = webpage.tx_rows({"auctions": [{"auction": auction, "lots": lots}]}, None, 4000.0, NOW, "/nonexistent.json")
    assert {r["kind"] for r in rows} == {"watch", "art"} and all(r["noval"] for r in rows)
    assert all(r["platform"] == "txauction" and r["value"]["typ"] == 0.0 for r in rows)


def test_hidden_lots_stay_off_the_page(tmp_path):
    import webpage
    page = _page([("1", "HANDBAG: [1] Gucci tote", 100.0, 1), ("2", "WATCH: [1] Kenneth Cole quartz watch", 5.0, 0)])
    auction, lots = txauction.parse_page(page)
    snap = {"auctions": [{"auction": auction, "lots": lots}]}
    hid = tmp_path / "h.json"
    hid.write_text(json.dumps({"hidden": ["99-2"]}))
    rows = webpage.tx_rows(snap, None, 4000.0, NOW, "/nonexistent.json", hidden_path=hid)
    assert [r["listing_id"] for r in rows] == ["99-1"]


def test_tracked_tx_lots_are_marked_and_keep_their_note(tmp_path):
    import webpage
    page = _page([("1", "HANDBAG: [1] Prada satchel", 310.0, 3)])
    auction, lots = txauction.parse_page(page)
    tr = tmp_path / "t.json"
    tr.write_text(json.dumps({"tracked": {"99-1": {"max_bid": 500, "note": "watching"}}}))
    rows = webpage.tx_rows({"auctions": [{"auction": auction, "lots": lots}]}, None, 4000.0, NOW, "/nonexistent.json", tracked_path=tr)
    assert rows[0]["tracked"] is True and rows[0]["my_max"] == 500 and rows[0]["track_note"] == "watching"


def test_trading_cards_are_not_listed():
    page = _page([("1", "COLLECTIBLE: [1] 2022 Pokemon Swsh #212 Giratina Vstar PSA Gem Mt 10 #68090408", 458.0, 31),
                  ("2", "COLLECTIBLES: [500] Assorted Baseball cards; Topps, Upper Deck, Fleer", 45.0, 3),
                  ("3", "HANDBAG: [1] Gucci tote", 100.0, 1)])
    auction, lots = txauction.parse_page(page)
    assert [r["topic"] for r in txauction.watch_rows(auction, lots, NOW)] == ["handbag"]
