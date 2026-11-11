import db
import catalog

COLS = "(platform, listing_id, title, brand, kind, style, category, listing_type, current_bid, buy_now, shipping, bids, end_time, url, first_seen, last_seen, domain, description, location)"


def add(conn, platform, lid):
    conn.execute(f"INSERT INTO catalog {COLS} VALUES (?,?,?,'Dell','laptop','','','auction',10,NULL,NULL,0,'2026-12-01T00:00:00+00:00','https://x/'||?,1,1,'electronics','', '')", (platform, lid, "Dell Latitude 7420", lid))
    conn.commit()


def row(conn, platform, lid):
    return conn.execute("SELECT * FROM catalog WHERE platform=? AND listing_id=?", (platform, lid)).fetchone()


def test_a_premium_read_from_the_listings_own_terms_overrides_the_source_constant(tmp_path):
    conn = db.connect(str(tmp_path / "t.db"))
    add(conn, "govdeals", "A")
    add(conn, "govdeals", "B")
    assert catalog.economics(row(conn, "govdeals", "A"))["prem"] == 0.10                 # no sale terms read: the per-source constant
    catalog.set_listing_premium(conn, "govdeals", "B", 0.125, "terms page: 12.5% buyer's premium")
    assert catalog.economics(row(conn, "govdeals", "B"))["prem"] == 0.125                # this listing's own premium wins
    assert catalog.economics(row(conn, "govdeals", "A"))["prem"] == 0.10                 # a neighbour is unaffected
    r = row(conn, "govdeals", "B")
    assert r["premium_source"].startswith("terms page")


def test_zero_is_a_real_premium_not_a_missing_one(tmp_path):
    conn = db.connect(str(tmp_path / "t.db"))
    add(conn, "propertyroom", "Z")
    catalog.set_listing_premium(conn, "propertyroom", "Z", 0.0, "sale terms: no premium")
    assert catalog.economics(row(conn, "propertyroom", "Z"))["prem"] == 0.0              # 0% overrides PropertyRoom's 15% constant


def test_older_databases_get_the_columns_by_migration(tmp_path):
    conn = db.connect(str(tmp_path / "t.db"))
    cols = {r[1] for r in conn.execute("PRAGMA table_info(catalog)")}
    assert {"buyer_premium", "premium_source"} <= cols
    assert catalog.listing_premium({"platform": "municibid"}) == 0.09                    # plain dicts without the field still work
