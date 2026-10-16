# Auction arbitrage scanner

A Python tool that finds underpriced items on online auction and government-surplus sites, values them against real eBay
**sold** prices, subtracts every cost (buyer's premium, sales tax, marketplace fees, shipping), and ranks what clears a
profit threshold on a local web page. Bidding and listing are manual: the software never places a bid.

## What is in here
- **Site clients** (`clients/`): ShopGoodwill, GovDeals, PublicSurplus, PropertyRoom, Municibid, GSA, B-Stock manifests, eBay, plus TXAuction (`txauction.py`).
- **Valuation engine** (`engine/`, `catalog.py`, `fill_ranges.py`): title cleaning and model matching, value ranges built from sold sales, sell-through/liquidity estimates, precious-metal melt, a text-based luxury authenticity screen.
- **Profit model**: net profit and maximum bid after all costs, with measured auction price-climb ratios.
- **Web page** (`webpage.py`): a single static, filterable page (served locally; works on a phone).
- **MCP pricing server** (`mcp_auction_pricer.py`, `free_comp_fillers.py`, `batch_evaluate.py`): spec-matched pricing tools with free fallbacks (Swappa public pages, a spec matrix, live metal spot).
- **Tests**: 532 pytest tests (`pytest -q`).

## Run it
```
python -m venv .venv && .venv/bin/pip install -r requirements.txt
cp .env.example .env          # add your own keys; every key is optional
.venv/bin/pytest -q
.venv/bin/python main.py --web --web-refresh --serve    # http://127.0.0.1:8765
```
The catalog database and crawl snapshots are created locally on first run and are not part of this repo.

## Status and honesty notes
Personal project, working end to end. All values are estimates that depend on how many comparable sold sales exist; the
tool reports its sample sizes. It has not been used to complete a trade, so no profit figures are claimed.
The TXAuction client reads only public `/auctions/` pages (its robots.txt disallows `/api/`). Check each site's terms before pointing the other clients at it.
