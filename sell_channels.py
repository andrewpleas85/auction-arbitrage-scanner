"""Extra resale channels (beyond eBay and consignment) for the channel model, and "buyout floors" for instant exits.

Fees are the SELLER's cost and were relayed by Muse from its research (research-more-sell-channels.md has the sources; none was re-fetched
here). Only Swappa came with a stated price level (85-95% of eBay); for every other channel no price data exists, so each is assumed to
realize PRICE_FACTOR_DEFAULT of the eBay-sold comp value: an ASSUMPTION that stops an unverified cheaper fee from making eBay look worse.

A channel is "counted" in the verified gate only when `counts` is True. Local-only and live-selling variants are shown for information
(they need effort or have no sold-price evidence), the same as the Facebook local-cash row.

BUYOUT FLOORS are instant exits, not listing channels: Rebag pays about 40-70% of resale value for bags and watches. A find that clears
the profit bar even at the LOW end of a floor (40%) is the strongest signal the page can show.
Skipped on purpose (per ruling): Back Market, 1stDibs, Amazon Renewed, Decluttr (closed), Guitar Center trade-in (floor reference only).
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

PRICE_FACTOR_DEFAULT = 0.90   # ASSUMPTION: share of the eBay-sold comp value a non-eBay channel realizes when no better figure is known
SWAPPA_FACTOR = (0.85, 0.95)  # stated: Swappa prices run 85-95% of eBay; the midpoint is used
STREETWEAR = re.compile(r"\b(supreme|nike|jordan|yeezy|adidas|off[- ]white|palace|stussy|bape|kith|fear of god|essentials|travis scott|new balance|carhartt)\b", re.I)
MENSWEAR_CLOTHING = {"clothing"}
TECH = {"phone", "tablet", "laptop", "camera", "lens", "console", "desktop", "monitor", "headphones", "audio"}
GEAR = {"guitar", "amp", "audio", "microphone", "keys", "drums", "synth", "speaker"}


@dataclass
class Channel:
    name: str
    fee: callable                 # (sale_price, ship_buyer_paid) -> dollars of fees
    factor: float = PRICE_FACTOR_DEFAULT
    kinds: set | None = None
    title_rx: re.Pattern | None = None
    counts: bool = True
    ship_mode: str = "seller"     # 'seller' pays the label, 'buyer' pays it (listing price drops by the shipping so the buyer's total stays level), 'none' = local pickup
    note: str = ""
    src: str = "relayed by Muse from its sell-channel research (not re-fetched here)"
    price_basis: str = f"ASSUMPTION: {PRICE_FACTOR_DEFAULT:.0%} of the eBay comp value (no channel price data)"
    floor: bool = False

    def eligible(self, kind: str, title: str) -> bool:
        if self.kinds is not None and kind not in self.kinds and not (self.title_rx and self.title_rx.search(title or "")):
            return False
        return True


def _pct(rate: float, flat: float = 0.0):
    return lambda price, ship: price * rate + flat


REGISTRY: dict[str, Channel] = {c.name: c for c in [
    Channel("Swappa", _pct(0.065), factor=sum(SWAPPA_FACTOR) / 2, kinds={"phone", "tablet", "laptop", "camera", "console"},
            note="~6.5% all-in; prices run about 85-95% of eBay (midpoint used: the lower fee AND the lower price are both modelled)",
            price_basis="stated: 85-95% of eBay (midpoint 90%)"),
    Channel("Depop", _pct(0.033, 0.45), kinds={"clothing"}, note="0% commission in the US; about 3.3% + $0.45 payment processing"),
    Channel("Mercari", lambda price, ship: 0.10 * (price + ship), ship_mode="buyer",
            note="10% flat, charged on price PLUS buyer-paid shipping (so heavy items cost more); buyer pays the label"),
    Channel("Chrono24", _pct(0.0695), kinds={"watch"}, note="~6.95% with escrow payout"),
    Channel("Grailed", lambda price, ship: price * (0.06 if price < 120 else 0.09) + price * 0.0349, kinds=MENSWEAR_CLOTHING,
            note="9% (6% under $120) + ~3.49% payment processing; menswear / designer clothing"),
    Channel("StockX", lambda price, ship: price * 0.12 + 5.0, kinds={"shoes"}, title_rx=STREETWEAR,
            note="9% + 3% + $5 at the entry seller level; StockX authenticates, which de-risks seizure goods"),
    Channel("Sweetwater Gear Exchange", _pct(0.075), kinds=GEAR, note="7.5% for cash payout; instruments and pro audio"),
    Channel("eBay Live", _pct(0.089), factor=1.0, counts=False, price_basis="same eBay audience (price factor 1.0)",
            note="8.9% combined; an eBay variant that needs live selling, so shown for information and not counted"),
    Channel("OfferUp (local)", _pct(0.0), factor=0.85, counts=False, ship_mode="none", price_basis="ASSUMPTION: 85% of eBay comps, same as the Facebook local-cash row",
            note="local-only, 0% fee; a second local audience for heavy items; not counted until local sold data exists"),
]}


def options(kind: str, title: str, value: float, ship_out: float) -> dict:
    """{channel: {'revenue','fee','sale_price','counts','note','price_basis'}} for every extra channel eligible for this item.
    `value` is the eBay-comp value; revenue is net of the channel's fee and (when the seller ships) the outbound shipping."""
    out = {}
    for name, ch in REGISTRY.items():
        if not ch.eligible(kind, title):
            continue
        if ch.ship_mode == "seller":
            sale, ship_buyer, ship_cost = value * ch.factor, 0.0, ship_out
        elif ch.ship_mode == "buyer":  # the buyer pays shipping on top: assume their TOTAL stays at the comp level, so the item price is lower by the shipping
            sale, ship_buyer, ship_cost = value * ch.factor - ship_out, ship_out, 0.0
        else:
            sale, ship_buyer, ship_cost = value * ch.factor, 0.0, 0.0
        fee = ch.fee(sale, ship_buyer)
        out[name] = {"revenue": round(sale - fee - ship_cost, 2), "fee": round(fee, 2), "sale_price": round(sale, 2), "counts": ch.counts, "note": ch.note,
                     "price_basis": ch.price_basis, "source": ch.src, "ship_mode": ch.ship_mode}
    return out


# ---------------------------------------------------------------- instant-exit floors
BUYOUT_LOW, BUYOUT_HIGH = 0.40, 0.70   # Rebag pays about 40-70% of resale value (relayed by Muse)
FLOOR_KINDS = {"bag", "wallet", "watch"}


def buyout_floors(kind: str, title: str, value: float) -> list[dict]:
    """Instant-exit floor values. Only Rebag has a stated rate; the dealer watch buyers (Bob's Watches, 1916 Company) gave no percentage, so they
    are named with no number rather than guessed."""
    if kind not in FLOOR_KINDS:
        return []
    out = [{"name": "Rebag buyout", "low": round(value * BUYOUT_LOW, 2), "high": round(value * BUYOUT_HIGH, 2), "rate": [BUYOUT_LOW, BUYOUT_HIGH],
            "source": "about 40-70% of resale value (relayed by Muse)"}]
    if kind == "watch":
        out.append({"name": "Dealer watch buyouts (Bob's Watches, 1916 Company)", "low": None, "high": None, "rate": None, "source": "no rate supplied: not modelled"})
    return out


def floor_signal(floor_net: float | None, floor_return: float | None, net_bar: float, min_return: float) -> bool:
    """True when the find clears the profit bar even at the LOW end of the buyout floor."""
    return floor_net is not None and floor_net >= net_bar and (floor_return or 0) >= min_return
