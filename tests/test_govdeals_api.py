from clients.govdeals_api import parse_results

DATA = {"assetSearchResults": [
    {"accountId": 432, "assetId": 42986, "assetShortDescription": "Lot of 10 VoIP Phones", "makebrand": "NEC", "model": "DT800",
     "currentBid": 10.0, "bidCount": 1, "assetAuctionEndDateUtc": "2026-10-09T19:23:00Z", "locationCity": "Hanover",
     "locationState": "MD", "companyName": "Agency", "isSoldAuction": False},
    {"accountId": 1, "assetId": 2, "assetShortDescription": "Sold thing", "currentBid": 5, "isSoldAuction": True},
    {"accountId": 3, "assetId": 4, "assetShortDescription": "No bids yet", "currentBid": None, "assetBidPrice": 25.0}]}


def test_parse_results():
    a, b = parse_results(DATA)
    assert (a.platform, a.listing_id, a.title, a.price, a.location) == ("govdeals", "432-42986", "NEC Lot of 10 VoIP Phones DT800", 10.0, "Hanover, MD")
    assert a.url == "https://www.govdeals.com/asset/42986/432" and a.end_time.isoformat() == "2026-10-09T19:23:00+00:00"
    assert a.extra_cost_pct == 10.0 and a.extra["bids"] == 1
    assert b.price == 25.0  # sold auctions skipped, missing currentBid falls back to the starting price


def test_leaves_follow_mapped_parents():
    from clients.govdeals_api import leaves
    menu = [{"menuId": "29", "children": [{"menuId": "218", "children": []}, {"menuId": "219", "children": [{"menuId": "219a", "children": []}]}]},
            {"menuId": "94C", "children": [{"menuId": "x", "children": []}]},
            {"menuId": "243", "children": []}]
    assert leaves(menu) == {"218": "computers", "219a": "computers", "243": "electronics"}
