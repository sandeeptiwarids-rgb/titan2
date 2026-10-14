"""Zerodha NSE-option charges and the single P&L function (spec §8.1, R3).

Every rupee of P&L in titan2 comes from net_pnl() over executed orders' fills: the ledger close, risk MTM (via
mtm_net), status.json, reports, the EOD truth check and G1. No function here takes a last-traded price: realized
P&L comes from fill values, and open quantity is valued only at the realizable bid handed to mtm_net().

Money is rupees as float. Each charge component is rounded to 2 dp per order, halves away from zero (contract-note
style), via round_money().
"""

from __future__ import annotations

import math
from dataclasses import dataclass, fields
from datetime import date, datetime
from decimal import ROUND_HALF_UP, Decimal
from typing import Iterable, Sequence

from titan2.model import Charges, PnL, Side

__all__ = [
    "AUTO_SQUAREOFF", "CHARGE_FIELDS", "EXTERNAL_KINDS", "MANUAL", "OrderFills", "OrderPart", "RATESETS",
    "RATES_VERIFIED_ON", "RateSet", "ZERO_CHARGES", "breakeven_px", "exercise_stt", "k_exit", "mtm_net", "net_pnl",
    "order_charges", "rates_for", "round_money", "round_trip_charges", "share",
]

MANUAL = "MANUAL"
AUTO_SQUAREOFF = "AUTO_SQUAREOFF"
EXTERNAL_KINDS = (MANUAL, AUTO_SQUAREOFF)    # Order.external_kind of role EXTERNAL; None for bot orders

CHARGE_FIELDS = tuple(f.name for f in fields(Charges))
ZERO_CHARGES = Charges(0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0)

_CENT = Decimal("0.01")


def _finite(x: object) -> bool:
    return isinstance(x, (int, float)) and not isinstance(x, bool) and math.isfinite(x)


def _plain_int(x: object) -> bool:
    return isinstance(x, int) and not isinstance(x, bool)


def round_money(x: float) -> float:
    """Rupees to 2 dp, halves away from zero (2.675 -> 2.68, -0.015 -> -0.02); never returns -0.0.

    Float noise is cleared at 6 dp first, so 0.015 (stored as 0.01499...) rounds like the decimal it stands for.
    """
    if not _finite(x):
        raise ValueError(f"money must be a finite number, got {x!r}")
    return float(Decimal(repr(round(x, 6))).quantize(_CENT, rounding=ROUND_HALF_UP)) + 0.0


# ---------------------------------------------------------------------------------------------------------------
# Dated rate sets
# ---------------------------------------------------------------------------------------------------------------
_FRACTION_FIELDS = ("stt_sell", "stt_exercise", "txn", "sebi", "ipft", "stamp_buy", "gst")


@dataclass(frozen=True, slots=True)
class RateSet:
    version: str
    effective_from: date
    brokerage_per_order: float   # Rs per EXECUTED order (options, flat); an unfilled IOC costs 0
    stt_sell: float              # on sell-side premium
    stt_exercise: float          # on intrinsic value of exercised ITM longs
    txn: float                   # NSE options transaction charge, on premium, both sides
    sebi: float                  # Rs 10/crore, both sides
    ipft: float                  # NSE IPFT on premium [V]
    stamp_buy: float             # buy side only
    gst: float                   # on brokerage + txn + sebi + ipft + sqoff_fee
    sqoff_fee: float             # Rs per broker auto-square-off order (before GST)

    def __post_init__(self) -> None:
        if not (isinstance(self.version, str) and self.version):
            raise ValueError(f"RateSet.version must be a non-empty str, got {self.version!r}")
        if not isinstance(self.effective_from, date) or isinstance(self.effective_from, datetime):
            raise ValueError(f"RateSet.effective_from must be a date, got {self.effective_from!r}")
        for name in _FRACTION_FIELDS:
            x = getattr(self, name)
            if not (_finite(x) and 0 <= x < 1):
                raise ValueError(f"RateSet {self.version}: {name}={x!r} is not a fraction in [0, 1)")
        for name in ("brokerage_per_order", "sqoff_fee"):
            x = getattr(self, name)
            if not (_finite(x) and x >= 0):
                raise ValueError(f"RateSet {self.version}: {name}={x!r} is not a rupee amount >= 0")


