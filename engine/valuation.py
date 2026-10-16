"""Turn comps into an estimated resale value and profit figures."""
import logging
import statistics
from dataclasses import dataclass

from clients.ebay import Comps
from config import Config
from engine.luxury_authenticator import LuxuryAssessment, LuxuryAuthenticator
from engine.matching import is_relevant
from engine.details import Assessment
from engine.title_cleaner import TelecomStatus, comp_matches, condition, is_locked, telecom_status


log = logging.getLogger(__name__)

@dataclass
class Valuation:
    estimated_value: float  # expected resale revenue (before fees)
    comp_count: int
    comp_source: str  # "sold" | "active"
    cost: float  # all-in acquisition cost
    net_profit: float
    margin_pct: float  # net profit / cost * 100 (ROI)
    gross_margin_pct: float = 0.0  # (est - cost) / est * 100
    detail: str = ""

    @property
    def roi_pct(self) -> float:
        return self.margin_pct

    def meets(self, cfg: Config) -> bool:
        return self.net_profit >= cfg.min_profit and self.margin_pct >= cfg.min_margin_pct


def trimmed_mean(prices: list[float]) -> float:
    """With 5+ samples drop the single highest and lowest price; average the rest."""
    if len(prices) < 5:
        return statistics.fmean(prices)
    return statistics.fmean(sorted(prices)[1:-1])


def estimate_unit_value(
    cfg: Config, query: str, comps: Comps, title: str | None = None, value_ceiling: float | None = None
) -> tuple[float, int] | None:
    """true_market_value: trimmed mean of the latest `sold_comps_max` matching sales.

    Needs at least `min_comps` matching sales (default 5). None if there aren't enough.
    Active-ask comps (opt-in only) are haircut by cfg.active_ask_discount.
    """
    if comps.source == "active" and not cfg.allow_active_comps:
        return None
    matches = [c for c in comps.items if (comp_matches(title, c.title) if title is not None
                                          else is_relevant(query, c.title))]
    matches.sort(key=lambda c: c.sold_date.timestamp() if c.sold_date else 0, reverse=True)
    prices = [c.price for c in matches[: cfg.sold_comps_max]]
    if len(prices) < cfg.min_comps:
        return None
    est = trimmed_mean(prices)
    if comps.source == "active":
        est *= cfg.active_ask_discount
    if value_ceiling is not None:
        est = min(est, value_ceiling)  # e.g. a fraction of the NEW retail price
    return est, len(prices)


def _finish(cfg: Config, revenue: float, units: int, cost: float, n: int, source: str,
            detail: str = "", fee_pct: float | None = None, fixed_fee: float | None = None,
            outbound: float | None = None) -> Valuation:
    # net_profit = tmv - total_cost - selling_fees, selling_fees = tmv * fee% + fixed fee per sale.
    # total_cost already includes buyer's premium and inbound shipping; outbound shipping is extra.
    fees = (revenue * (cfg.resale_fee_pct if fee_pct is None else fee_pct) / 100
            + (cfg.resale_fixed_fee if fixed_fee is None else fixed_fee) * units)
    net = revenue - cost - (cfg.outbound_shipping if outbound is None else outbound) * units - fees
    return Valuation(
        estimated_value=round(revenue, 2),
        comp_count=n,
        comp_source=source,
        cost=round(cost, 2),
        net_profit=round(net, 2),
        margin_pct=round(net / cost * 100, 1) if cost > 0 else 0.0,
        gross_margin_pct=round((revenue - cost) / revenue * 100, 1) if revenue > 0 else 0.0,
        detail=detail,
    )


