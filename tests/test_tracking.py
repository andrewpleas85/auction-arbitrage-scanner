import db
import tracking


def test_parse_urls():
    assert tracking.parse_listing_url("https://www.propertyroom.com/l/apple-ipad-mini-4-128gb-wi-fi-tablet/18984356") == ("propertyroom", "18984356")
    assert tracking.parse_listing_url("https://shopgoodwill.com/item/12345") == ("shopgoodwill", "12345")
    assert tracking.parse_listing_url("https://example.com/x/1") is None


def test_track_untrack_and_max_bid_kept(tmp_path):
    conn = db.connect(str(tmp_path / "t.db"))
    url = "https://www.propertyroom.com/l/x/111"
    tracking.track(conn, url, 40.0, "note")
    tracking.track(conn, url)  # re-adding without a max keeps the old one
    row = conn.execute("SELECT max_bid, note FROM tracked").fetchone()
    assert (row["max_bid"], row["note"]) == (40.0, "note")
    assert tracking.untrack(conn, url) == 1 and conn.execute("SELECT COUNT(*) FROM tracked").fetchone()[0] == 0


def test_merge_replaces_and_numbers_after():
    rows = [{"platform": "p", "listing_id": "1", "rank": 1}, {"platform": "p", "listing_id": "2", "rank": 2}]
    mine = [{"platform": "p", "listing_id": "2", "tracked": True}, {"platform": "p", "listing_id": "9", "tracked": True}]
    out = tracking.merge(rows, mine)
    assert [r["listing_id"] for r in out] == ["1", "2", "9"]
    assert out[1]["tracked"] and out[1]["rank"] == 2 and out[2]["rank"] == 3