# Sources, verified 2026-10-10: zerodha.com/charges (STT on option sell premium and on exercise 0.15% since the
# Budget 2026-27 hike effective 2026-04-01, previously 0.10% / 0.125%; NSE transaction charge 0.03553% of premium;
# SEBI Rs 10/crore; stamp 0.003% on buys; GST 18%; auto square-off Rs 50 + GST per order); NSE circular NSE/FA/73061
# (Feb 27 2026); Zerodha bulletin 391488 (Oct 2024).
# 2024-10-01: NSE IPFT on option premium was Rs 50/crore (0.0005%) on top of the 0.03503% transaction charge, so
#   txn + ipft = 0.03553% (the reference cost model's exchange charge for Oct-2024..Feb-2026).
# 2026-04-01 [V]: IPFT is ~0 and the transaction charge alone is 0.03553%. Pin from a real contract note (Q5,
#   tests/unit/fixtures/contract_note.json).
RATESETS: tuple[RateSet, ...] = (
    RateSet("2024-10-01", date(2024, 10, 1), 20.0, 0.001, 0.00125, 0.0003503, 1e-6, 0.000005, 0.00003, 0.18, 50.0),
    RateSet("2026-04-01", date(2026, 4, 1), 20.0, 0.0015, 0.0015, 0.0003553, 1e-6, 0.0, 0.00003, 0.18, 50.0),
)

# The last day the newest RateSet was checked against Zerodha's charges page. A rates-staleness check should age
# from this date: the newest effective_from can be old while the rates are still current.
RATES_VERIFIED_ON = date(2026, 10, 10)


def rates_for(d: date) -> RateSet:
    """The RateSet in force on d (latest effective_from <= d). A datetime counts as its own (IST wall) date."""
    if isinstance(d, datetime):
        d = d.date()
    if not isinstance(d, date):
        raise TypeError(f"rates_for needs a date, got {type(d).__name__}")
    in_force = [r for r in RATESETS if r.effective_from <= d]
    if not in_force:
        raise ValueError(f"no RateSet in force on {d}")
    return max(in_force, key=lambda r: r.effective_from)


# ---------------------------------------------------------------------------------------------------------------
# Orders and charges
# ---------------------------------------------------------------------------------------------------------------
@dataclass(frozen=True, slots=True)
class OrderFills:
    """One order's executed quantity and value: value = sum(px * qty) over its effective fills."""

    side: Side
    filled_qty: int
    value: float
    external_kind: str | None = None   # EXTERNAL orders: MANUAL | AUTO_SQUAREOFF

    def __post_init__(self) -> None:
        object.__setattr__(self, "side", Side(self.side))
        q, v = self.filled_qty, self.value
        if not (_plain_int(q) and q >= 0):
            raise ValueError(f"filled_qty must be an int >= 0, got {q!r}")
        if not (_finite(v) and v >= 0):
            raise ValueError(f"value must be a finite number >= 0, got {v!r}")
        if (q == 0) != (v == 0):
            raise ValueError(f"filled_qty {q} and value {v} disagree: a fill has both or neither")
        if self.external_kind is not None and self.external_kind not in EXTERNAL_KINDS:
            raise ValueError(f"unknown external_kind {self.external_kind!r}")
        object.__setattr__(self, "value", float(v))

    @classmethod
    def from_fills(cls, side: Side, fills: Iterable, external_kind: str | None = None) -> OrderFills:
        """Aggregate one order's effective fills (model.Fill, BrokerTrade: anything with int .qty and .px)."""
        qty, parts = 0, []
        for f in fills:
            if not (_plain_int(f.qty) and f.qty > 0 and _finite(f.px) and f.px > 0):
                raise ValueError(f"bad fill qty={f.qty!r} px={f.px!r}")
            qty += f.qty
            parts.append(f.px * f.qty)
        return cls(side, qty, math.fsum(parts), external_kind)


