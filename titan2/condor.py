"""NIFTY weekly far-OTM iron condor: a PAPER tracker (docs/research/edge_research_verdict.md, "Paper-trading spec").

It places no order of any kind and has no order path. It reads Kite (quotes, day bars, basket margins), decides as
the spec says, and books paper fills in its own database, <var>/db/condor.db. Purpose: check the backtest's
assumptions (real credits and spreads at 15:25, the 3x stop, settlement) on live data for 52 weeks. Paper cannot
prove the edge itself; that would take about 200 trades.

The rules, from the spec:
- Contract: the nearest NIFTY weekly expiry (Tuesday, or the trading day before when Tuesday is a holiday: the
  instruments dump is the authority), 1 lot (the dump's lot size), one condor per expiry and one open at a time. A
  condor expiring today needs no more decisions and settles at today's close, so it does not block the next entry.
- Entry at about 15:25 on the 3rd trading day before expiry (for the Monday 2026-10-19 expiry, Wednesday 10-14):
  F = K + C - P at the strike whose call and put mids are closest; sigma = the Black-76 IV of that straddle with
  T = 3/252 (trading days); d = 1.5 sigma sqrt(3/252). Short put = F e^-d rounded down to 50, short call = F e^d
  rounded up to 50, wings W = 2% of F (rounded to 50) further out; a strike that is not listed moves further out.
  The week is skipped when a leg has no bid, no ask or a crossed or locked book (bid at or above the ask), when a
  quote is stale or unstamped, or when the credit is not positive.
- Fills: wings bought at the ask, shorts sold at the bid, each walking the depth for the lot; the 2x-spread version
  pays one more half-spread on every leg. Zerodha's charges on every order (titan2.charges).
- Each later trading day but expiry, at about 15:25: if the condor's mid value is >= 3x the entry credit, close it
  (shorts bought back at the ask, then wings sold at the bid). A leg with no bid is marked at half its ask; a wing
  with no bid is abandoned (no order, nothing back); a short with no ask postpones the stop by a day. No stop on
  expiry day: cash settlement at NIFTY 50's official close (Kite's day bar), Rs 20 + GST per in-the-money leg and
  exercise STT on in-the-money longs. Every condor also gets a no-stop result (held to settlement), for information.
- Kill rules: after 12 entries, the median real credit is below 85% of the same strikes' day-close credit, or the
  median real spread is above 2x the research cost model's; <= 19 wins in the first 26 or <= 41 in the first 52;
  cumulative net at 2x spreads below -Rs 42,000; any loss beyond 1.05x its computed max loss, or a stored net its
  fills do not reproduce. A kill stops new entries; an open condor is still marked and settled.
Not built: the T-2/T-1 entry shadows, and the bhavcopy download (each leg's Kite day close stands in for it).

The laptop calls CondorJob.poll() from its supervisor thread on real-time sessions only (never under --date):
settle_due() once after the start, tick() once a trading day in [15:25, 15:29:30), tried up to 3 times 45 s apart
while a step fails. A tick counts as done only when no step failed. A missed 15:25 (laptop off) misses that day's
entry or stop check; settlement catches up at any later start. `titan2 condor [status|dry-run|tick|settle]` does
the same by hand; tick and settle refuse while a laptop session runs (it does them itself), and every write is
conditional on the state it read, so two writers can never book one entry, exit or settlement twice.
"""

from __future__ import annotations

import json
import logging
import math
import sqlite3
import statistics
import threading
import time
from dataclasses import dataclass, field
from datetime import date, datetime, time as dtime, timedelta
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping, Sequence

from titan2 import charges
from titan2.charges import OrderFills, RateSet
from titan2.clock import IST
from titan2.model import Side

log = logging.getLogger(__name__)
Out = Callable[[str], Any]

DB_FILE = "condor.db"
NAME = "NIFTY"
SPOT_SYMBOL, SPOT_TOKEN = "NSE:NIFTY 50", 256265
VIX_SYMBOL = "NSE:INDIA VIX"
ENTRY_TD = 3                       # enter on the 3rd trading day before expiry
SD_MULT = 1.5
WING_PCT = 0.02
STEP = 50.0                        # strike rounding
STOP_MULT = 3.0
TICK_AT, TICK_UNTIL = dtime(15, 25), dtime(15, 29, 30)
TICK_ATTEMPTS, RETRY_AFTER_S = 3, 45.0
QUOTE_MAX_AGE_S = 60.0
ATM_PCT = 0.03                     # the forward and IV come from the strikes within 3% of spot
KITE_PAUSE_S = 1.1                 # between this client's Kite calls: quote() allows 1 a second
KILL_MIN_ENTRIES = 12
KILL_CREDIT_RATIO, KILL_SPREAD_RATIO = 0.85, 2.0
KILL_WINS = ((26, 19), (52, 41))   # (first n trades, at most this many wins kills)
KILL_NET_2X = -42_000.0
KILL_LOSS_VS_MAX = 1.05

SCHEMA = (
    """CREATE TABLE IF NOT EXISTS condors(
        id INTEGER PRIMARY KEY, expiry TEXT NOT NULL UNIQUE, entry_day TEXT NOT NULL,
        state TEXT NOT NULL CHECK (state IN ('OPEN', 'CLOSED')), qty INTEGER NOT NULL CHECK (qty > 0),
        credit REAL NOT NULL, credit_2x REAL NOT NULL, max_loss REAL NOT NULL, entry TEXT NOT NULL,
        close_credit REAL, close_legs TEXT, exit_reason TEXT, exit_day TEXT, exit TEXT,
        settle_px REAL, net REAL, net_2x REAL, net_nostop REAL, result TEXT)""",
    """CREATE TABLE IF NOT EXISTS marks(
        condor_id INTEGER NOT NULL, day TEXT NOT NULL, ts TEXT NOT NULL, value_mid REAL, ratio REAL,
        quotes TEXT NOT NULL, PRIMARY KEY (condor_id, day))""",
    "CREATE TABLE IF NOT EXISTS notes(id INTEGER PRIMARY KEY, ts TEXT NOT NULL, day TEXT NOT NULL, kind TEXT NOT NULL, "
    "detail TEXT NOT NULL)",
    "CREATE TABLE IF NOT EXISTS ticks(day TEXT PRIMARY KEY, ts TEXT NOT NULL, outcome TEXT NOT NULL)",
)
LEG_ROLES = ("SP", "SC", "LP", "LC")            # short put, short call, long put (wing), long call (wing)
SHORT = {"SP": True, "SC": True, "LP": False, "LC": False}


