import asyncio
import logging
import sqlite3

import httpx

import db
from clients.imei import ImeiClient, ImeiReport, parse, should_check
from config import Config
from engine.details import Assessment
from engine.title_cleaner import extract_imei, is_valid_luhn
from engine.valuation import ValuationEngine


def make_imei(body14: str) -> str:
    for c in "0123456789":
        if is_valid_luhn(body14 + c):
            return body14 + c


GOOD = make_imei("35123456789012")
BAD_CHECK = GOOD[:-1] + str((int(GOOD[-1]) + 1) % 10)
CFG = Config()
PHONE = "Apple iPhone 13 128GB"
BOILER = "This item does not come with telecommunications service. identification of this item as stolen on a telecommunication carriers records."


def test_luhn():
    assert is_valid_luhn("490154203237518")  # well-known valid IMEI
    assert not is_valid_luhn("490154203237519")
    assert not is_valid_luhn("") and not is_valid_luhn("12ab")
    assert is_valid_luhn(GOOD) and not is_valid_luhn(BAD_CHECK)


def test_extract():
    assert extract_imei(f"iPhone IMEI: {GOOD} works") == GOOD
    assert extract_imei(f"IMEI {BAD_CHECK}") is None  # fails Luhn
    assert extract_imei(f"serial {GOOD}9") is None  # 16 digits is not an IMEI
    assert extract_imei("no number here") is None and extract_imei("") is None
    spaced = f"{GOOD[:2]} {GOOD[2:8]} {GOOD[8:14]} {GOOD[14]}"
    assert extract_imei(spaced) == GOOD
    other = make_imei("49015420323751")
    assert extract_imei(other) == other  # any prefix is accepted once Luhn passes
    assert extract_imei(f"call 555-0100 then {'-'.join([GOOD[:4], GOOD[4:8], GOOD[8:]])}") == GOOD


def test_parse_flat_and_report_formats():
    r = parse(GOOD, {"status": "success", "fmi_status": "OFF", "blacklist_status": "CLEAN", "sim_lock": "UNLOCKED"})
    assert r.verdict == "clean" and r.sim_lock == "UNLOCKED"
    r = parse(GOOD, {"status": "success", "result": "Model: iPhone 13<br>Find My iPhone: ON<br>Blacklist Status: Clean<br>Sim-Lock Status: Locked"})
    assert (r.fmi_status, r.blacklist_status, r.sim_lock, r.verdict) == ("ON", "CLEAN", "LOCKED", "bad")
    assert parse(GOOD, {"fmi_status": "OFF", "blacklist_status": "LOST_STOLEN"}).verdict == "bad"
    assert parse(GOOD, {"fmi_status": "OFF"}).verdict == "unknown"  # blacklist not reported: not proof of clean
    assert parse(GOOD, {"status": "error", "result": "Insufficient balance"}) is None
    assert parse(GOOD, {"status": "success", "result": "nothing useful"}) is None


def test_cost_rule():
    from dataclasses import replace
    cfg = replace(CFG, sickw_api_key="k", sickw_service_id="30", enable_paid_imei_checks=True)
    assert should_check(cfg, f"IMEI {GOOD}", 150.0) == GOOD
    assert should_check(cfg, f"IMEI {GOOD}", 99.99) is None  # below $100
    assert should_check(cfg, "no imei", 500.0) is None  # nothing printed
    assert should_check(replace(cfg, enable_paid_imei_checks=False), f"IMEI {GOOD}", 500.0) is None
    assert should_check(replace(cfg, sickw_api_key=""), f"IMEI {GOOD}", 500.0) is None


def test_valuation_cases(caplog):
    eng = ValuationEngine(CFG)
    text = f"{BOILER} IMEI {GOOD}"
    assert eng.telecom_factor(PHONE, text)[0] == 0.2  # Case 3: boilerplate fallback
    clean = ImeiReport(GOOD, "OFF", "CLEAN", "UNLOCKED")
    f, ts = eng.telecom_factor(PHONE, text, clean)  # Case 1
    assert f == 1.0 and ts.status == "clean" and not ts.unverified_telecom_status
    with caplog.at_level(logging.WARNING):
        for bad in (ImeiReport(GOOD, "ON", "CLEAN"), ImeiReport(GOOD, "OFF", "BLACKLISTED")):  # Case 2
            assert eng.telecom_factor(PHONE, text, bad)[0] == 0.2
    assert f"IMEI {GOOD} is locked/blacklisted. Applied 80% haircut." in caplog.text
    unknown = ImeiReport(GOOD, "OFF", None)
    assert eng.telecom_factor(PHONE, text, unknown)[0] == 0.2  # inconclusive report: keep the disclaimer rule
    a = Assessment(found=True, text=text, tested_ok=True)
    assert eng.detail_condition_factor(PHONE, a) == 0.2
    assert eng.detail_condition_factor(PHONE, a, clean) == 1.0
    assert eng.detail_condition_factor(PHONE, a, ImeiReport(GOOD, "ON", "CLEAN")) == 0.2
    assert eng.detail_condition_factor("Samsung 55 inch TV", Assessment(found=True, text="", tested_ok=True), clean) == 1.0


