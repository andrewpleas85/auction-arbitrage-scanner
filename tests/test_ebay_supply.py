import asyncio
from types import SimpleNamespace
from datetime import datetime, timedelta, timezone

import httpx
import pytest

from clients.ebay import EbayClient

BIN = {"itemId": "v1|111|0", "title": "Tektronix TDS 2024C Oscilloscope", "buyingOptions": ["FIXED_PRICE"],
       "price": {"value": "450.00", "currency": "USD"}, "itemWebUrl": "https://www.ebay.com/itm/111",
       "image": {"imageUrl": "https://i.ebayimg.com/1.jpg"}, "condition": "Used",
       "shippingOptions": [{"shippingCost": {"value": "25.50", "currency": "USD"}, "shippingCostType": "FIXED"}],
       "itemLocation": {"city": "Austin", "stateOrProvince": "TX", "country": "US"},
       "seller": {"feedbackPercentage": "99.5"}}


def _auction(end, bid="120.00", options=("AUCTION",)):
    return {"itemId": "v1|222|0", "title": "Fluke 87V Multimeter", "buyingOptions": list(options),
            "price": {"value": "400.00", "currency": "USD"}, "currentBidPrice": {"value": bid, "currency": "USD"},
            "bidCount": 7, "itemEndDate": end, "itemWebUrl": "https://www.ebay.com/itm/222",
            "shippingOptions": [{"shippingCostType": "CALCULATED"}]}


def test_parse_buy_it_now():
    l = EbayClient.parse_summary(BIN, "oscilloscope")
    assert (l.platform, l.listing_id, l.listing_type, l.price, l.shipping_cost) == (
        "ebay", "v1|111|0", "buy_it_now", 450.0, 25.5)
    assert l.end_time is None and l.location == "Austin, TX" and l.image_url.endswith("1.jpg")
    assert l.extra["condition"] == "Used" and l.category == "oscilloscope"


def test_parse_auction_uses_current_bid_and_end_time():
    end = (datetime.now(timezone.utc) + timedelta(hours=2)).strftime("%Y-%m-%dT%H:%M:%S.000Z")
    l = EbayClient.parse_summary(_auction(end))
    assert (l.listing_type, l.price, l.extra["bid_count"]) == ("auction", 120.0, 7)  # bid, not the 400 buy-now price
    assert l.shipping_cost == 0.0 and l.extra["shipping_cost_type"] == "CALCULATED"
    assert abs((l.end_time - datetime.now(timezone.utc)).total_seconds() - 7200) < 5


def test_parse_auction_with_buy_it_now_option_is_still_an_auction():
    l = EbayClient.parse_summary(_auction("2030-01-01T00:00:00.000Z", options=("AUCTION", "FIXED_PRICE")))
    assert l.listing_type == "auction" and l.price == 120.0


@pytest.mark.parametrize("raw", [
    {**BIN, "price": {"value": "450", "currency": "EUR"}},
    {**BIN, "price": {"currency": "USD"}},
    {**BIN, "price": None},
])
def test_parse_skips_non_usd_or_unpriced(raw):
    assert EbayClient.parse_summary(raw) is None


def _run_search(auction, items, ttl=300, repeat=1):
    seen = {"search": [], "tokens": 0}

    def handler(req):
        if req.url.path.endswith("/token"):
            seen["tokens"] += 1
            return httpx.Response(200, json={"access_token": "tok", "expires_in": 7200})
        seen["search"].append(req)
        return httpx.Response(200, json={"itemSummaries": items})

    async def go():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as h:
            c = EbayClient(h, "id", "secret", False, False)
            for _ in range(repeat):
                out = await c.new_listings("oscilloscope", auction, max_price=1500, ttl=ttl)
            return out

    return asyncio.run(go()), seen


def test_buy_it_now_search_request_shape():
    out, seen = _run_search(False, [BIN])
    req = seen["search"][0]
    f = req.url.params["filter"]
    assert req.headers["authorization"] == "Bearer tok" and req.headers["x-ebay-c-marketplace-id"] == "EBAY_US"
    assert req.url.params["q"] == "oscilloscope" and req.url.params["sort"] == "newlyListed"
    assert "buyingOptions:{FIXED_PRICE}" in f and "deliveryCountry:US" in f and "price:[1..1500]" in f
    assert [l.listing_id for l in out] == ["v1|111|0"]


def test_auction_search_sorts_by_ending_soonest():
    out, seen = _run_search(True, [_auction("2030-01-01T00:00:00.000Z")])
    p = seen["search"][0].url.params
    assert p["sort"] == "endingSoonest" and "buyingOptions:{AUCTION}" in p["filter"]
    assert out[0].listing_type == "auction"


def test_search_results_are_cached_to_protect_the_daily_call_limit():
    _, seen = _run_search(False, [BIN], ttl=300, repeat=3)
    assert len(seen["search"]) == 1 and seen["tokens"] == 1


def test_ebay_source_fails_soft_without_keys(tmp_path, monkeypatch):
    from clients.ebay import MockEbayClient
    from config import Config
    from main import Scanner
    monkeypatch.setenv("DB_PATH", str(tmp_path / "t.db"))
    sc = Scanner.__new__(Scanner)
    sc.cfg, sc.ebay = Config(sources=["ebay"]), MockEbayClient()
    sc.ebay_search = getattr(sc.ebay, "ebay", None)
    sc.govdeals = sc.gsa = sc.propertyroom = SimpleNamespace(fetch=None)  # read eagerly by the fetcher dict
    fetchers = sc._discovery_fetchers()
    assert list(fetchers) == ["ebay"] and asyncio.run(fetchers["ebay"]()) == []
    assert asyncio.run(sc._ending_fetchers()["ebay"]()) == []