class CondorError(Exception):
    """A Kite call failed after its retries: the step that made it books nothing and is noted."""


# ---------------------------------------------------------------------------------------------------------------
# Pricing
# ---------------------------------------------------------------------------------------------------------------
def _ncdf(x: float) -> float:
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def black76(F: float, K: float, T: float, sigma: float, kind: str) -> float:
    """Black-76 price of a European CE or PE on forward F, rates taken as 0 (a 3-day hold)."""
    if T <= 0 or sigma <= 0:
        return max(0.0, F - K) if kind == "CE" else max(0.0, K - F)
    v = sigma * math.sqrt(T)
    d1 = (math.log(F / K) + 0.5 * v * v) / v
    d2 = d1 - v
    if kind == "CE":
        return F * _ncdf(d1) - K * _ncdf(d2)
    return K * _ncdf(-d2) - F * _ncdf(-d1)


def straddle_iv(price: float, F: float, K: float, T: float) -> float | None:
    """The sigma at which the K straddle (CE + PE) is worth `price`; None when no sigma in (0, 5] fits."""
    def gap(s: float) -> float:
        return black76(F, K, T, s, "CE") + black76(F, K, T, s, "PE") - price

    lo, hi = 1e-4, 5.0
    if not (price > 0 and T > 0) or gap(lo) > 0 or gap(hi) < 0:
        return None
    for _ in range(100):
        mid = (lo + hi) / 2
        if gap(mid) > 0:
            hi = mid
        else:
            lo = mid
    return (lo + hi) / 2


def model_spread(mid: float, otm: float, dte_trading: int) -> float:
    """The research cost model's full bid-ask spread (Rs/unit) of a NIFTY weekly (cost_model.py, calibrated on the
    2026-10-09 15:30 chain): (otm upper bound, rupee floor, fraction of mid) per bucket."""
    table = ([(0.005, 0.30, 0.006), (0.015, 0.30, 0.010), (0.025, 0.30, 0.035), (0.035, 0.20, 0.060), (9, 0.15, 0.080)]
             if dte_trading <= 2 else
             [(0.005, 1.00, 0.009), (0.015, 1.00, 0.013), (0.025, 0.80, 0.022), (0.035, 0.60, 0.033), (9, 0.50, 0.060)])
    for bound, floor_rs, pct in table:
        if abs(otm) <= bound:
            return max(floor_rs, pct * mid, 0.05)
    return max(0.50, 0.06 * mid)


# ---------------------------------------------------------------------------------------------------------------
# Contracts and quotes
# ---------------------------------------------------------------------------------------------------------------
@dataclass(frozen=True, slots=True)
class Opt:
    symbol: str                    # "NFO:<tradingsymbol>"
    token: int
    strike: float
    kind: str                      # CE | PE
    lot: int
    tick: float


@dataclass(frozen=True, slots=True)
class Q:
    bid: float | None
    ask: float | None
    ltp: float | None
    bids: tuple[tuple[float, int], ...]
    asks: tuple[tuple[float, int], ...]
    age_s: float | None

    @property
    def mid(self) -> float | None:
        return (self.bid + self.ask) / 2 if self.bid and self.ask and self.ask >= self.bid else None

    @property
    def spread(self) -> float | None:
        return self.ask - self.bid if self.mid is not None else None

    @property
    def two_sided(self) -> bool:
        """A bid, an ask and a positive spread. A crossed (ask < bid) or locked (ask == bid) book has a side left
        behind by a move of the other, which no exchange shows (the paper exchange's rule too, paper model 2), so
        the entry skips the week on such a leg (_stale), as for a missing side. A locked leg used to pass with its
        mid and book a zero spread, in the real and the 2x fill and in the kill rule's median spread."""
        return self.mid is not None and self.ask > self.bid

    def to_json(self) -> dict[str, Any]:
        return {"bid": self.bid, "ask": self.ask, "ltp": self.ltp, "age_s": self.age_s,
                "bids": [list(x) for x in self.bids], "asks": [list(x) for x in self.asks]}


def _expiry_of(x: Any) -> date:
    return x if isinstance(x, date) and not isinstance(x, datetime) else date.fromisoformat(str(x)[:10])


def nifty_chain(rows: Iterable[Mapping[str, Any]]) -> dict[date, dict[tuple[float, str], Opt]]:
    """NIFTY options of the NFO dump: expiry -> (strike, kind) -> Opt."""
    out: dict[date, dict[tuple[float, str], Opt]] = {}
    for r in rows:
        if (r.get("name") != NAME or r.get("instrument_type") not in ("CE", "PE")
                or r.get("segment") not in (None, "NFO-OPT")):
            continue
        o = Opt("NFO:" + str(r["tradingsymbol"]), int(r["instrument_token"]), float(r["strike"]),
                str(r["instrument_type"]), int(r["lot_size"]), float(r.get("tick_size") or 0.05))
        out.setdefault(_expiry_of(r["expiry"]), {})[(o.strike, o.kind)] = o
    return out


def nifty_future(rows: Iterable[Mapping[str, Any]], today: date) -> str | None:
    """The nearest NIFTY future of the NFO dump (logged with each entry)."""
    futs = sorted((_expiry_of(r["expiry"]), "NFO:" + str(r["tradingsymbol"])) for r in rows
                  if r.get("name") == NAME and r.get("instrument_type") == "FUT")
    return next((sym for exp, sym in futs if exp >= today), None)


def _ist(t: Any) -> datetime | None:
    if not isinstance(t, datetime):
        return None
    return t.replace(tzinfo=IST) if t.tzinfo is None else t


def parse_quote(raw: Mapping[str, Any] | None, now: datetime) -> Q | None:
    """A Kite quote() entry; levels with no price or no quantity dropped; age from its exchange timestamp (None
    when it carries none)."""
    if not raw:
        return None
    depth = raw.get("depth") or {}

    def levels(side: str) -> tuple[tuple[float, int], ...]:
        return tuple((float(x["price"]), int(x["quantity"])) for x in depth.get(side) or ()
                     if float(x.get("price") or 0) > 0 and int(x.get("quantity") or 0) > 0)

    bids, asks = levels("buy"), levels("sell")
    stamp = _ist(raw.get("timestamp")) or _ist(raw.get("last_trade_time"))
    age = None if stamp is None else max(0.0, (now - stamp).total_seconds())
    ltp = raw.get("last_price")
    return Q(bids[0][0] if bids else None, asks[0][0] if asks else None,
             float(ltp) if isinstance(ltp, (int, float)) and ltp > 0 else None, bids, asks, age)


