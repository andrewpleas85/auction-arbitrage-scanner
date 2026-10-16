"""Paid IMEI status lookup (SickW API): is the phone iCloud-locked (Find My ON) or blacklisted?

Each call costs money, so callers must gate it (see `should_check`): only when an IMEI was explicitly printed in the
listing AND the item is worth >= `IMEI_MIN_VALUE`. Results are cached in `comp_cache` (provider "imei") because a
device's status does not change between scans and we must never pay twice for the same IMEI. Failed lookups are not cached.

Endpoint (per the task spec): https://sickw.com/api.php?format=json&key=KEY&imei=IMEI&service=SERVICE_ID
SickW's real JSON is {"status": "success", "result": "<text/html report>"}, and different SERVICE_IDs return different
reports, so `parse` accepts BOTH the flat fields the spec names (fmi_status, blacklist_status, sim_lock) and the labelled
lines of a "result" report ("Find My iPhone: OFF", "Blacklist Status: Clean", "Sim-Lock Status: Unlocked"). Not yet
verified against a live key: pick a service that returns FMI + blacklist and check one known IMEI before trusting it.
"""
import html
import json
import logging
import re
import sqlite3
import time
from dataclasses import dataclass

import httpx

from config import Config
from engine.title_cleaner import extract_imei

log = logging.getLogger(__name__)

URL = "https://sickw.com/api.php"
IMEI_MIN_VALUE = 100.0  # cost rule: never pay for a lookup on a cheap item
CACHE_TTL_DAYS = 30


@dataclass
class ImeiReport:
    imei: str
    fmi_status: str | None = None  # "OFF" | "ON" | None (unknown)
    blacklist_status: str | None = None  # "CLEAN" | "BLACKLISTED" | "LOST_STOLEN" | ... | None
    sim_lock: str | None = None  # "UNLOCKED" | "LOCKED" | None
    manual: bool = False  # entered by hand (--set-imei), not from a paid lookup

    @property
    def verdict(self) -> str:
        """'bad' = iCloud-locked or not clean; 'clean' = Find My OFF and blacklist CLEAN, both explicit; else 'unknown'."""
        if self.fmi_status == "ON" or (self.blacklist_status is not None and self.blacklist_status != "CLEAN"):
            return "bad"
        if self.fmi_status == "OFF" and self.blacklist_status == "CLEAN":
            return "clean"
        return "unknown"

    def to_json(self) -> str:
        return json.dumps(self.__dict__)


def _norm_fmi(v) -> str | None:
    s = str(v or "").strip().upper()
    if s in {"OFF", "FALSE", "NO", "DISABLED", "CLEAN"}:
        return "OFF"
    if s in {"ON", "TRUE", "YES", "ENABLED", "LOCKED"}:
        return "ON"
    return None


def _norm_blacklist(v) -> str | None:
    s = re.sub(r"[\s-]+", "_", str(v or "").strip().upper())
    if not s or s in {"UNKNOWN", "N/A", "NA", "NONE_FOUND"}:
        return None
    return "CLEAN" if s in {"CLEAN", "CLEAR", "GOOD", "NOT_BLACKLISTED", "NO"} else s


def _norm_sim(v) -> str | None:
    s = str(v or "").strip().upper()
    return "UNLOCKED" if s.startswith("UNLOCK") else "LOCKED" if s.startswith("LOCK") else None


def _label(text: str, *names: str) -> str | None:
    m = re.search(rf"(?:{'|'.join(names)})\s*:\s*([A-Za-z_ -]+)", text, re.I)
    return m.group(1).strip() if m else None


def parse(imei: str, data: dict) -> ImeiReport | None:
    """SickW JSON -> ImeiReport; None when the call failed or the payload has nothing usable."""
    if not isinstance(data, dict) or str(data.get("status", "success")).lower() not in {"success", "ok"}:
        return None
    fmi, bl, sim = data.get("fmi_status"), data.get("blacklist_status"), data.get("sim_lock")
    result = data.get("result")
    if isinstance(result, str):
        text = html.unescape(re.sub(r"<[^>]+>", "\n", result))
        fmi = fmi or _label(text, r"icloud\s*(?:lock|status)?", r"find\s*my\s*(?:iphone)?", r"fmi")
        bl = bl or _label(text, r"blacklist(?:\s*status)?", r"gsma\s*status")
        sim = sim or _label(text, r"sim[- ]?lock(?:\s*status)?", r"locked")
    report = ImeiReport(imei, _norm_fmi(fmi), _norm_blacklist(bl), _norm_sim(sim))
    return None if report.verdict == "unknown" and report.sim_lock is None and report.fmi_status is None \
        and report.blacklist_status is None else report


def should_check(cfg: Config, text: str, estimated_value: float) -> str | None:
    """The IMEI to look up, or None. Enforces the cost rule: paid checks on, key + service set, value >= $100, IMEI in the text."""
    if not (cfg.enable_paid_imei_checks and cfg.sickw_api_key and cfg.sickw_service_id):
        return None
    if estimated_value < IMEI_MIN_VALUE:
        return None
    return extract_imei(text)


