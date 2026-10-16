import asyncio
from datetime import datetime, timedelta, timezone

import pytest

import db
import report
from alerting.discord import CRITICAL, WATCHLIST, build_embed
from config import Config
from engine.timeleft import time_left
from engine.valuation import ValuationEngine, Valuation
from models import Listing

NOW = datetime(2026, 10, 8, 12, 0, tzinfo=timezone.utc)


@pytest.mark.parametrize("delta,expected", [
    (timedelta(days=3, hours=5, minutes=20), "3d 5h"), (timedelta(hours=2, minutes=7), "2h 7m"),
    (timedelta(minutes=45, seconds=30), "45m"), (timedelta(seconds=20), "<1m"),
    (timedelta(seconds=0), "ended"), (timedelta(hours=-1), "ended"),
])
def test_time_left_formats(delta, expected):
    assert time_left(NOW + delta, NOW) == expected


def test_time_left_without_end_time():
    assert time_left(None) is None


def _rec(conn, i, end, status="watchlist", kind="auction", price=10.0, **kw):
    db.record(conn, platform="shopgoodwill", listing_id=str(i), title=f"Item {i}", price=price, shipping_cost=0,
              status=status, listing_type=kind, end_time=end, estimated_market_value=100, net_profit_usd=40,
              url=f"https://x/{i}", **kw)


def test_record_stores_url_and_update_time(tmp_path):
    conn = db.connect(str(tmp_path / "t.db"))
    _rec(conn, 1, NOW + timedelta(hours=1))
    r = conn.execute("SELECT url, updated_at FROM listings").fetchone()
    assert r["url"] == "https://x/1" and r["updated_at"] > 0


def test_watchlist_sorted_soonest_first_and_drops_ended_and_other_statuses(tmp_path):
    conn = db.connect(str(tmp_path / "t.db"))
    _rec(conn, "late", NOW + timedelta(days=2))
    _rec(conn, "soon", NOW + timedelta(minutes=30))
    _rec(conn, "mid", NOW + timedelta(hours=5), status="alerted")
    _rec(conn, "gone", NOW - timedelta(hours=1))                       # already ended
    _rec(conn, "skip", NOW + timedelta(hours=1), status="skipped")     # not tracked
    _rec(conn, "bin", NOW + timedelta(hours=1), kind="buy_it_now")     # not an auction
    rows = report.watchlist_rows(conn, NOW)
    assert [r["listing_id"] for r in rows] == ["soon", "mid", "late"]
    assert [r["left"] for r in rows] == ["30m", "5h 0m", "2d 0h"]


def test_watchlist_text_output(tmp_path):
    conn = db.connect(str(tmp_path / "t.db"))
    _rec(conn, 1, NOW + timedelta(hours=3), price=499.99, comp_source="melt")
    _rec(conn, 2, NOW + timedelta(days=1), kind="daily_auction")
    out = report.format_watchlist(report.watchlist_rows(conn, NOW))
    assert "3h 0m" in out and "$  499.99" in out and "melt" in out and "daily*" in out and "https://x/1" in out
    assert "No tracked auctions" in report.format_watchlist([])


def _val(source="sold"):
    return Valuation(300, 6, source, 100, 150, 150.0)


def test_watchlist_embed_shows_time_left():
    l = Listing("shopgoodwill", "1", "Gold lot", 500, 0, "https://u", None, "c", listing_type="auction",
                end_time=datetime.now(timezone.utc) + timedelta(minutes=12, seconds=30))
    e = build_embed(l, _val(), WATCHLIST)
    auction = next(f for f in e["fields"] if f["name"] == "Auction")
    assert "Ends in 12m" in auction["value"]


def test_buy_it_now_alert_on_an_auction_shows_the_auction_clock():
    l = Listing("shopgoodwill", "1", "Gold lot", 700, 0, "https://u", None, "c", listing_type="buy_it_now",
                end_time=datetime.now(timezone.utc) + timedelta(days=2, hours=3, minutes=5),
                extra={"auction_bid": 400.0})
    action = next(f for f in build_embed(l, _val(), CRITICAL)["fields"] if f["name"] == "Action")
    assert "current bid $400.00" in action["value"] and "ends in 2d 3h" in action["value"]
    plain = Listing("reverb", "2", "Pedal", 90, 0, "https://u", None, "c")
    assert "auction" not in next(f for f in build_embed(plain, _val(), CRITICAL)["fields"] if f["name"] == "Action")["value"]


def test_cli_watchlist_reads_the_database_without_scanning(tmp_path, monkeypatch, capsys):
    import main
    path = str(tmp_path / "w.db")
    conn = db.connect(path)
    _rec(conn, 9, datetime.now(timezone.utc) + timedelta(hours=2, minutes=1), price=42.0)
    conn.close()
    monkeypatch.setattr("sys.argv", ["main.py", "--watchlist"])
    monkeypatch.setattr(main, "Config", lambda: Config(db_path=path))
    asyncio.run(main.amain())
    out = capsys.readouterr().out
    assert "2h" in out and "Item 9" in out and "$   42.00" in out