def walk(levels: Sequence[tuple[float, int]], qty: int) -> tuple[float | None, bool]:
    """Average price for qty taken through the levels (best first); the part beyond the shown depth is assumed at
    the last level shown (thin=True). (None, True) on an empty side."""
    if not levels:
        return None, True
    left, value = qty, 0.0
    for px, q in levels:
        take = min(left, q)
        value += take * px
        left -= take
        if left == 0:
            return value / qty, False
    return (value + left * levels[-1][0]) / qty, True


def _tick_round(px: float, tick: float, up: bool) -> float:
    n = px / tick
    return round((math.ceil(n - 1e-9) if up else math.floor(n + 1e-9)) * tick, 2)


def fill_prices(q: Q, sell: bool, qty: int, tick: float) -> dict[str, Any]:
    """One leg's paper fill: real (a sell walks the bids, a buy the asks) and 2x (one more half-spread against us).
    A sell into no bid is an abandoned leg: no order, nothing back. A buy from no ask has no price (None)."""
    px, thin = walk(q.bids if sell else q.asks, qty)
    if px is None:
        return ({"real": 0.0, "x2": 0.0, "thin": True, "abandoned": True} if sell else
                {"real": None, "x2": None, "thin": True})
    half = (q.spread or 0.0) / 2
    x2 = max(_tick_round(px - half, tick, up=False), tick) if sell else _tick_round(px + half, tick, up=True)
    return {"real": round(px, 4), "x2": x2, "thin": thin}


def leg_mark(q: Q | None, short: bool) -> float | None:
    """A leg's mark: its mid; with no bid, half its ask (the middle of 0 and the ask). A short with no ask cannot be
    bought back: None (unknown). A wing with no ask is marked at its bid, else 0."""
    if q is not None and q.mid is not None:
        return q.mid
    if q is not None and q.ask is not None:
        return q.ask / 2
    if short:
        return None
    return q.bid if q is not None and q.bid is not None else 0.0


# ---------------------------------------------------------------------------------------------------------------
# Calendar and strikes
# ---------------------------------------------------------------------------------------------------------------
def entry_day_for(expiry: date, cal: Any) -> date | None:
    """The trading day with exactly ENTRY_TD trading days after it, expiry included; None when the calendar does
    not cover the expiry (its holidays are unknown)."""
    if not cal.covers(expiry):
        return None
    d: date | None = expiry
    for _ in range(ENTRY_TD):
        d = cal.prev_trading_day(d)
        if d is None:
            return None
    return d


def trading_days_left(today: date, expiry: date, cal: Any) -> int:
    """Trading days after today up to and including expiry."""
    return len(cal.trading_days(today + timedelta(days=1), expiry))


def round_down(x: float) -> float:
    return math.floor(x / STEP + 1e-9) * STEP


def round_up(x: float) -> float:
    return math.ceil(x / STEP - 1e-9) * STEP


def listed(strikes: Sequence[float], target: float, outward: str) -> float | None:
    """target if listed, else the nearest listed strike further out ('down' for puts, 'up' for calls)."""
    if target in strikes:
        return target
    pool = [k for k in strikes if (k < target if outward == "down" else k > target)]
    if not pool:
        return None
    return max(pool) if outward == "down" else min(pool)


@dataclass(frozen=True, slots=True)
class Plan:
    F: float
    sigma: float
    d: float
    atm: float
    strikes: dict[str, float]      # role -> strike
    legs: dict[str, Opt]


def plan_strikes(chain: Mapping[tuple[float, str], Opt], quotes: Mapping[str, Q | None], spot: float,
                 td: int) -> Plan | str:
    """The spec's strikes from the ATM mids, or why not."""
    T = td / 252.0
    pairs = []
    for (k, kind), o in chain.items():
        if kind != "CE" or abs(k / spot - 1) > ATM_PCT:
            continue
        p = chain.get((k, "PE"))
        qc, qp = quotes.get(o.symbol), quotes.get(p.symbol) if p else None
        if p is None or qc is None or qp is None or qc.mid is None or qp.mid is None:
            continue
        pairs.append((abs(qc.mid - qp.mid), k, qc.mid, qp.mid))
    if not pairs:
        return "NO_ATM_QUOTES: no strike near spot has a two-sided call and put"
    _, atm, c, p = min(pairs)
    F = atm + c - p
    sigma = straddle_iv(c + p, F, atm, T)
    if sigma is None:
        return f"NO_IV: the {atm:.0f} straddle at {c + p:.2f} fits no volatility"
    d = SD_MULT * sigma * math.sqrt(ENTRY_TD / 252.0)
    w = round(WING_PCT * F / STEP) * STEP
    puts = sorted(k for (k, kind) in chain if kind == "PE")
    calls = sorted(k for (k, kind) in chain if kind == "CE")
    sp = listed(puts, round_down(F * math.exp(-d)), "down")
    sc = listed(calls, round_up(F * math.exp(d)), "up")
    if sp is None or sc is None:
        return "NO_STRIKE: the short strikes are beyond the listed chain"
    lp, lc = listed(puts, sp - w, "down"), listed(calls, sc + w, "up")
    if lp is None or lc is None:
        return "NO_STRIKE: the wings are beyond the listed chain"
    strikes = {"SP": sp, "SC": sc, "LP": lp, "LC": lc}
    legs = {r: chain[(k, "PE" if r in ("SP", "LP") else "CE")] for r, k in strikes.items()}
    return Plan(F, sigma, d, atm, strikes, legs)


# ---------------------------------------------------------------------------------------------------------------
# Money
# ---------------------------------------------------------------------------------------------------------------
def order_cost(side: Side, qty: int, px: float, rates: RateSet) -> float:
    return charges.order_charges(OrderFills(side, qty, qty * px), rates).total


def _money(fills: Mapping[str, Mapping[str, Any]], qty: int, rates: RateSet, key: str,
           opening: bool) -> tuple[float, float]:
    """(per-unit cash of the four legs, charges of their orders): opening sells the shorts and buys the wings, so
    the cash is the credit; closing buys the shorts back and sells the wings, so it is the debit. An abandoned leg
    (price 0) sends no order and costs nothing."""
    cash = sum((1 if SHORT[r] else -1) * fills[r][key] for r in LEG_ROLES)
    cost = sum(order_cost(Side.SELL if SHORT[r] == opening else Side.BUY, qty, fills[r][key], rates)
               for r in LEG_ROLES if fills[r][key])
    return cash, round(cost, 2)