def share(x: float, start: int, end: int, total: int) -> float:
    """The [start, end) share of an amount spread over total units, with cumulative rounding: the shares of
    consecutive ranges covering [0, total) add up exactly to round_money(x)."""
    return round_money(round_money(x * end / total) - round_money(x * start / total))


@dataclass(frozen=True, slots=True)
class OrderPart:
    """The share [start, end) of one executed order's quantity, booked on one leg.

    One broker order (the 15:26 square-off, or a manual close of the whole symbol) can close the legs of several
    books holding the same contract. Its value and each charge component are then allocated by quantity with
    cumulative rounding (share()), so the parts of one order add up exactly to the order: its brokerage and
    square-off fee are booked once, split between the books, and the paper ledger still equals the exchange's
    recompute to the paisa (§9.8). The order counts as executed once, on its first part.
    """

    whole: OrderFills
    start: int
    end: int

    def __post_init__(self) -> None:
        if not isinstance(self.whole, OrderFills):
            raise TypeError("OrderPart.whole must be an OrderFills")
        q = self.whole.filled_qty
        if not (_plain_int(self.start) and _plain_int(self.end) and 0 <= self.start < self.end <= q):
            raise ValueError(f"part [{self.start!r}, {self.end!r}) is not inside the order's {q} executed")

    @property
    def side(self) -> Side:
        return self.whole.side

    @property
    def filled_qty(self) -> int:
        return self.end - self.start

    @property
    def value(self) -> float:
        return share(self.whole.value, self.start, self.end, self.whole.filled_qty)

    @property
    def external_kind(self) -> str | None:
        return self.whole.external_kind


def order_charges(o: OrderFills | OrderPart, r: RateSet) -> Charges:
    """Charges for one order. Unfilled costs 0; brokerage once per executed order however many fills it had. An
    OrderPart gets its share of the whole order's charges, component by component."""
    if isinstance(o, OrderPart) and isinstance(r, RateSet):
        c = order_charges(o.whole, r)
        n = o.whole.filled_qty
        return Charges(**{name: share(getattr(c, name), o.start, o.end, n) for name in CHARGE_FIELDS})
    if not isinstance(o, OrderFills) or not isinstance(r, RateSet):
        raise TypeError("order_charges(OrderFills, RateSet)")
    if o.filled_qty == 0:
        return ZERO_CHARGES
    v = o.value
    brokerage = r.brokerage_per_order
    txn, sebi, ipft = r.txn * v, r.sebi * v, r.ipft * v
    sqoff_fee = r.sqoff_fee if o.external_kind == AUTO_SQUAREOFF else 0.0
    # GST on the unrounded components, as Zerodha's virtual contract note computes it; each part rounded after
    gst = r.gst * (brokerage + txn + sebi + ipft + sqoff_fee)
    return Charges(
        brokerage=round_money(brokerage),
        stt=round_money(r.stt_sell * v) if o.side is Side.SELL else 0.0,
        txn=round_money(txn),
        sebi=round_money(sebi),
        ipft=round_money(ipft),
        gst=round_money(gst),
        stamp=round_money(r.stamp_buy * v) if o.side is Side.BUY else 0.0,
        sqoff_fee=round_money(sqoff_fee),
    )


def net_pnl(orders: Sequence[OrderFills | OrderPart], r: RateSet) -> PnL:
    """The only P&L function: gross = sum(SELL value) - sum(BUY value); net = gross - sum of every order's charges.
    An OrderPart contributes its share of one order's value and charges."""
    sells: list[float] = []
    buys: list[float] = []
    parts: dict[str, list[float]] = {name: [] for name in CHARGE_FIELDS}
    executed = 0
    for o in orders:
        c = order_charges(o, r)
        if o.filled_qty > 0 and not (isinstance(o, OrderPart) and o.start > 0):
            executed += 1
        (sells if o.side is Side.SELL else buys).append(o.value)
        for name in CHARGE_FIELDS:
            parts[name].append(getattr(c, name))
    charges = Charges(**{name: round_money(math.fsum(xs)) for name, xs in parts.items()})
    gross = round_money(math.fsum(sells) - math.fsum(buys))
    return PnL(gross, charges, round_money(gross - charges.total), executed, r.version)


