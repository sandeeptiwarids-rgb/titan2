"""The scenario universe: session dates, underlyings, option chains and their default books (spec §16.1).

Sessions come from the real titan2/data/nse_sessions.json. 2026-10-12 is a Monday session; NIFTY weeklies expire on
Tuesdays (2026-10-20 is a holiday, so that week's weekly expires Monday 2026-10-19); monthlies on the last Tuesday
(2026-10-27; 2026-11-24 is a holiday, so November's is Monday 2026-11-23). Lot sizes and ticks are per contract.

The incident names (HINDPETRO, SONACOMS, SAMMAANCAP, ...) are cosmetic: their spots and lots are chosen so the
scenario reaches the code path under test with today's caps (an entry needs a premium of at least Rs 20, at most
Rs 50,000 per lot and at most Rs 15,000 of risk per lot, so the real lots of some names could never be entered).
"""

from __future__ import annotations

import itertools
import math
from dataclasses import dataclass
from datetime import date

from titan2.clock import SessionCalendar
from titan2.instruments import InstrumentStore
from titan2.model import Instrument, Level, Underlying, floor_tick

SESSION = date(2026, 10, 12)          # Monday
PREV_SESSION = date(2026, 10, 9)      # Friday
NEXT_SESSION = date(2026, 10, 13)     # Tuesday
CALENDAR = SessionCalendar.load()
TICK = 0.05

STOCK_EXPIRIES = (date(2026, 10, 27), date(2026, 11, 23))
NIFTY_EXPIRIES = (date(2026, 10, 13), date(2026, 10, 19), date(2026, 10, 27))
BANKNIFTY_EXPIRIES = (date(2026, 10, 27), date(2026, 11, 23))
MONTHLY = frozenset({date(2026, 10, 27), date(2026, 11, 23)})


@dataclass(frozen=True)
class Spec:
    name: str
    token: int
    spot: float
    lot: int
    step: float
    tv_pct: float                     # ATM time value as a fraction of spot (the default book's premium)
    kind: str = "STOCK"
    n_strikes: int = 5                # strikes on each side of the spot

    @property
    def symbol(self) -> str:
        return {"NIFTY": "NSE:NIFTY 50", "BANKNIFTY": "NSE:NIFTY BANK"}.get(self.name, f"NSE:{self.name}")


SPECS: tuple[Spec, ...] = (
    Spec("NIFTY", 256265, 25000.0, 65, 50.0, 0.006, "INDEX", 6),
    Spec("BANKNIFTY", 260105, 56000.0, 30, 100.0, 0.007, "INDEX", 6),
    Spec("RELIANCE", 738561, 2900.0, 500, 20.0, 0.012),
    Spec("INFY", 408065, 1500.0, 400, 20.0, 0.024),
    Spec("SBIN", 779521, 820.0, 750, 10.0, 0.035),
    Spec("TCS", 2953217, 3200.0, 175, 20.0, 0.03),
    Spec("HINDPETRO", 359937, 400.0, 1000, 5.0, 0.055),
    Spec("ZYDUSLIFE", 2029825, 950.0, 900, 10.0, 0.025),
    Spec("SONACOMS", 4724609, 600.0, 600, 10.0, 0.045),
    Spec("SAMMAANCAP", 3699969, 150.0, 1000, 2.5, 0.15),
    Spec("NMDC", 3924993, 70.0, 6750, 1.0, 0.05),
    Spec("IDEA", 3677697, 70.0, 4300, 1.0, 0.05),
    Spec("ASHOKLEY", 54273, 125.0, 4300, 2.5, 0.2),
    Spec("BHEL", 112129, 250.0, 6000, 5.0, 0.1),
)
BY_NAME = {s.name: s for s in SPECS}
BY_SYMBOL = {s.symbol: s for s in SPECS}


def _month_code(d: date) -> str:
    return "123456789OND"[d.month - 1]