def entry_money(fills: Mapping[str, Mapping[str, Any]], qty: int, rates: RateSet, key: str) -> tuple[float, float]:
    """(credit per unit, charges of the four opening orders) at fill prices `key` (real | x2)."""
    return _money(fills, qty, rates, key, opening=True)


def exit_money(fills: Mapping[str, Mapping[str, Any]], qty: int, rates: RateSet, key: str) -> tuple[float, float]:
    """(debit per unit, charges of the closing orders): shorts bought back, wings sold (or abandoned)."""
    return _money(fills, qty, rates, key, opening=False)


def intrinsic(kind: str, strike: float, settle: float) -> float:
    return max(0.0, settle - strike) if kind == "CE" else max(0.0, strike - settle)


def settlement(strikes: Mapping[str, float], settle: float, qty: int, rates: RateSet) -> tuple[float, float]:
    """(value owed per unit at expiry, settlement charges): Rs 20 + GST per in-the-money leg, exercise STT on the
    in-the-money longs."""
    value, cost = 0.0, 0.0
    for r in LEG_ROLES:
        i = intrinsic("PE" if r in ("SP", "LP") else "CE", strikes[r], settle)
        value += i if SHORT[r] else -i
        if i > 0:
            cost += rates.brokerage_per_order * (1 + rates.gst)
            if not SHORT[r]:
                cost += charges.exercise_stt(i, qty, rates)
    return value, round(cost, 2)


def max_loss(strikes: Mapping[str, float], credit: float, qty: int, entry_cost: float, rates: RateSet) -> float:
    """The worst settlement: the wider side fully in the money, both its legs exercised (exercise STT counted on
    one width; an index far beyond the wing adds a little more)."""
    width = max(strikes["SP"] - strikes["LP"], strikes["LC"] - strikes["SC"])
    exit_cost = 2 * rates.brokerage_per_order * (1 + rates.gst) + rates.stt_exercise * width * qty
    return round((width - credit) * qty + entry_cost + exit_cost, 2)


# ---------------------------------------------------------------------------------------------------------------
# The database
# ---------------------------------------------------------------------------------------------------------------
def db_path(var: Path) -> Path:
    return Path(var) / "db" / DB_FILE


def connect(var: Path, readonly: bool = False) -> sqlite3.Connection:
    path = db_path(var)
    if readonly:
        con = sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True, timeout=10)
    else:
        path.parent.mkdir(parents=True, exist_ok=True)
        con = sqlite3.connect(path, timeout=10)
        for ddl in SCHEMA:
            con.execute(ddl)
        con.commit()
    con.row_factory = sqlite3.Row
    return con


def note(con: sqlite3.Connection, now: datetime, kind: str, detail: str, out: Out | None = None) -> None:
    con.execute("INSERT INTO notes(ts, day, kind, detail) VALUES (?, ?, ?, ?)",
                (now.isoformat(), now.date().isoformat(), kind, detail))
    con.commit()
    log.info("condor %s: %s", kind, detail)
    if out is not None:
        out(f"condor: {detail}")


def _rows(con: sqlite3.Connection, where: str = "1", params: Sequence[Any] = ()) -> list[sqlite3.Row]:
    return con.execute(f"SELECT * FROM condors WHERE {where} ORDER BY expiry", params).fetchall()


# ---------------------------------------------------------------------------------------------------------------
# Kite, through one paced and retried path
# ---------------------------------------------------------------------------------------------------------------
@dataclass
class Ctx:
    var: Path
    kite: Any
    cal: Any
    now: datetime                  # the step's start (IST); wall() moves on from it
    out: Out = print
    sleep: Callable[[float], Any] = time.sleep
    mono: Callable[[], float] = time.monotonic
    t0: float = field(default=0.0, init=False)
    last_call: float | None = field(default=None, init=False)

    def __post_init__(self) -> None:
        self.t0 = self.mono()

    @property
    def today(self) -> date:
        return self.now.date()

    def wall(self) -> datetime:
        return self.now + timedelta(seconds=max(0.0, self.mono() - self.t0))

    def call(self, fn: Callable[[], Any], what: str) -> Any:
        """One Kite call at least KITE_PAUSE_S after this client's previous one, retried twice on a rate limit or
        a network error."""
        for attempt in range(3):
            if self.last_call is not None:
                wait = KITE_PAUSE_S - (self.mono() - self.last_call)
                if wait > 0:
                    self.sleep(wait)
            self.last_call = self.mono()
            try:
                return fn()
            except Exception as exc:
                text = f"{type(exc).__name__}: {exc}"
                if attempt == 2 or not any(s in text for s in ("429", "Too many", "Timeout", "timed out",
                                                               "Connection", "NetworkException")):
                    raise CondorError(f"{what} failed: {text[:200]}") from exc
                self.sleep(1.5 * (attempt + 1))
        raise AssertionError("unreachable")


def fetch_quotes(ctx: Ctx, symbols: Sequence[str]) -> dict[str, Q | None]:
    syms = list(dict.fromkeys(s for s in symbols if s))
    raw = ctx.call(lambda: ctx.kite.quote(syms), "quote") or {}
    now = ctx.wall()
    return {s: parse_quote(raw.get(s), now) for s in syms}


def day_close(ctx: Ctx, token: int, day: date) -> float | None:
    start = datetime.combine(day, dtime(0, 0))
    bars = ctx.call(lambda: ctx.kite.historical_data(token, start, start + timedelta(hours=23, minutes=59), "day"),
                    "historical_data") or []
    for b in bars:
        t = b.get("date")
        if (t.date() if isinstance(t, datetime) else _expiry_of(t)) == day:
            return float(b["close"])
    return None


def basket_margin(ctx: Ctx, legs: Mapping[str, Opt], fills: Mapping[str, Mapping[str, Any]],
                  qty: int) -> float | None:
    """Kite's margin for the four legs as one NRML basket, wings first (information only); None when Kite will not
    say."""
    orders = [{"exchange": "NFO", "tradingsymbol": legs[r].symbol.split(":", 1)[1],
               "transaction_type": "SELL" if SHORT[r] else "BUY", "variety": "regular", "product": "NRML",
               "order_type": "LIMIT", "quantity": qty, "price": fills[r]["real"]} for r in ("LP", "LC", "SP", "SC")]
    try:
        res = ctx.call(lambda: ctx.kite.basket_order_margins(orders, consider_positions=False), "basket margins")
        return float(((res or {}).get("final") or {}).get("total"))
    except Exception as exc:
        log.warning("condor: no basket margin: %s", exc)
        return None