def valuate(
    cfg: Config, query: str, comps: Comps, price: float, shipping: float,
    title: str | None = None, condition_factor: float = 1.0, extra_cost: float = 0.0,
    value_ceiling: float | None = None,
) -> Valuation | None:
    """Single-item valuation. Returns None when there aren't enough relevant comps."""
    unit = estimate_unit_value(cfg, query, comps, title, value_ceiling)
    if unit is None:
        return None
    est, n = unit
    est *= condition_factor
    detail = f"condition factor x{condition_factor:g}" if condition_factor != 1.0 else ""
    return _finish(cfg, est, 1, price + shipping + extra_cost, n, comps.source, detail)


def valuate_lot(
    cfg: Config, lines: list[tuple[int, float | None]], lot_cost: float
) -> Valuation | None:
    """Lot valuation. `lines` = [(qty, unit_value or None if uncomped)]; lot_cost is all-in.

    Uncomped lines contribute $0 (conservative). Value is haircut by cfg.lot_haircut.
    """
    total_units = sum(q for q, _ in lines)
    comped = [(q, v) for q, v in lines if v is not None]
    if not comped:
        return None
    manifest_value = sum(q * v for q, v in comped)
    comped_units = sum(q for q, _ in comped)
    revenue = manifest_value * cfg.lot_haircut
    detail = (f"{comped_units}/{total_units} units comped, manifest ${manifest_value:,.0f} "
              f"x{cfg.lot_haircut:g} haircut")
    return _finish(cfg, revenue, comped_units, lot_cost, len(comped), "mixed", detail)