def tradingsymbol(name: str, expiry: date, strike: float, opt_type: str) -> str:
    """Kite style: monthly NAME + YY + MON (RELIANCE26OCT2900CE); weekly NAME + YY + M + DD (NIFTY26O1925000CE)."""
    k = f"{strike:g}"
    if expiry in MONTHLY:
        return f"{name}{expiry:%y}{expiry:%b}".upper() + f"{k}{opt_type}"
    return f"{name}{expiry:%y}{_month_code(expiry)}{expiry:%d}{k}{opt_type}"


def _expiries(spec: Spec) -> tuple[date, ...]:
    if spec.name == "NIFTY":
        return NIFTY_EXPIRIES
    if spec.name == "BANKNIFTY":
        return BANKNIFTY_EXPIRIES
    return STOCK_EXPIRIES


def strikes(spec: Spec) -> list[float]:
    return [round(spec.spot + i * spec.step, 2) for i in range(-spec.n_strikes, spec.n_strikes + 1)]


def _contracts() -> list[Instrument]:
    out, tok = [], itertools.count(20_000_001)
    for spec in SPECS:
        for expiry in _expiries(spec):
            for k in strikes(spec):
                for ot in ("CE", "PE"):
                    ts = tradingsymbol(spec.name, expiry, k, ot)
                    out.append(Instrument(f"NFO:{ts}", next(tok), ts, "NFO", spec.name, spec.symbol, ot, k, expiry,
                                          spec.lot, TICK))
    return out


CONTRACTS: tuple[Instrument, ...] = tuple(_contracts())
UNDERLYINGS: tuple[Underlying, ...] = tuple(Underlying(s.symbol, s.token, s.name, s.kind) for s in SPECS)


def instrument_store(session: date = SESSION) -> InstrumentStore:
    """Today's dump: contracts that have not expired before `session` (an expired row fails the sanity check)."""
    live = [c for c in CONTRACTS if c.expiry >= session]
    return InstrumentStore(session, CALENDAR, live, UNDERLYINGS, min_stocks=0)


def premium(spec: Spec, strike: float, opt_type: str, spot: float | None = None) -> float:
    """The default fair price: intrinsic + a time value that fades away from the money."""
    s = spec.spot if spot is None else spot
    intrinsic = max(s - strike, 0.0) if opt_type == "CE" else max(strike - s, 0.0)
    tv = s * spec.tv_pct * max(0.25, 1.0 - abs(strike - s) / (spec.step * 6))
    return max(TICK, floor_tick(intrinsic + tv, TICK))


def ladder(best: float, side: str, qty: int, n: int = 5, step: float = TICK) -> tuple[Level, ...]:
    """n levels from best outward (bids down, asks up), each holding qty."""
    sign = -1 if side == "bid" else 1
    out = []
    for i in range(n):
        px = round(best + sign * i * step, 2)
        if px > 0:
            out.append(Level(px, qty, max(1, qty // 500)))
    return tuple(out)


def default_book(inst: Instrument, spot: float | None = None) -> tuple[tuple[Level, ...], tuple[Level, ...], float]:
    """(bids, asks, ltp) of a liquid default book: 2-tick spread under Rs 50 (else ~0.2%), 4 lots per level."""
    spec = BY_SYMBOL[inst.underlying]
    fair = premium(spec, inst.strike, inst.opt_type, spot)
    spread = 2 * TICK if fair < 50 else max(2 * TICK, floor_tick(fair * 0.002, TICK))
    bid = max(TICK, floor_tick(fair - spread / 2, TICK))
    ask = round(bid + spread, 2)
    qty = 4 * inst.lot_size
    return ladder(bid, "bid", qty), ladder(ask, "ask", qty), round((bid + ask) / 2, 2)


def symbol_of(store: InstrumentStore, underlying: str, opt_type: str, pref: str, spot: float,
              session: date = SESSION) -> Instrument:
    """The contract the gateway will select for (underlying, opt_type, pref) at this spot."""
    c = store.select_contract(underlying, opt_type, pref, spot, session)
    if not isinstance(c, Instrument):
        raise AssertionError(f"no contract for {underlying} {opt_type} {pref} at {spot}: {c}")
    return c


def is_round(x: float) -> bool:
    return math.isclose(x, round(x, 2), abs_tol=1e-9)