def load_chain(ctx: Ctx) -> tuple[dict[date, dict], str | None]:
    """(today's NIFTY chain, the nearest NIFTY future) from the morning's instruments dump; Kite's NFO list when the
    dump is not there."""
    from titan2 import instruments

    dump = instruments.load_dump(Path(ctx.var) / "cache", ctx.today)
    rows = dump[0] if dump else ctx.call(lambda: ctx.kite.instruments("NFO"), "instruments")
    rows = rows or []
    return nifty_chain(rows), nifty_future(rows, ctx.today)


def kite_from_session(env: Mapping[str, str], var: Path) -> Any:
    from titan2 import token as token_mod

    sess = token_mod.read_session(token_mod.session_path(var))
    return token_mod.kite_client(str(env.get("ZERODHA_API_KEY", "")).strip(), sess.access_token)


# ---------------------------------------------------------------------------------------------------------------
# The steps
# ---------------------------------------------------------------------------------------------------------------
def _stale(qs: Mapping[str, Q | None], max_age: float | None = QUOTE_MAX_AGE_S) -> str | None:
    for sym, q in qs.items():
        if q is None or not q.two_sided:                 # a missing side, or a crossed or locked book
            return f"NO_TWO_SIDED_QUOTE: {sym}"
        if max_age is not None and q.age_s is None:
            return f"UNSTAMPED_QUOTE: {sym} carries no exchange time"
        if max_age is not None and q.age_s > max_age:
            return f"STALE_QUOTE: {sym} is {q.age_s:.0f} s old"
    return None


def build_entry(ctx: Ctx, expiry: date, chain: Mapping[tuple[float, str], Opt], td: int, fut: str | None = None,
                max_age: float | None = QUOTE_MAX_AGE_S) -> dict[str, Any] | str:
    """The entry record of a condor on `expiry`, or why not, from three snapshots: spot, VIX and the future; the
    strikes near spot (forward and IV); then the four legs (the fills). max_age None (dry runs only) accepts quotes
    of any age."""
    top = fetch_quotes(ctx, [SPOT_SYMBOL, VIX_SYMBOL, fut or ""])
    spot_q, vix_q, fut_q = top.get(SPOT_SYMBOL), top.get(VIX_SYMBOL), top.get(fut) if fut else None
    if spot_q is None or not spot_q.ltp:
        return "NO_SPOT: Kite gave no NIFTY 50 price"
    spot = spot_q.ltp
    near = fetch_quotes(ctx, [o.symbol for (k, _), o in chain.items() if abs(k / spot - 1) <= ATM_PCT])
    plan = plan_strikes(chain, near, spot, td)
    if isinstance(plan, str):
        return plan
    legs_q = fetch_quotes(ctx, [plan.legs[r].symbol for r in LEG_ROLES])
    leg_q = {r: legs_q.get(o.symbol) for r, o in plan.legs.items()}
    why = _stale({plan.legs[r].symbol: q for r, q in leg_q.items()}, max_age)
    if why:
        return why
    qty = plan.legs["SP"].lot
    fills = {r: fill_prices(leg_q[r], SHORT[r], qty, plan.legs[r].tick) for r in LEG_ROLES}
    rates = charges.rates_for(ctx.today)
    credit, cost = entry_money(fills, qty, rates, "real")
    credit_2x, cost_2x = entry_money(fills, qty, rates, "x2")
    if not credit > 0:
        return f"NO_CREDIT: the four legs net {credit:.2f} a unit at the bid/ask"
    spreads = {r: {"real": leg_q[r].spread, "model": model_spread(leg_q[r].mid, plan.strikes[r] / spot - 1, td)}
               for r in LEG_ROLES}
    margin = basket_margin(ctx, plan.legs, fills, qty)
    return {
        "ts": ctx.wall().isoformat(), "expiry": expiry.isoformat(), "td": td, "qty": qty,
        "market": {"spot": spot, "vix": vix_q.ltp if vix_q else None, "future": fut,
                   "future_px": fut_q.ltp if fut_q else None, "F": round(plan.F, 2), "sigma": round(plan.sigma, 5),
                   "d": round(plan.d, 5), "atm": plan.atm},
        "strikes": plan.strikes,
        "legs": {r: {"symbol": o.symbol, "token": o.token, "tick": o.tick, "quote": leg_q[r].to_json(),
                     "fill": fills[r], "spread": spreads[r]} for r, o in plan.legs.items()},
        "credit": round(credit, 4), "credit_2x": round(credit_2x, 4), "charges": cost, "charges_2x": cost_2x,
        "max_loss": max_loss(plan.strikes, credit, qty, cost, rates), "margin": margin,
    }


def _describe(e: Mapping[str, Any]) -> str:
    s, q = e["strikes"], e["qty"]
    return (f"sell {s['SP']:.0f} PE / {s['SC']:.0f} CE, buy {s['LP']:.0f} PE / {s['LC']:.0f} CE for "
            f"{e['expiry']}: credit {e['credit']:.2f} x {q} = Rs {e['credit'] * q:,.0f} "
            f"(Rs {e['credit_2x'] * q:,.0f} at 2x spreads), max loss Rs {e['max_loss']:,.0f}")


def enter(ctx: Ctx, con: sqlite3.Connection) -> str:
    """Enter today if today is an expiry's entry day, nothing is open past today and the test is not killed."""
    chains, fut = load_chain(ctx)
    target = next((e for e in sorted(chains) if e > ctx.today and entry_day_for(e, ctx.cal) == ctx.today), None)
    if target is None:
        return "NOT_ENTRY_DAY"
    if _rows(con, "expiry = ?", (target.isoformat(),)):
        return "ALREADY_ENTERED"
    if _rows(con, "state = 'OPEN' AND expiry > ?", (ctx.today.isoformat(),)):
        note(con, ctx.now, "SKIP", f"{target}: a condor is still open", ctx.out)
        return "SKIP_OPEN"
    killed = kill_status(_rows(con))
    if killed:
        note(con, ctx.now, "SKIP", f"{target}: the test is killed ({killed}); no new entries", ctx.out)
        return "SKIP_KILLED"
    e = build_entry(ctx, target, chains[target], trading_days_left(ctx.today, target, ctx.cal), fut)
    if isinstance(e, str):
        note(con, ctx.now, "SKIP", f"{target}: week skipped: {e}", ctx.out)
        return "SKIP"
    try:
        con.execute("INSERT INTO condors(expiry, entry_day, state, qty, credit, credit_2x, max_loss, entry) "
                    "VALUES (?, ?, 'OPEN', ?, ?, ?, ?, ?)", (e["expiry"], ctx.today.isoformat(), e["qty"],
                                                             e["credit"], e["credit_2x"], e["max_loss"],
                                                             json.dumps(e)))
        con.commit()
    except sqlite3.IntegrityError:                       # another writer entered this expiry first
        con.rollback()
        return "ALREADY_ENTERED"
    note(con, ctx.now, "ENTRY", "entered (paper): " + _describe(e), ctx.out)
    return "ENTERED"