def mtm_net(done: Sequence[OrderFills], open_qty: int, rb: float | None, r: RateSet) -> float | None:
    """Net if the open quantity were sold now, in one order, at the realizable bid rb.

    Flat (open_qty 0): the realized net, whatever rb is. Open with rb None (no bids): None, i.e. MTM unknown,
    which blocks entries and the daily-stop decision but never an exit. The caller enforces rb freshness.
    """
    if not (_plain_int(open_qty) and open_qty >= 0):
        raise ValueError(f"open_qty must be an int >= 0, got {open_qty!r}")
    done = list(done)
    if open_qty == 0:
        return net_pnl(done, r).net
    if rb is None:
        return None
    if not (_finite(rb) and rb > 0):
        raise ValueError(f"realizable bid must be a finite price > 0, got {rb!r}")
    return net_pnl([*done, OrderFills(Side.SELL, open_qty, open_qty * rb)], r).net


# ---------------------------------------------------------------------------------------------------------------
# Closed forms used by exits and sizing
# ---------------------------------------------------------------------------------------------------------------
def k_exit(r: RateSet) -> float:
    """Exit charges per rupee sold, excluding the flat brokerage: STT + (txn + SEBI + IPFT) with GST (§7.1)."""
    return r.stt_sell + (r.txn + r.sebi + r.ipft) * (1 + r.gst)


def breakeven_px(entry_value: float, qty: int, c_in: float, r: RateSet) -> float:
    """The exact exit price at which selling qty in one order nets zero (§7.1), before 2-dp rounding of charges.

    entry_value = sum(px * qty) of the entry fills; c_in = the entry orders' actual charges.
    """
    if not (_plain_int(qty) and qty > 0):
        raise ValueError(f"qty must be an int > 0, got {qty!r}")
    if not (_finite(entry_value) and entry_value > 0):
        raise ValueError(f"entry_value must be a finite amount > 0, got {entry_value!r}")
    if not (_finite(c_in) and c_in >= 0):
        raise ValueError(f"c_in must be a finite amount >= 0, got {c_in!r}")
    return (entry_value + c_in + r.brokerage_per_order * (1 + r.gst)) / (qty * (1 - k_exit(r)))


def round_trip_charges(px: float, lot: int, r: RateSet) -> float:
    """Entry plus exit charges for one lot bought and sold at px (one order each way)."""
    if not (_plain_int(lot) and lot > 0):
        raise ValueError(f"lot must be an int > 0, got {lot!r}")
    if not (_finite(px) and px > 0):
        raise ValueError(f"px must be a finite price > 0, got {px!r}")
    value = px * lot
    buy = order_charges(OrderFills(Side.BUY, lot, value), r)
    sell = order_charges(OrderFills(Side.SELL, lot, value), r)
    return round_money(buy.total + sell.total)


def exercise_stt(intrinsic_per_unit: float, qty: int, r: RateSet) -> float:
    """STT on an exercised in-the-money long; an OTM expiry costs nothing.

    Never expected: MIS and the DTE rules close every position before expiry. Zerodha also charges one brokerage
    (+GST) on an exercise, which is not part of this figure.
    """
    if not (_plain_int(qty) and qty >= 0):
        raise ValueError(f"qty must be an int >= 0, got {qty!r}")
    if not _finite(intrinsic_per_unit):
        raise ValueError(f"intrinsic_per_unit must be finite, got {intrinsic_per_unit!r}")
    if intrinsic_per_unit <= 0 or qty == 0:
        return 0.0
    return round_money(r.stt_exercise * intrinsic_per_unit * qty)
