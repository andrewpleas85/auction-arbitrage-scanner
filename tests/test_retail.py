import asyncio
from datetime import datetime, timezone

import httpx
import pytest

import db
from alerting.discord import CRITICAL, build_embed
from clients.ebay import Comp, Comps
from clients.retail import BestBuyClient, model_matches
from config import Config
from engine.valuation import ValuationEngine
from models import Listing

run = asyncio.run


@pytest.mark.parametrize("a,b,ok", [
    ("WH-1000XM5", "WH1000XM5/B", True), ("wh-1000xm5", "WH-1000XM4", False),
    ("S918U", "SM-S918UZKAXAA", True), ("2230", "2230", True), ("2230", "22305", False),
    ("a1", "a1b2c3", False), ("", "X", False),
])
def test_model_matches(a, b, ok):
    assert model_matches(a, b) is ok


def P(model, price, name="Sony WH-1000XM5 Wireless Headphones", cond="new", **kw):
    return {"modelNumber": model, "salePrice": price, "regularPrice": price + 50, "name": name,
            "condition": cond, "url": "https://bby/x", **kw}


def test_parse_takes_cheapest_new_exact_model_with_matching_brand():
    products = [P("WH1000XM5/B", 399.99), P("WH1000XM5/S", 349.99), P("WH1000XM5/B", 200.0, cond="refurbished"),
                P("WH1000XM4/B", 250.0), P("WH1000XM5/B", 100.0, name="Bose Wireless Headphones")]
    r = BestBuyClient.parse(products, "wh-1000xm5", "sony")
    assert (r.sale_price, r.model) == (349.99, "WH1000XM5/S")
    assert BestBuyClient.parse([P("WH1000XM4/B", 250.0)], "wh-1000xm5", "sony") is None
    assert BestBuyClient.parse([], "wh-1000xm5", None) is None


def _client(responses, key="k"):
    seen = []

    def handler(req):
        seen.append(req)
        status, body = responses.pop(0) if responses else (200, {"products": []})
        return httpx.Response(status, json=body)

    http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return BestBuyClient(http, key), seen, http


def test_lookup_request_shape_and_match():
    async def go():
        c, seen, http = _client([(200, {"products": [P("WH1000XM5/B", 349.99)]})])
        async with http:
            return await c.lookup("Sony WH-1000XM5 headphones black"), seen

    r, seen = run(go())
    assert r.sale_price == 349.99 and len(seen) == 1
    assert "/v1/products(modelNumber=wh-1000xm5)" in str(seen[0].url).replace("%3D", "=")
    p = seen[0].url.params
    assert p["apiKey"] == "k" and p["format"] == "json" and "salePrice" in p["show"]


def test_lookup_falls_back_to_prefix_search_then_caches_the_miss():
    async def go():
        c, seen, http = _client([(200, {"products": []}), (200, {"products": [P("WH1000XM5/B", 349.99)]})])
        async with http:
            first = await c.lookup("Sony WH-1000XM5 headphones")
            second = await c.lookup("Sony WH-1000XM5 headphones")  # cached
            return first, second, seen

    first, second, seen = run(go())
    assert first.sale_price == 349.99 and second is first
    assert len(seen) == 2 and "modelNumber=wh-1000xm5*" in str(seen[1].url).replace("%3D", "=").replace("%2A", "*")


def test_lookup_skips_titles_without_a_model_number():
    async def go():
        c, seen, http = _client([])
        async with http:
            return await c.lookup("Vintage black headphones lot"), seen
    r, seen = run(go())
    assert r is None and seen == []


def test_403_disables_the_client_instead_of_hammering_the_api():
    async def go():
        c, seen, http = _client([(403, {})])
        async with http:
            a = await c.lookup("Sony WH-1000XM5")
            b = await c.lookup("Canon EOS-R6 body")
            return a, b, c.disabled_reason, seen
    a, b, reason, seen = run(go())
    assert a is None and b is None and "403" in reason and len(seen) == 1


# ---------------- the cap, inside the scanner
class FixedComps:
    async def comps(self, q, limit=50):
        return Comps("sold", [Comp("Sony WH-1000XM5 headphones", 500.0, datetime.now(timezone.utc)) for _ in range(6)])


class StubRetail:
    def __init__(self, price):
        self.price = price

    async def lookup(self, title):
        from clients.retail import RetailPrice
        return RetailPrice("Sony WH-1000XM5", "WH1000XM5/B", self.price, self.price + 50, "https://bby/x")


def _scanner(tmp_path, monkeypatch, retail_price):
    from main import Scanner
    monkeypatch.setenv("DB_PATH", str(tmp_path / "t.db"))
    sc = Scanner.__new__(Scanner)
    sc.cfg, sc.conn = Config(min_market_value=0), db.connect(str(tmp_path / "t.db"))
    sc.engine, sc.ebay, sc._comp_cache, sc.dry_run = ValuationEngine(sc.cfg), FixedComps(), {}, True
    sc._ebay_gate, sc._warned_no_sold, sc.retail = asyncio.Semaphore(2), False, StubRetail(retail_price)
    return sc


def _l(price=100.0, title="Sony WH-1000XM5 headphones"):
    return Listing("reverb", "1", title, price, 0, "u", None, "c")


def test_estimate_is_capped_at_a_fraction_of_new_retail(tmp_path, monkeypatch):
    sc = _scanner(tmp_path, monkeypatch, retail_price=400.0)
    l = _l()
    run(sc._process(l))
    row = sc.conn.execute("SELECT estimated_market_value, retail_price, status FROM listings").fetchone()
    assert row["estimated_market_value"] == pytest.approx(400 * 0.85) and row["retail_price"] == 400.0
    assert l.extra["retail"]["price"] == 400.0


def test_cap_can_remove_a_deal_that_only_worked_on_inflated_comps(tmp_path, monkeypatch):
    sc = _scanner(tmp_path, monkeypatch, retail_price=150.0)  # new costs $150, comps said $500
    run(sc._process(_l(price=100.0)))
    row = sc.conn.execute("SELECT status, estimated_market_value FROM listings").fetchone()
    assert row["status"] == "skipped" and row["estimated_market_value"] == pytest.approx(150 * 0.85)


def test_high_retail_leaves_the_estimate_alone(tmp_path, monkeypatch):
    sc = _scanner(tmp_path, monkeypatch, retail_price=1000.0)
    run(sc._process(_l()))
    row = sc.conn.execute("SELECT estimated_market_value, retail_price FROM listings").fetchone()
    assert row["estimated_market_value"] == pytest.approx(500.0) and row["retail_price"] == 1000.0


def test_unprofitable_listings_never_cost_a_retail_lookup(tmp_path, monkeypatch):
    sc = _scanner(tmp_path, monkeypatch, retail_price=400.0)
    calls = []
    orig = sc.retail.lookup
    sc.retail.lookup = lambda t: (calls.append(t), orig(t))[1]
    run(sc._process(_l(price=480.0)))  # buy price near resale value: no profit, so no lookup
    assert calls == []


def test_embed_shows_new_retail_price():
    val = ValuationEngine(Config())
    from engine.valuation import Valuation
    l = _l()
    l.extra["retail"] = {"price": 399.99, "url": "https://bby/x"}
    e = build_embed(l, Valuation(340, 6, "sold", 100, 150, 150.0), CRITICAL)
    assert any(f["name"] == "New at Best Buy" and "$399.99" in f["value"] for f in e["fields"])
