from clients.municibid import parse_detail, parse_page

PAGE = ('<a href="/listing/111/dell-latitude-5420">x</a>'
        '[\\"$\\",\\"$L3f\\",\\"111\\",{\\"listing\\":{\\"id\\":111,\\"title\\":\\"Dell Latitude 5420\\",\\"subtitle\\":\\"\\",'
        '\\"currentPrice\\":45,\\"bidCount\\":3,\\"endsAtUtc\\":\\"2026-10-20T14:00:00\\",\\"primaryImageUrl\\":\\"http://i/x.jpg\\",'
        '\\"location\\":\\"Akron, OH\\",\\"agencyName\\":\\"City of Akron\\",\\"reservePrice\\":null,\\"sold\\":null,\\"hasVideo\\":false},'
        '\\"completed\\":false}]')


def test_parse_page():
    [l] = parse_page(PAGE)
    assert (l.platform, l.listing_id, l.title, l.price, l.extra["bids"]) == ("municibid", "111", "Dell Latitude 5420", 45.0, 3)
    assert l.url == "https://municibid.com/listing/111/dell-latitude-5420"
    assert l.end_time.isoformat() == "2026-10-20T14:00:00+00:00" and l.extra_cost_pct == 9.0 and l.extra["pickup_only"]


def test_parse_detail():
    html = '{\\"initial\\":{\\"currentPrice\\":638,\\"bidCount\\":7,\\"endsAtUtc\\":\\"2026-10-09T18:00:00\\",\\"status\\":1}} \\"ended\\":true'
    d = parse_detail(html, "1")
    assert d["price"] == 638.0 and d["bids"] == 7 and d["ended"]
    assert parse_detail("nothing", "1") == {}