class ValuationEngine:
    """Realized-value engine: sold comps -> trimmed mean -> condition haircut -> all-in net margin.

    Condition weighting (by title): working x1.0, refurbished/mint x1.10, untested/as-is/no power/
    spares x0.6, locked/for parts/bad ESN/MDM x0.2. Factors come from Config.
    """

    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.luxury = LuxuryAuthenticator()

    def luxury_assessment(self, title: str, text: str = "") -> LuxuryAssessment | None:
        return self.luxury.assess(title, text)

    def _with_luxury(self, base: float, title: str, text: str) -> float:
        la = self.luxury.assess(title, text)
        return base if la is None else min(base, la.factor)

    def luxury_max_bid(self, title: str, text: str = "") -> float | None:
        """Scrap-level bid ceiling ($60) for an unverified Tier 1 luxury piece, else None."""
        la = self.luxury.assess(title, text)
        return la.max_bid if la else None

    def luxury_factor(self, title: str, text: str = "") -> float:
        """Risk-adjusted share of a genuine designer item's value. Tier 1 luxury: verified 100%, unverified 15%. Tier 2 streetwear:
        verified (Certilogo/Aura code) 100%, unverified 50%. Low-margin brands 10%, other designer brands 70%, counterfeit
        red flags 5%. Non-designer items return 1.0."""
        la = self.luxury.assess(title, text)
        return la.factor if la else 1.0

    def telecom_factor(self, title: str, text: str = "", imei_report=None) -> tuple[float, TelecomStatus]:
        """Risk-adjusted share of a CLEAN-device comp for cellular devices: clean 100%, moderate 50%, unverified 20%.

        'unverified' (the government telecom boilerplate, or untested / unable to test) assumes the worst case (bad ESN, blacklisted
        or locked), so the device is valued at parts value. Non-cellular items return 1.0. Nothing is discarded: if an unverified
        phone turns out clean on delivery, the upside is the whole difference.
        """
        ts = telecom_status(title, text)
        if not ts.mobile:
            return 1.0, ts
        if imei_report is not None and imei_report.verdict != "unknown":
            # a paid IMEI report beats every listing-text guess, including the government boilerplate
            if imei_report.verdict == "clean":
                return 1.0, TelecomStatus(True, "clean", False, [f"IMEI {imei_report.imei} verified: Find My OFF, blacklist clean"])
            log.warning("IMEI %s is locked/blacklisted. Applied 80%% haircut.", imei_report.imei)
            return 0.2, TelecomStatus(True, "unverified", ts.unverified_telecom_status,
                                      [f"IMEI {imei_report.imei} is iCloud-locked or blacklisted"])
        return {"clean": self.cfg.telecom_factor_clean, "moderate": self.cfg.telecom_factor_moderate,
                "unverified": self.cfg.telecom_factor_unverified}[ts.status], ts

    def condition_factor(self, title: str, text: str = "", imei_report=None) -> float:
        return self._with_luxury(self._condition_factor(title, text, imei_report), title, text)

    def _condition_factor(self, title: str, text: str = "", imei_report=None) -> float:
        base = {
            "locked": self.cfg.locked_condition_factor,
            "untested": self.cfg.risky_condition_factor,
            "refurbished": self.cfg.refurbished_condition_factor,
            "working": 1.0,
        }[condition(title)]
        tf, ts = self.telecom_factor(title, text, imei_report)
        return base if (not ts.mobile or ts.status == "clean") else min(base, tf)

    def detail_condition_factor(self, title: str, a: Assessment, imei_report=None) -> float:
        return self._with_luxury(self._detail_condition_factor(title, a, imei_report), title, a.text)

    def _detail_condition_factor(self, title: str, a: Assessment, imei_report=None) -> float:
        """Condition weight using the listing's own test results over title guesses.

        Activation-locked or dead => locked tier; explicit "powers on / works" => full value (or the
        refurbished uplift); explicitly untested => untested tier; otherwise fall back to the title.
        A lock/for-parts word in the TITLE always wins.
        """
        if is_locked(title) or a.activation_lock or a.dead:
            return self.cfg.locked_condition_factor
        if a.tested_ok:
            base = self.cfg.refurbished_condition_factor if condition(title) == "refurbished" else 1.0
        elif a.untested:
            base = self.cfg.risky_condition_factor
        else:
            return self._condition_factor(title, a.text, imei_report)
        # "powers on" says nothing about the carrier: a phone still needs its telecom risk applied from the listing text
        tf, ts = self.telecom_factor(title, a.text, imei_report)
        return base if (not ts.mobile or ts.status == "clean") else min(base, tf)

    def valuate(self, query: str, comps: Comps, price: float, shipping: float, title: str,
                buyer_premium_pct: float = 0.0, value_ceiling: float | None = None,
                condition_factor: float | None = None, fix_cost: float = 0.0) -> Valuation | None:
        """total_cost = price + price * premium% + shipping; None without enough matching sold comps."""
        factor = self.condition_factor(title) if condition_factor is None else condition_factor
        val = valuate(self.cfg, query, comps, price, shipping, title=title, condition_factor=factor,
                      extra_cost=price * buyer_premium_pct / 100 + fix_cost, value_ceiling=value_ceiling)
        if val is not None and fix_cost:
            val.detail = (val.detail + "; " if val.detail else "") + f"includes ${fix_cost:,.0f} est. to fix/replace parts"
        return val

    def unit_value(self, query: str, comps: Comps, title: str) -> tuple[float, int] | None:
        """Condition-weighted true market value of one unit, plus the comp count."""
        unit = estimate_unit_value(self.cfg, query, comps, title)
        return (unit[0] * self.condition_factor(title), unit[1]) if unit else None

    def valuate_melt(self, realizable: float, price: float, shipping: float, buyer_premium_pct: float = 0.0,
                     detail: str = "") -> Valuation | None:
        """Value = what a refiner/dealer pays (melt x payout). No marketplace fees; insured shipping to the buyer."""
        if realizable <= 0:
            return None
        cost = price * (1 + buyer_premium_pct / 100) + shipping
        return _finish(self.cfg, realizable, 1, cost, 1, "melt", detail, fee_pct=0.0, fixed_fee=0.0,
                       outbound=self.cfg.melt_ship_usd)

    def valuate_lot(self, lines: list[tuple[int, float | None]], lot_cost: float) -> Valuation | None:
        return valuate_lot(self.cfg, lines, lot_cost)