def mark(ctx: Ctx, con: sqlite3.Connection, row: sqlite3.Row) -> str:
    """Today's 15:25 mark of an open condor; the 3x stop on any day but expiry."""
    e = json.loads(row["entry"])
    legs = e["legs"]
    qs = fetch_quotes(ctx, [legs[r]["symbol"] for r in LEG_ROLES])
    by_role = {r: qs.get(legs[r]["symbol"]) for r in LEG_ROLES}
    marks = {r: leg_mark(by_role[r], SHORT[r]) for r in LEG_ROLES}
    value = None if None in marks.values() else sum((1 if SHORT[r] else -1) * marks[r] for r in LEG_ROLES)
    ratio = None if value is None else value / row["credit"]
    con.execute("INSERT OR REPLACE INTO marks(condor_id, day, ts, value_mid, ratio, quotes) VALUES (?, ?, ?, ?, ?, ?)",
                (row["id"], ctx.today.isoformat(), ctx.wall().isoformat(), value, ratio,
                 json.dumps({r: (q.to_json() if q else None) for r, q in by_role.items()})))
    con.commit()
    if ctx.today >= date.fromisoformat(row["expiry"]):
        return "MARKED_EXPIRY_DAY"               # no stop on expiry day: held to settlement
    if value is None:
        note(con, ctx.now, "MARK", f"{row['expiry']}: a short leg has no ask (it cannot be bought back); the stop "
                                   "check waits a day", ctx.out)
        return "MARK_INCOMPLETE"
    if value < STOP_MULT * row["credit"]:
        ctx.out(f"condor: {row['expiry']} marked {value:.2f} a unit = {ratio:.2f}x the credit (stop at "
                f"{STOP_MULT:g}x): held")
        return "HELD"
    qty = row["qty"]
    fills = {r: fill_prices(by_role[r], not SHORT[r], qty, legs[r]["tick"]) for r in LEG_ROLES}
    rates = charges.rates_for(ctx.today)
    debit, cost = exit_money(fills, qty, rates, "real")
    debit_2x, cost_2x = exit_money(fills, qty, rates, "x2")
    net = round((row["credit"] - debit) * qty - e["charges"] - cost, 2)
    net_2x = round((row["credit_2x"] - debit_2x) * qty - e["charges_2x"] - cost_2x, 2)
    ex = {"ts": ctx.wall().isoformat(), "value_mid": value, "fills": fills, "debit": debit, "debit_2x": debit_2x,
          "charges": cost, "charges_2x": cost_2x}
    cur = con.execute("UPDATE condors SET state = 'CLOSED', exit_reason = 'STOP_3X', exit_day = ?, exit = ?, "
                      "net = ?, net_2x = ? WHERE id = ? AND state = 'OPEN'",
                      (ctx.today.isoformat(), json.dumps(ex), net, net_2x, row["id"]))
    con.commit()
    if cur.rowcount != 1:
        return "ALREADY_CLOSED"
    abandoned = [legs[r]["symbol"] for r in LEG_ROLES if fills[r].get("abandoned")]
    note(con, ctx.now, "STOP", f"{row['expiry']}: mid value {value:.2f} >= {STOP_MULT:g}x the credit "
                               f"{row['credit']:.2f}: closed (paper), net Rs {net:,.0f} (Rs {net_2x:,.0f} at 2x)"
                               + (f"; no bid, left to expire: {', '.join(abandoned)}" if abandoned else ""), ctx.out)
    return "STOPPED"


def record_close_credit(ctx: Ctx, con: sqlite3.Connection, row: sqlite3.Row) -> None:
    """The entry day's close of each leg and their credit (Kite day bars, the bhavcopy stand-in), fetched while the
    contracts still trade."""
    e = json.loads(row["entry"])
    entry_day = date.fromisoformat(row["entry_day"])
    closes = {r: day_close(ctx, e["legs"][r]["token"], entry_day) for r in LEG_ROLES}
    if None in closes.values():
        return
    cc = sum((1 if SHORT[r] else -1) * closes[r] for r in LEG_ROLES)
    con.execute("UPDATE condors SET close_credit = ?, close_legs = ? WHERE id = ? AND close_credit IS NULL",
                (round(cc, 4), json.dumps(closes), row["id"]))
    con.commit()


def settle_due(ctx: Ctx, con: sqlite3.Connection) -> list[str]:
    """Settle every condor whose expiry has passed (its NIFTY 50 day close known): the held ones for real, the
    stopped ones for their no-stop result."""
    done = []
    for row in _rows(con, "expiry < ? AND (state = 'OPEN' OR net_nostop IS NULL)", (ctx.today.isoformat(),)):
        expiry = date.fromisoformat(row["expiry"])
        px = day_close(ctx, SPOT_TOKEN, expiry)
        if px is None:
            note(con, ctx.now, "SETTLE", f"{row['expiry']}: NIFTY 50's close is not available yet; retried later")
            continue
        e = json.loads(row["entry"])
        qty = row["qty"]
        rates = charges.rates_for(expiry)
        value, cost = settlement(e["strikes"], px, qty, rates)
        held = round((row["credit"] - value) * qty - e["charges"] - cost, 2)
        held_2x = round((row["credit_2x"] - value) * qty - e["charges_2x"] - cost, 2)
        result = json.dumps({"settle_px": px, "value": value, "charges": cost, "held_net": held,
                             "held_net_2x": held_2x})
        if row["state"] == "OPEN":
            cur = con.execute("UPDATE condors SET state = 'CLOSED', exit_reason = 'SETTLED', exit_day = ?, "
                              "settle_px = ?, net = ?, net_2x = ?, net_nostop = ?, result = ? "
                              "WHERE id = ? AND state = 'OPEN'",
                              (expiry.isoformat(), px, held, held_2x, held, result, row["id"]))
            msg = (f"{row['expiry']}: settled at NIFTY {px:,.2f}: owed {value:.2f} a unit against a credit of "
                   f"{row['credit']:.2f}: net Rs {held:,.0f} (Rs {held_2x:,.0f} at 2x spreads)")
        else:
            cur = con.execute("UPDATE condors SET settle_px = ?, net_nostop = ?, result = ? "
                              "WHERE id = ? AND net_nostop IS NULL", (px, held, result, row["id"]))
            msg = f"{row['expiry']}: the stopped condor would have netted Rs {held:,.0f} held to settlement"
        con.commit()
        if cur.rowcount == 1:
            note(con, ctx.now, "SETTLE", msg, ctx.out)
            done.append(row["expiry"])
    return done


