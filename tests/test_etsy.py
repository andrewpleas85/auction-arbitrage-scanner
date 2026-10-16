import asyncio

import httpx
import pytest

from clients.etsy import EtsyClient

# Shaped exactly like a real GET /v3/application/listings/active response (2026-10-09)
PHYSICAL = {"listing_id": 4517006305, "title": "Nikon One Touch Zoom 90 Compact 35mm Film Camera",
            "url": "https://www.etsy.com/listing/4517006305/nikon-one-touch-zoom-90", "state": "active", "listing_type": "physical",
            "price": {"amount": 14000, "divisor": 100, "currency_code": "USD"}, "quantity": 1, "num_favorers": 12,
            "original_creation_timestamp": 1791557566, "images": []}
DOWNLOAD = {"listing_id": 1, "title": "Oberheim OB-Xa Exploded View Poster Printable", "url": "https://www.etsy.com/listing/1/x",
            "state": "active", "listing_type": "download", "price": {"amount": 59900, "divisor": 100, "currency_code": "USD"}}
SOLD_OUT = {**PHYSICAL, "listing_id": 2, "state": "sold_out"}


def test_parse_physical_listing():
    l = EtsyClient._parse(PHYSICAL, "vintage camera")
    assert (l.platform, l.listing_id, l.price, l.listing_type) == ("etsy", "4517006305", 140.0, "buy_it_now")
    assert l.url.startswith("https://www.etsy.com/listing/") and l.category == "vintage camera"
    assert l.extra["quantity"] == 1 and l.extra["favorers"] == 12 and l.image_url is None


def test_digital_downloads_and_inactive_listings_are_skipped():
    assert EtsyClient._parse(DOWNLOAD, "q") is None and EtsyClient._parse(SOLD_OUT, "q") is None


def test_images_are_used_when_etsy_returns_them():
    raw = {**PHYSICAL, "images": [{"url_170x135": "https://i.etsystatic.com/x.jpg"}]}
    assert EtsyClient._parse(raw, "q").image_url == "https://i.etsystatic.com/x.jpg"


def _run(handler, key="KEY", secret="SECRET"):
    async def go():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as h:
            c = EtsyClient(h, key, secret)
            return await c.new_listings("vintage camera", 10), c
    return asyncio.run(go())


def test_search_request_uses_keystring_colon_secret_and_filters_results():
    seen = []

    def handler(req):
        seen.append(req)
        return httpx.Response(200, json={"count": 3, "results": [PHYSICAL, DOWNLOAD, SOLD_OUT]})

    out, c = _run(handler)
    assert [l.listing_id for l in out] == ["4517006305"]
    assert seen[0].headers["x-api-key"] == "KEY:SECRET" and seen[0].url.params["sort_on"] == "created"
    assert seen[0].url.path.endswith("/listings/active") and len(seen) == 1


def test_falls_back_to_the_bare_keystring_once_and_remembers_it():
    calls = []

    def handler(req):
        calls.append(req.headers["x-api-key"])
        return httpx.Response(403) if req.headers["x-api-key"] == "KEY:SECRET" else httpx.Response(200, json={"results": [PHYSICAL]})

    out, c = _run(handler)
    assert len(out) == 1 and calls == ["KEY:SECRET", "KEY"] and c._key == "KEY"


def test_a_rejected_key_raises():
    with pytest.raises(httpx.HTTPStatusError):
        _run(lambda req: httpx.Response(403, json={"error": "API key not found or not active"}))


def test_non_usd_listing_is_skipped():
    from clients.etsy import EtsyClient
    raw = {"listing_id": 1, "title": "Vintage synth", "url": "https://etsy.com/x", "state": "active",
           "listing_type": "physical", "price": {"amount": 59900, "divisor": 100, "currency_code": "INR"}}
    assert EtsyClient._parse(raw, "q") is None
    raw["price"]["currency_code"] = "USD"
    assert EtsyClient._parse(raw, "q").price == 599.0
