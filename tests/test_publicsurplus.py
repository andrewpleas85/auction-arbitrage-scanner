from clients.publicsurplus import parse_detail, parse_page
from tracking import parse_listing_url

CARD = '''<div class="auction-item" id="{id}catGrid"><div class="auction-item-img"><a href="/sms/auction/view?auc={id}" >
<img src="https://cdn/x.jpg" alt="Picture" /></a><span class="auction-item-state"> AZ </span></div>
<h6><a href="/sms/auction/view?auc={id}"
   title="#{id} - Dell Latitude 5420 &amp; charger">x</a></h6>
Price: <b id="val_{id}catGrid"> $1,270.50 </b><script>updateTimeLeftSpan(timeLeftInfoMap, {id}, "{id}catGrid", 1791570430435, 1791576000000, 1791394858134, 0, "", "", "catList" , cb);</script></div>'''


def test_parse_page():
    ls = parse_page(CARD.format(id=111) + CARD.format(id=222))
    assert [l.listing_id for l in ls] == ["111", "222"]
    l = ls[0]
    assert (l.platform, l.title, l.price, l.location) == ("publicsurplus", "Dell Latitude 5420 & charger", 1270.5, "AZ")
    assert l.url == "https://www.publicsurplus.com/sms/auction/view?auc=111" and l.extra_cost_pct == 10.5
    assert l.end_time.isoformat() == "2026-10-09T20:00:00+00:00"


def test_parse_detail():
    page = ("<p>Auction Ends Oct 9, 2026 12:32 PM MDT This auction might extend</p><td># of Bids</td><td>11</td>"
            "<p>Current Price: $18.50 Increment: $0.50</p>")
    d = parse_detail(page)
    assert d["price"] == 18.5 and d["bids"] == 11 and not d["ended"] and d["end"].isoformat() == "2026-10-09T18:32:00+00:00"
    assert parse_detail("nothing here") == {}


def test_track_links():
    assert parse_listing_url("https://www.publicsurplus.com/sms/auction/view?auc=4096932") == ("publicsurplus", "4096932")
    assert parse_listing_url("https://municibid.com/listing/12345/dell") == ("municibid", "12345")


def test_shipping_mode_and_pickup_location():
    from clients.publicsurplus import near_chicago
    base = ("<p>Current Price: $5.00</p><p>Auction Ends Oct 9, 2026 12:32 PM CDT</p><td># of Bids</td><td>2</td>"
            "<p>Shipping {ship} Bid on Item Region: IL</p><p>Pick-up Location Cook County Surplus [ 100 W Randolph St Chicago, IL 60601 ] 100 W</p>")
    d = parse_detail(base.format(ship="Buyer must pickup item(s)"))
    assert d["ship_mode"] == "pickup" and (d["pickup_state"], d["pickup_zip"]) == ("IL", "60601")
    assert near_chicago(d["pickup_state"], d["pickup_zip"])
    assert parse_detail(base.format(ship="Contact agency for shipping"))["ship_mode"] == "ask"
    assert not near_chicago("TX", "78207") and not near_chicago("IL", "62701") and near_chicago("IN", "46320") and not near_chicago(None, None)


def test_detail_reads_free_shipping_removed_drive_and_untested_wording():
    page = ("<p>Current Price: $73.00</p><td># of Bids</td><td>1</td><p>Auction Ends Oct 12, 2026 03:00 PM MDT</p>"
            "<p>Shipping Contact agency for shipping Bid on Item Region: AZ</p>"
            "<p>This item will be SHIPPED FOR FREE via FEDEX Ground to the buyer's Continental US physical address. "
            "*New accounts need to wait 30 days before bidding* Hard drive (HDD) and/or solid-state drive (SSD) have been removed for "
            "security reasons. Power supply and other accessories are not included. Unit powers up to BIOS with no further testing done.</p>"
            "<p>Buyers Premium: A Buyers Premium of 10% will be added to the final sale price</p>")
    d = parse_detail(page)
    assert d["premium_pct"] == 10.0 and d["free_shipping"] and d["drives_removed"]
    assert d["no_power_supply"] and d["untested"] and d["needs_30_day_account"]


def test_zero_bid_lot_is_still_read_so_the_pickup_filter_can_run():
    page = ("<p>Auction Ends Oct 13, 2026 02:00 PM MDT</p><td># of Bids</td><td>No Bids</td><p>Opening Price: $50.00</p>"
            "<p>Shipping Buyer must pickup item(s) Bid on Item Region: CA</p>"
            "<p>Pick-up Location Headquarters [ 6135 State Farm Dr Rohnert Park, CA 94928 ] 6135</p>")
    from clients.publicsurplus import near_chicago
    d = parse_detail(page)
    assert d and d["price"] == 50.0 and d["bids"] == 0 and d["ship_mode"] == "pickup"
    assert (d["pickup_state"], d["pickup_zip"]) == ("CA", "94928") and not near_chicago(d["pickup_state"], d["pickup_zip"])