def kill_status(rows: Sequence[sqlite3.Row | Mapping[str, Any]]) -> str | None:
    """The first kill rule met by these condors (in expiry order), else None."""
    closed = [r for r in rows if r["state"] == "CLOSED" and r["net"] is not None]
    for r in closed:
        if r["net"] < 0 and -r["net"] > KILL_LOSS_VS_MAX * r["max_loss"]:
            return f"LOSS_BEYOND_MAX: {r['expiry']} lost Rs {-r['net']:,.0f} > {KILL_LOSS_VS_MAX}x Rs {r['max_loss']:,.0f}"
        if not reproduces(r):
            return f"PNL_MISMATCH: {r['expiry']}'s stored net is not what its fills give"
    if len(rows) >= KILL_MIN_ENTRIES:
        ratios = [r["credit"] / r["close_credit"] for r in rows if r["close_credit"]]
        if len(ratios) >= KILL_MIN_ENTRIES and statistics.median(ratios) < KILL_CREDIT_RATIO:
            return f"CREDIT_SHORT: median real credit {statistics.median(ratios):.0%} of the day-close credit"
        spread = [leg["spread"]["real"] / leg["spread"]["model"] for r in rows
                  for leg in json.loads(r["entry"])["legs"].values()
                  if leg["spread"]["real"] is not None and leg["spread"]["model"]]
        if spread and statistics.median(spread) > KILL_SPREAD_RATIO:
            return f"SPREAD_WIDE: median real spread {statistics.median(spread):.1f}x the cost model's"
    for n, most in KILL_WINS:
        if len(closed) >= n:
            wins = sum(1 for r in closed[:n] if r["net"] > 0)
            if wins <= most:
                return f"TOO_FEW_WINS: {wins} wins in the first {n}"
    total_2x = sum(r["net_2x"] for r in closed if r["net_2x"] is not None)
    if total_2x < KILL_NET_2X:
        return f"DRAWDOWN: cumulative net at 2x spreads Rs {total_2x:,.0f}"
    return None


def reproduces(r: sqlite3.Row | Mapping[str, Any]) -> bool:
    """True when the stored credit, charges and net are what the stored fills (and settlement) give."""
    e = json.loads(r["entry"])
    fills = {role: leg["fill"] for role, leg in e["legs"].items()}
    credit, cost = entry_money(fills, r["qty"], charges.rates_for(date.fromisoformat(r["entry_day"])), "real")
    if abs(credit - r["credit"]) > 1e-3 or abs(cost - e["charges"]) > 0.011:
        return False
    if r["exit_reason"] == "STOP_3X":
        debit, xcost = exit_money(json.loads(r["exit"])["fills"], r["qty"],
                                  charges.rates_for(date.fromisoformat(r["exit_day"])), "real")
    elif r["exit_reason"] == "SETTLED":
        debit, xcost = settlement(e["strikes"], r["settle_px"], r["qty"],
                                  charges.rates_for(date.fromisoformat(r["expiry"])))
    else:
        return False
    return abs(round((r["credit"] - debit) * r["qty"] - e["charges"] - xcost, 2) - r["net"]) <= 0.011


def _guard(ctx: Ctx, con: sqlite3.Connection, what: str, fn: Callable[[], Any]) -> Any:
    """One step of a tick; its failure is noted and does not stop the steps after it."""
    try:
        return fn()
    except Exception as exc:
        log.exception("condor %s failed", what)
        note(con, ctx.now, "ERROR", f"{what} failed: {exc}", ctx.out)
        return f"{what}_FAILED"


def tick(ctx: Ctx, *, force: bool = False) -> str:
    """The 15:25 step: the open condor's mark (and stop) first, then settlement of what is due (so the kill rules
    see it), the entry, and the day-close credits. Recorded as done for the day (ticks table) only when neither the
    mark nor the entry failed, so a retry is possible; every step is safe to repeat."""
    con = connect(ctx.var)
    try:
        day = ctx.today.isoformat()
        if not force and con.execute("SELECT 1 FROM ticks WHERE day = ?", (day,)).fetchone():
            return "ALREADY_TICKED"
        if not ctx.cal.is_trading_day(ctx.today):
            return "NOT_A_TRADING_DAY"
        outcome = [_guard(ctx, con, "MARK", lambda r=row: mark(ctx, con, r))
                   for row in _rows(con, "state = 'OPEN' AND expiry >= ?", (day,))]
        _guard(ctx, con, "SETTLE", lambda: settle_due(ctx, con))
        outcome.append(_guard(ctx, con, "ENTRY", lambda: enter(ctx, con)))
        for row in _rows(con, "close_credit IS NULL AND entry_day < ? AND expiry >= ?", (day, day)):
            _guard(ctx, con, "CLOSE_CREDIT", lambda r=row: record_close_credit(ctx, con, r))
        result = ",".join(outcome)
        if "FAILED" not in result:
            con.execute("INSERT OR REPLACE INTO ticks(day, ts, outcome) VALUES (?, ?, ?)",
                        (day, ctx.now.isoformat(), result))
            con.commit()
        return result
    finally:
        con.close()