def cached_report(conn: sqlite3.Connection | None, imei: str) -> ImeiReport | None:
    """A stored report (paid lookup or manual entry) for this IMEI, free to read."""
    if conn is None:
        return None
    r = conn.execute("SELECT payload, fetched_at FROM comp_cache WHERE provider='imei' AND query=?", (imei,)).fetchone()
    if not r or time.time() - r["fetched_at"] > CACHE_TTL_DAYS * 86400:
        return None
    return ImeiReport(**json.loads(r["payload"]))


def store_report(conn: sqlite3.Connection, report: ImeiReport) -> None:
    conn.execute("INSERT OR REPLACE INTO comp_cache (provider, query, fetched_at, payload) VALUES ('imei', ?, ?, ?)",
                 (report.imei, int(time.time()), report.to_json()))
    conn.commit()


def set_manual(conn: sqlite3.Connection, imei: str, verdict: str) -> ImeiReport:
    """Record a hand-checked result (e.g. from the CTIA Stolen Phone Checker). 'clean' means YOU checked blacklist AND iCloud."""
    from engine.title_cleaner import is_valid_luhn
    imei = re.sub(r"\D", "", imei)
    if len(imei) != 15 or not is_valid_luhn(imei):
        raise ValueError(f"{imei!r} is not a valid 15-digit IMEI")
    if verdict not in ("clean", "bad"):
        raise ValueError("verdict must be 'clean' or 'bad'")
    report = ImeiReport(imei, "OFF", "CLEAN", manual=True) if verdict == "clean" else ImeiReport(imei, None, "BLACKLISTED", manual=True)
    store_report(conn, report)
    return report


class ImeiClient:
    def __init__(self, cfg: Config, http: httpx.AsyncClient, conn: sqlite3.Connection | None = None):
        self.cfg, self.http, self.conn = cfg, http, conn

    def _cached(self, imei: str) -> ImeiReport | None:
        return cached_report(self.conn, imei)

    def _store(self, report: ImeiReport) -> None:
        if self.conn is not None:
            store_report(self.conn, report)

    async def fetch_raw(self, imei: str) -> dict | None:
        """The raw SickW JSON (one paid call), or None on any failure."""
        try:
            resp = await self.http.get(URL, params={"format": "json", "key": self.cfg.sickw_api_key, "imei": imei,
                                                    "service": self.cfg.sickw_service_id}, timeout=45)
            resp.raise_for_status()
            return resp.json()
        except (httpx.HTTPError, ValueError) as e:
            log.warning("IMEI lookup failed for %s: %s", imei, type(e).__name__)  # never log the URL: it carries the key
            return None

    async def check_imei_status(self, imei: str) -> ImeiReport | None:
        """Cached lookup. None on any failure (the caller then falls back to the telecom-disclaimer rule)."""
        if cached := self._cached(imei):
            return cached
        data = await self.fetch_raw(imei)
        report = parse(imei, data) if data is not None else None
        if report is None:
            log.warning("IMEI lookup for %s returned nothing usable", imei)
            return None
        self._store(report)
        return report


async def check_imei_cli(cfg: Config, http: httpx.AsyncClient, conn: sqlite3.Connection, imei: str) -> str:
    """`main.py --check-imei N`: Luhn, cache, raw response, parsed flags and the multiplier. At most one paid call, none if cached."""
    from engine.title_cleaner import is_valid_luhn
    from engine.valuation import ValuationEngine

    imei = re.sub(r"\D", "", imei)
    out = [f"IMEI {imei}", f"  Luhn valid: {is_valid_luhn(imei) and len(imei) == 15}"]
    if len(imei) != 15 or not is_valid_luhn(imei):
        return "\n".join(out + ["  not a valid IMEI: no lookup made"])
    client = ImeiClient(cfg, http, conn)
    report = client._cached(imei)
    out.append(f"  cached: {'yes' if report else 'no'}")
    if report is None:
        if not (cfg.sickw_api_key and cfg.sickw_service_id):
            return "\n".join(out + ["  SICKW_API_KEY / SICKW_SERVICE_ID not set: no lookup made"])
        data = await client.fetch_raw(imei)
        out.append(f"  raw SickW response: {json.dumps(data, indent=2)[:3000] if data is not None else 'request failed'}")
        report = parse(imei, data) if data is not None else None
        if report is not None:
            client._store(report)
    if report is None:
        return "\n".join(out + ["  no usable status: inconclusive, the telecom-disclaimer haircut stays"])
    factor, _ = ValuationEngine(cfg).telecom_factor("Apple iPhone", "", report)
    out += [f"  FMI: {report.fmi_status}   blacklist: {report.blacklist_status}   SIM lock: {report.sim_lock}",
            f"  verdict: {report.verdict}   valuation multiplier: {factor:.0%} of a clean comp"
            + ("" if report.verdict != "unknown" else " (inconclusive: standard 20%/50% rule applies)")]
    return "\n".join(out)
