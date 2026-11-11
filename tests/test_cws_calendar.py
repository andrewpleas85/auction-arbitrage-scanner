import asyncio

import httpx

import db
import lot_sources as ls
from clients import cws_calendar as cw

FIX = open("tests/fixtures/cws_home_cards.html").read()


def test_home_page_cards_are_parsed_and_real_estate_is_flagged():
    cards = cw.parse_home(FIX)
    assert [(c["catalog_id"], c["date"], c["real_estate"]) for c in cards] == [("479", "Nov 12 2026", False), ("470", "Nov 13 2026", True), ("473", "Nov 13 2026", True)]
    assert cards[0]["url"] == "https://bid.cwsmarketing.com/auctions/catalog/id/479" and "US Customs" in cards[0]["description"]


def test_only_non_real_estate_sales_become_discovery_leads_and_none_are_priced():
    leads = cw.to_leads(cw.parse_home(FIX))
    assert len(leads) == 1 and leads[0]["source"] == "cws_treasury" and leads[0]["lead_id"] == "cws-479" and leads[0]["dates"] == "Nov 12 2026"


def test_the_calendar_sync_is_one_polite_request_and_stores_leads(tmp_path):
    seen = []

    def handler(req):
        seen.append(str(req.url))
        return httpx.Response(200, text=FIX)

    async def go():
        conn = db.connect(str(tmp_path / "t.db"))
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
            return await cw.sync(conn, http), conn

    res, conn = asyncio.run(go())
    assert res == {"cards": 3, "real_estate_skipped": 2, "leads": 1} and len(seen) == 1
    assert ls.leads(conn)[0]["priced"] is False and ls.leads(conn)[0]["label"].startswith("CWS")


def test_it_is_part_of_the_normal_catalog_run():
    import catalog
    assert "cws_calendar" in catalog.ALL_DOMAINS