def test_client_calls_once_then_caches():
    calls = []

    def handler(req: httpx.Request):
        calls.append(req.url.params["imei"])
        return httpx.Response(200, json={"status": "success", "fmi_status": "OFF", "blacklist_status": "CLEAN", "sim_lock": "UNLOCKED"})

    from dataclasses import replace
    cfg = replace(CFG, sickw_api_key="k", sickw_service_id="30")
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript(db.SCHEMA if hasattr(db, "SCHEMA") else "")

    async def go():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
            c = ImeiClient(cfg, http, conn)
            return await c.check_imei_status(GOOD), await c.check_imei_status(GOOD)

    r1, r2 = asyncio.run(go())
    assert r1.verdict == r2.verdict == "clean" and calls == [GOOD]


def test_client_failure_returns_none_and_is_not_cached():
    from dataclasses import replace
    cfg = replace(CFG, sickw_api_key="k", sickw_service_id="30")

    async def go():
        async with httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(500))) as http:
            return await ImeiClient(cfg, http, None).check_imei_status(GOOD)

    assert asyncio.run(go()) is None


BLOB = ("Model: iPhone 13 Pro<br>Capacity: 128GB<br>iCloud Lock: OFF<br>iCloud Status: Clean<br>"
        "Blacklist Status: CLEAN<br>Sim-Lock Status: Unlocked<br>Warranty: Expired")


def test_multiline_text_blob():
    r = parse(GOOD, {"status": "success", "result": BLOB})
    assert (r.fmi_status, r.blacklist_status, r.sim_lock, r.verdict) == ("OFF", "CLEAN", "UNLOCKED", "clean")
    plain = "iCloud Status: ON\nBlacklist Status: Clean\nSim-Lock: Locked"
    assert parse(GOOD, {"status": "success", "result": plain}).verdict == "bad"
    assert parse(GOOD, {"status": "success", "result": "iCloud: OFF\nBlacklist: Lost"}).verdict == "bad"
    assert parse(GOOD, {"status": "success", "result": "iCloud Lock: OFF\nBlacklist Status: Stolen"}).blacklist_status == "STOLEN"
    # conservative: FMI off alone, or blacklist clean alone, is inconclusive
    assert parse(GOOD, {"status": "success", "result": "iCloud Lock: OFF\nModel: x"}).verdict == "unknown"
    assert parse(GOOD, {"status": "success", "result": "Blacklist Status: Clean"}).verdict == "unknown"


def test_check_imei_cli_output():
    from dataclasses import replace
    from clients.imei import check_imei_cli
    cfg = replace(CFG, sickw_api_key="k", sickw_service_id="30")
    calls = []

    def handler(req):
        calls.append(1)
        return httpx.Response(200, json={"status": "success", "result": BLOB})

    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript(db.SCHEMA)

    async def go(n):
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
            return await check_imei_cli(cfg, http, conn, n)

    out = asyncio.run(go(GOOD))
    assert "Luhn valid: True" in out and "cached: no" in out and "100%" in out and "raw SickW response" in out
    assert "cached: yes" in asyncio.run(go(GOOD)) and len(calls) == 1
    assert "no lookup made" in asyncio.run(go(BAD_CHECK)) and len(calls) == 1


def test_manual_entry_and_site_row():
    import webpage
    from clients.imei import set_manual, cached_report
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript(db.SCHEMA)
    eng = ValuationEngine(CFG)
    row = {"title": PHONE, "factor": 0.5, "mobile": True}
    notes: list = []
    webpage._apply_phone(row, eng, f"IMEI {GOOD}", notes, conn)
    assert row["imei"] == GOOD and row["imei_verdict"] == "unchecked" and row["factor"] <= 0.5
    set_manual(conn, GOOD, "clean")
    assert cached_report(conn, GOOD).manual
    row = {"title": PHONE, "factor": 0.5, "mobile": True}
    webpage._apply_phone(row, eng, f"IMEI {GOOD}", [], conn)
    assert row["imei_verdict"] == "clean" and row["factor"] == 1.0
    set_manual(conn, GOOD, "bad")
    row = {"title": PHONE, "factor": 0.5, "mobile": True}
    webpage._apply_phone(row, eng, f"IMEI {GOOD}", [], conn)
    assert row["imei_verdict"] == "bad" and row["factor"] == 0.2
    laptop = {"title": "Apple MacBook Air 13", "factor": 1.0}
    webpage._apply_phone(laptop, eng, f"serial notes IMEI {GOOD}", [], conn)
    assert laptop["imei"] == GOOD  # non-phones still surface the number