# ---------------------------------------------------------------------------------------------------------------
# Status, the CLI and the laptop job
# ---------------------------------------------------------------------------------------------------------------
def status_text(var: Path) -> str:
    if not db_path(var).is_file():
        return "condor (paper): no condor yet; the first entry is at 15:25 on the 3rd trading day before an expiry"
    con = connect(var, readonly=True)
    try:
        rows = _rows(con)
        notes = con.execute("SELECT ts, kind, detail FROM notes ORDER BY id DESC LIMIT 3").fetchall()
        marks = {r["condor_id"]: r for r in con.execute("SELECT * FROM marks ORDER BY day")}
    finally:
        con.close()
    lines = ["condor (paper, NIFTY weekly 1.5-SD iron condor, 1 lot):"]
    for r in rows:
        if r["state"] == "OPEN":
            e = json.loads(r["entry"])
            m = marks.get(r["id"])
            last = (f"; last mark {m['value_mid']:.2f} ({m['ratio']:.2f}x) on {m['day']}"
                    if m is not None and m["ratio"] is not None else "")
            lines.append(f"  OPEN   {_describe(e)}{last}")
    closed = [r for r in rows if r["state"] == "CLOSED"]
    if closed:
        net = sum(r["net"] for r in closed)
        net_2x = sum(r["net_2x"] for r in closed)
        wins = sum(1 for r in closed if r["net"] > 0)
        lines.append(f"  closed {len(closed)}: {wins} won, net Rs {net:,.0f} (Rs {net_2x:,.0f} at 2x spreads); "
                     f"last: {closed[-1]['expiry']} {closed[-1]['exit_reason']} Rs {closed[-1]['net']:,.0f}")
    killed = kill_status(rows)
    lines.append(f"  test: {'KILLED: ' + killed if killed else 'running'} ({len(rows)} of 52 weeks entered)")
    lines += [f"  note {n['ts'][:16]} {n['kind']}: {n['detail']}" for n in notes]
    return "\n".join(lines)


def dry_run(ctx: Ctx) -> str:
    """The condor that would be entered for the next expiry if it were its entry time now (nothing saved)."""
    chains, fut = load_chain(ctx)
    target = next((e for e in sorted(chains) if e > ctx.today and (entry_day_for(e, ctx.cal) or e) >= ctx.today),
                  None)
    if target is None:
        return "no NIFTY expiry ahead in the instruments"
    td = trading_days_left(ctx.today, target, ctx.cal)
    e = build_entry(ctx, target, chains[target], td, fut, max_age=None)
    head = f"dry run for the {target} expiry (entry day {entry_day_for(target, ctx.cal)}; {td} trading days left now)"
    if isinstance(e, str):
        return f"{head}: no condor: {e}"
    m = e["market"]
    return (f"{head}: NIFTY {m['spot']:,.2f}, F {m['F']:,.2f}, ATM IV {m['sigma']:.1%}, VIX {m['vix']}: "
            + _describe(e) + (f"; Kite margin Rs {e['margin']:,.0f}" if e["margin"] else ""))


def cli(action: str, var: Path, env: Mapping[str, str], out: Out = print, *, kite: Any = None, cal: Any = None,
        now: datetime | None = None, laptop_runs: Callable[[Path], bool] | None = None) -> int:
    """`titan2 condor [status|dry-run|tick|settle]`: 0 done, 2 refused or failed. tick and settle refuse while a
    laptop session runs: it does them itself."""
    try:
        if action == "status":
            out(status_text(var))
            return 0
        if action not in ("dry-run", "tick", "settle"):
            out(f"unknown condor action {action!r}")
            return 2
        if action in ("tick", "settle"):
            if laptop_runs is None:
                from titan2.laptop import another_runs as laptop_runs
            if laptop_runs(Path(var)):
                out(f"refused: a titan2 session is running; it does the condor's {action} itself")
                return 2
        from titan2.clock import RealClock, SessionCalendar

        ctx = Ctx(Path(var), kite or kite_from_session(env, Path(var)), cal or SessionCalendar.load(),
                  now or RealClock().now_wall(), out)
        if action == "dry-run":
            out(dry_run(ctx))
        elif action == "settle":
            con = connect(ctx.var)
            try:
                out(f"settled: {settle_due(ctx, con) or 'nothing due'}")
            finally:
                con.close()
        else:
            if not (TICK_AT <= ctx.now.time() < TICK_UNTIL):
                out(f"refused: the condor tick runs only between {TICK_AT:%H:%M} and {TICK_UNTIL:%H:%M:%S} "
                    "(dry-run shows the strikes any time)")
                return 2
            out(f"tick: {tick(ctx)}")
    except Exception as exc:
        out(f"condor {action} failed: {exc}")
        return 2
    return 0


class CondorJob:
    """The laptop's condor duty, polled by laptop.Supervisor: settle_due() once after the start (when condor.db
    exists), tick() once a trading day in [TICK_AT, TICK_UNTIL), tried up to TICK_ATTEMPTS times RETRY_AFTER_S
    apart while a step fails. Each runs on its own thread; a failure is logged and noted, never raised into the
    supervisor."""

    def __init__(self, var: Path, env: Mapping[str, str], clock: Any, cal: Any, out: Out,
                 kite_factory: Callable[[], Any] | None = None) -> None:
        self.var, self.env, self.clock, self.cal, self.out = Path(var), dict(env), clock, cal, out
        self.kite_factory = kite_factory or (lambda: kite_from_session(self.env, self.var))
        self._settled = False
        self._day: date | None = None
        self._attempts = 0
        self._last: datetime | None = None
        self._done = False
        self._thread: threading.Thread | None = None

    def poll(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        now = self.clock.now_wall()
        if not self._settled:
            self._settled = True
            if db_path(self.var).is_file():              # nothing to settle before the first condor
                self._start("settle", now)
                return
        if self._day != now.date():
            self._day, self._attempts, self._last, self._done = now.date(), 0, None, False
        if self._done or self._attempts >= TICK_ATTEMPTS or not (TICK_AT <= now.time() < TICK_UNTIL):
            return
        if self._last is not None and (now - self._last).total_seconds() < RETRY_AFTER_S:
            return
        self._attempts += 1
        self._last = now
        self._start("tick", now)

    def _start(self, what: str, now: datetime) -> None:
        self._thread = threading.Thread(target=self._run, args=(what, now), name=f"condor-{what}", daemon=True)
        self._thread.start()

    def _run(self, what: str, now: datetime) -> None:
        try:
            ctx = Ctx(self.var, self.kite_factory(), self.cal, now, self.out)
            if what == "tick":
                outcome = tick(ctx)
                log.info("condor tick (attempt %d): %s", self._attempts, outcome)
                self._done = "FAILED" not in outcome
            else:
                con = connect(self.var)
                try:
                    settle_due(ctx, con)
                finally:
                    con.close()
        except Exception as exc:
            log.exception("condor %s failed", what)
            self.out(f"condor {what} failed ({exc}); `titan2 condor status` shows where it stands")

    def join(self, timeout: float) -> None:
        if self._thread is not None:
            self._thread.join(timeout)
