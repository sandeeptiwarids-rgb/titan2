"""Instruments: today's dump, the expiry (DTE) rule, strike selection, eligibility and the F&O ban list (spec §11).

Today's dump (kite.instruments("NFO") and ("NSE")) is checked, cached under cache/instruments/<date>/ and indexed into
an immutable InstrumentStore. Only today's dump is ever used, because lots, ticks, strikes and expiries change (NIFTY
lot 65 and BANKNIFTY 30 when this was written, revised twice a year). Every Instrument field, lot size and tick
included, comes from that contract's own row. Open legs never need the dump: their rows carry every field.

Expiries come only from the dump. NSE's schedule (NIFTY weeklies on Tuesday, monthlies and BANKNIFTY on the last
Tuesday, a holiday moving an expiry to the previous trading day, no NIFTY weekly in a monthly-expiry week) is never
re-derived here from weekdays. The DTE rule counts trading sessions with the SessionCalendar.

Fail closed:
- a bad dump raises InstrumentsError, and the caller raises HALT(NO_INSTRUMENTS) + CRITICAL;
- a contract that cannot be selected exactly is Refusal("CONTRACT", ...): nothing is clamped, nothing raises;
- a missing, stale or unparseable ban list makes every stock ineligible today (indices are unaffected).
"""

from __future__ import annotations

import gzip
import json
import logging
import math
import os
import re
import shutil
import time
import zlib
from collections import defaultdict
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path
from types import MappingProxyType
from typing import Any, Callable, Iterable, Mapping, Sequence

from titan2.clock import SessionCalendar
from titan2.model import Instrument, Refusal, StrikePref, Underlying, is_tick_multiple

log = logging.getLogger(__name__)

STOCK = "STOCK"
INDEX = "INDEX"
CONTRACT = "CONTRACT"                  # gate name of G15: every selection Refusal uses it
OPT_TYPES = ("CE", "PE")

# Ported from agentic_trader/config.py:1564-1569 @b4694f3 — changes: only NIFTY and BANKNIFTY; keyed by the NFO name;
#   no lot sizes (lot and tick always come from the contract's own row)
INDEX_UNDERLYINGS: Mapping[str, str] = MappingProxyType({"NIFTY": "NSE:NIFTY 50", "BANKNIFTY": "NSE:NIFTY BANK"})

# Ported from agentic_trader/autonomous_trader.py:9394 @b4694f3 — changes: used only to keep index option names out
#   of the stock universe; of these, only INDEX_UNDERLYINGS are traded
NON_STOCK_OPTION_NAMES = frozenset({"NIFTY", "BANKNIFTY", "FINNIFTY", "MIDCPNIFTY", "NIFTYNXT50", "SENSEX", "BANKEX"})

MIN_TD_FLOOR: Mapping[str, int] = MappingProxyType({INDEX: 2, STOCK: 5})  # settings may raise these, never lower
MIN_INDEX_EXPIRIES = 2                 # sanity: NIFTY and BANKNIFTY each list CE and PE for >= 2 expiries
MIN_STOCK_UNDERLYINGS = 100            # sanity: fewer F&O stocks than this means a broken dump
MIN_TICK = 0.01                        # model tick math rounds prices to 2 dp, so a tick must be a multiple of 0.01


class InstrumentsError(Exception):
    """Today's dump is missing or fails a sanity check. The caller raises HALT(NO_INSTRUMENTS) + CRITICAL."""


# ---------------------------------------------------------------------------------------------------------------
# Row conversion (kite.instruments rows, or the same rows read back from the JSON cache)
# ---------------------------------------------------------------------------------------------------------------
_ISO_DATE = re.compile(r"\d{4}-\d{2}-\d{2}")


def _to_float(value: Any, field: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float, str)):
        raise ValueError(f"{field} {value!r} is not a number")
    try:
        return float(value.strip() if isinstance(value, str) else value)
    except ValueError:
        raise ValueError(f"{field} {value!r} is not a number") from None


def _to_int(value: Any, field: str) -> int:
    x = _to_float(value, field)
    if not math.isfinite(x) or x != int(x):
        raise ValueError(f"{field} {value!r} is not a whole number")
    return int(x)


def _token_or_none(value: Any) -> int | None:
    """A positive instrument token, or None (used for equity rows, where a bad row only drops that name)."""
    try:
        token = _to_int(value, "instrument_token")
    except ValueError:
        return None
    return token if token > 0 else None


def _to_expiry(value: Any) -> date:
    """kiteconnect gives a date; the JSON cache gives 'YYYY-MM-DD'."""
    if isinstance(value, date) and not isinstance(value, datetime):
        return value
    if isinstance(value, str) and _ISO_DATE.fullmatch(value.strip()):
        return date.fromisoformat(value.strip())
    raise ValueError(f"expiry {value!r} is not a date")


def _instrument(row: Mapping[str, Any], u: Underlying) -> Instrument:
    """Convert one NFO-OPT row. Values are only converted here; InstrumentStore validates them."""
    ts = row["tradingsymbol"]
    if not isinstance(ts, str):
        raise ValueError(f"tradingsymbol {ts!r}")
    return Instrument(symbol="NFO:" + ts, token=_to_int(row["instrument_token"], "instrument_token"),
                      tradingsymbol=ts, exchange=row["exchange"], name=u.name, underlying=u.symbol,
                      opt_type=row["instrument_type"], strike=_to_float(row["strike"], "strike"),
                      expiry=_to_expiry(row["expiry"]), lot_size=_to_int(row["lot_size"], "lot_size"),
                      tick=_to_float(row["tick_size"], "tick_size"))


def _finite_pos(x: Any) -> bool:
    return isinstance(x, (int, float)) and not isinstance(x, bool) and math.isfinite(x) and x > 0


def _contract_problem(inst: Instrument) -> str | None:
    """Why a contract is unusable, else None (§11.1: lot, tick and strike > 0; plus what the order path needs)."""
    if (inst.exchange != "NFO" or not isinstance(inst.tradingsymbol, str) or not inst.tradingsymbol
            or inst.symbol != "NFO:" + inst.tradingsymbol):
        return f"bad symbol/exchange {inst.symbol!r}/{inst.exchange!r}"
    if inst.opt_type not in OPT_TYPES:
        return f"opt_type {inst.opt_type!r}"
    if isinstance(inst.token, bool) or not isinstance(inst.token, int) or inst.token <= 0:
        return f"token {inst.token!r}"
    if isinstance(inst.lot_size, bool) or not isinstance(inst.lot_size, int) or inst.lot_size <= 0:
        return f"lot_size {inst.lot_size!r}"
    if not (_finite_pos(inst.tick) and is_tick_multiple(inst.tick, MIN_TICK)):
        return f"tick {inst.tick!r} (must be a positive multiple of {MIN_TICK})"
    if not _finite_pos(inst.strike):
        return f"strike {inst.strike!r}"
    if not isinstance(inst.expiry, date) or isinstance(inst.expiry, datetime):
        return f"expiry {inst.expiry!r}"
    return None


def _row_list(rows: Any, label: str, problems: list[str]) -> list[Mapping[str, Any]]:
    if isinstance(rows, (str, bytes)) or not isinstance(rows, Sequence):
        problems.append(f"{label} dump is not a list of rows")
        return []
    good = [r for r in rows if isinstance(r, Mapping)]
    if len(good) != len(rows):
        problems.append(f"{label} dump has {len(rows) - len(good)} rows that are not objects")
    if not good:
        problems.append(f"{label} dump is empty")
    return good


def _join(problems: list[str], limit: int = 12) -> str:
    more = f"; ... and {len(problems) - limit} more" if len(problems) > limit else ""
    return "; ".join(problems[:limit]) + more


# ---------------------------------------------------------------------------------------------------------------
# Pure rules: expiry and strike
# ---------------------------------------------------------------------------------------------------------------
# Ported from agentic_trader/autonomous_trader.py:9429-9447 @b4694f3 — changes: the expiries of one name from today's
#   dump; distance counted in trading sessions (td) instead of calendar days; the first expiry with td >= MIN_TD wins
def first_eligible_expiry(expiries: Iterable[date], today: date, min_td: int,
                          calendar: SessionCalendar) -> date | None:
    """The first expiry e >= today with td(today, e) >= min_td (§11.4), else None.

    td counts trading sessions in (today, e], so with MIN_TD 2 a Tuesday weekly rolls on the Monday before it, and
    with MIN_TD 5 a stock monthly rolls from E-4. Dates beyond the calendar count as no sessions (it rolls early).
    """
    if isinstance(min_td, bool) or not isinstance(min_td, int) or min_td < 1:
        raise ValueError(f"min_td must be an int >= 1, got {min_td!r}")
    for e in sorted(set(expiries)):
        if e >= today and calendar.td(today, e) >= min_td:
            return e
    return None


# Ported from agentic_trader/options_trader.py:3497-3550 @b4694f3 — changes: ATM and ITM1 only; an ATM tie goes to the
#   more in-the-money strike; a missing ITM1 neighbour, or a spot beyond the listed strikes, is a Refusal instead of
#   v1's silent clamp to the edge strike (options_trader 3529-3531)
def pick_strike(strikes: Iterable[float], spot: float, opt_type: str, pref: StrikePref | str) -> float | Refusal:
    """The listed strike for pref around spot (§11.5).

    ATM = argmin |k - spot|; a tie (within 1e-6) picks the lower strike for CE and the higher for PE.
    ITM1 = the next listed strike below ATM for CE, above ATM for PE. A missing neighbour is a Refusal.
    A spot half a strike step or more beyond the listed range is refused too: the edge strike would not be ATM.
    """
    if opt_type not in OPT_TYPES:
        return Refusal(CONTRACT, f"BAD_REQUEST: opt_type {opt_type!r}")
    try:
        pref = StrikePref(pref)
    except ValueError:
        return Refusal(CONTRACT, f"BAD_REQUEST: strike pref {pref!r}")
    if not _finite_pos(spot):
        return Refusal(CONTRACT, f"SPOT_INVALID: {spot!r}")
    listed = list(strikes)
    if not all(_finite_pos(k) for k in listed):
        return Refusal(CONTRACT, "NO_STRIKES: invalid strike listed")
    ks = sorted(set(listed))
    if len(ks) < 2:
        return Refusal(CONTRACT, f"NO_STRIKES: {len(ks)} listed")
    below, above = round(ks[0] - spot, 6), round(spot - ks[-1], 6)       # same 1e-6 tolerance as the tie-break
    if below >= round((ks[1] - ks[0]) / 2, 6) or above >= round((ks[-1] - ks[-2]) / 2, 6):
        return Refusal(CONTRACT, f"SPOT_OUTSIDE_STRIKES: spot {spot} vs listed {ks[0]}..{ks[-1]}")
    ce = opt_type == "CE"
    atm = min(range(len(ks)), key=lambda i: (round(abs(ks[i] - spot), 6), ks[i] if ce else -ks[i]))
    if pref is StrikePref.ATM:
        return ks[atm]
    nxt = atm - 1 if ce else atm + 1
    if not 0 <= nxt < len(ks):
        return Refusal(CONTRACT, f"NO_ITM1_NEIGHBOUR: ATM {ks[atm]} is the {'lowest' if ce else 'highest'} "
                                 f"listed {opt_type} strike")
    return ks[nxt]


# ---------------------------------------------------------------------------------------------------------------
# The store
# ---------------------------------------------------------------------------------------------------------------
class InstrumentStore:
    """Today's option contracts and underlyings with the §11.2 indexes. Immutable, so any thread may read it.

    Build it with from_rows() (raw kite.instruments rows), from_cache() or prep_instruments(). The constructor runs
    every sanity check itself, so an unchecked store cannot exist.
    """

    def __init__(self, session: date, calendar: SessionCalendar, contracts: Iterable[Instrument],
                 underlyings: Iterable[Underlying], *, min_td_index: int = MIN_TD_FLOOR[INDEX],
                 min_td_stock: int = MIN_TD_FLOOR[STOCK], min_stocks: int = MIN_STOCK_UNDERLYINGS,
                 summary: Mapping[str, Any] | None = None) -> None:
        for kind, value in ((INDEX, min_td_index), (STOCK, min_td_stock)):
            if isinstance(value, bool) or not isinstance(value, int) or value < MIN_TD_FLOOR[kind]:
                raise ValueError(f"MIN_TD_TO_EXPIRY_{kind} must be an int >= {MIN_TD_FLOOR[kind]}, got {value!r}")
        if not isinstance(session, date) or isinstance(session, datetime):
            raise TypeError(f"session must be a date, got {session!r}")
        problems: list[str] = []

        unders: dict[str, Underlying] = {}
        unders_by_token: dict[int, Underlying] = {}
        for u in underlyings:
            if u.kind == INDEX:
                bad = INDEX_UNDERLYINGS.get(u.name) != u.symbol
            else:
                bad = u.kind != STOCK or u.symbol != f"NSE:{u.name}" or u.name in NON_STOCK_OPTION_NAMES
            if bad or isinstance(u.token, bool) or not isinstance(u.token, int) or u.token <= 0:
                problems.append(f"bad underlying {u!r}")
            elif u.symbol in unders or u.token in unders_by_token:
                problems.append(f"duplicate underlying {u.symbol} / token {u.token}")
            else:
                unders[u.symbol] = u
                unders_by_token[u.token] = u
        for symbol in INDEX_UNDERLYINGS.values():
            if symbol not in unders:
                problems.append(f"no NSE token for {symbol}")
        n_stocks = sum(1 for u in unders.values() if u.kind == STOCK)
        if n_stocks < min_stocks:
            problems.append(f"only {n_stocks} stock underlyings (need >= {min_stocks})")

        by_symbol: dict[str, Instrument] = {}
        by_token: dict[int, Instrument] = {}
        by_key: dict[tuple[str, date, str, float], Instrument] = {}
        strikes: dict[tuple[str, date, str], set[float]] = defaultdict(set)
        expiries: dict[str, set[date]] = defaultdict(set)
        for inst in contracts:
            key = (inst.name, inst.expiry, inst.opt_type, inst.strike)
            u = unders.get(inst.underlying)
            why = _contract_problem(inst)
            if why is not None:
                problems.append(f"{inst.symbol}: {why}")
            elif u is None or u.name != inst.name:
                problems.append(f"{inst.symbol}: underlying {inst.underlying!r} is not in the dump")
            elif inst.expiry < session:
                problems.append(f"{inst.symbol}: expired {inst.expiry}")
            elif (inst.symbol in by_symbol or inst.token in by_token or inst.token in unders_by_token
                  or key in by_key):
                problems.append(f"{inst.symbol}: duplicate symbol, token or strike")
            else:
                by_symbol[inst.symbol] = by_token[inst.token] = by_key[key] = inst
                strikes[key[:3]].add(inst.strike)
                expiries[inst.name].add(inst.expiry)
        for name in INDEX_UNDERLYINGS:
            both = [e for e in expiries.get(name, ()) if (name, e, "CE") in strikes and (name, e, "PE") in strikes]
            if len(both) < MIN_INDEX_EXPIRIES:
                problems.append(f"{name} lists CE and PE for {len(both)} expiries (need >= {MIN_INDEX_EXPIRIES})")
        if problems:
            raise InstrumentsError(_join(problems))

        self.session = session
        self.calendar = calendar
        self._min_td = MappingProxyType({INDEX: min_td_index, STOCK: min_td_stock})
        self._unders = unders
        self._unders_by_token = unders_by_token
        self._name_to_symbol = {u.name: u.symbol for u in unders.values()}
        self._by_symbol = by_symbol
        self._by_token = by_token
        self._by_key = by_key
        self._strikes = {k: tuple(sorted(v)) for k, v in strikes.items()}
        self._expiries = {k: tuple(sorted(v)) for k, v in expiries.items()}
        self.summary: Mapping[str, Any] = MappingProxyType({
            **dict(summary or {}), "session": session.isoformat(), "contracts": len(by_symbol),
            "stocks": n_stocks, "indices": len(unders) - n_stocks})

    # -- construction -------------------------------------------------------------------------------------------
    @classmethod
    def from_rows(cls, nfo_rows: Sequence[Mapping[str, Any]], nse_rows: Sequence[Mapping[str, Any]], session: date,
                  calendar: SessionCalendar, **opts: Any) -> InstrumentStore:
        """Parse raw kite.instruments("NFO") / ("NSE") rows (or their JSON cache) and check them; raises
        InstrumentsError. opts: min_td_index, min_td_stock (settings MIN_TD_TO_EXPIRY_*), min_stocks.

        Kept: NIFTY and BANKNIFTY (index tokens from the NSE rows "NIFTY 50" and "NIFTY BANK") and every NFO-OPT
        name with an NSE EQ row (NSE:X <-> X). Other names are skipped and listed in summary; contracts that
        expired before today are dropped (counted in summary). Every kept row must be valid, or the dump fails.
        """
        problems: list[str] = []
        nse = _row_list(nse_rows, "NSE", problems)
        nfo = _row_list(nfo_rows, "NFO", problems)

        index_names = {sym.split(":", 1)[1]: name for name, sym in INDEX_UNDERLYINGS.items()}  # "NIFTY 50" -> NIFTY
        unders: dict[str, Underlying] = {}      # NFO name -> Underlying
        eq_tokens: dict[str, int] = {}
        ambiguous: set[str] = set()
        for row in nse:
            ts = row.get("tradingsymbol")
            if row.get("exchange") != "NSE" or not isinstance(ts, str):
                continue
            if ts in index_names:
                name = index_names[ts]
                try:
                    u = Underlying(f"NSE:{ts}", _to_int(row.get("instrument_token"), "token"), name, INDEX)
                except ValueError as exc:
                    problems.append(f"NSE index row {ts!r}: {exc}")
                    continue
                if unders.setdefault(name, u) != u:
                    problems.append(f"NSE index row {ts!r} is listed twice with different tokens")
            elif row.get("segment") == "NSE" and row.get("instrument_type") == "EQ":
                token = _token_or_none(row.get("instrument_token"))
                if token is not None and eq_tokens.setdefault(ts, token) != token:
                    ambiguous.add(ts)            # two tokens for one symbol: that stock is not traded today
        for ts in ambiguous:
            del eq_tokens[ts]

        options: dict[str, list[Mapping[str, Any]]] = defaultdict(list)
        for row in nfo:
            name = row.get("name")
            if row.get("segment") == "NFO-OPT" and isinstance(name, str):
                options[name].append(row)
        no_eq = sorted(n for n in options if n not in NON_STOCK_OPTION_NAMES and n not in eq_tokens)

        contracts: list[Instrument] = []
        expired = 0
        for name, rows in options.items():
            if name in INDEX_UNDERLYINGS:
                u = unders.get(name)             # from the NSE index row; a missing one is reported by the checks
            elif name in NON_STOCK_OPTION_NAMES or name not in eq_tokens:
                continue                         # an index we do not trade, or a name with no usable NSE EQ row
            else:
                u = Underlying(f"NSE:{name}", eq_tokens[name], name, STOCK)
            if u is None:
                continue
            live = 0
            for row in rows:
                try:
                    inst = _instrument(row, u)
                except (KeyError, TypeError, ValueError) as exc:
                    problems.append(f"{name} row {row.get('tradingsymbol')!r}: {exc}")
                    continue
                if inst.expiry < session:
                    expired += 1                 # never usable; not a reason to fail the dump
                else:
                    contracts.append(inst)
                    live += 1
            if live and u.kind == STOCK:
                unders[name] = u
        if problems:
            raise InstrumentsError(_join(problems))
        summary = {"nfo_rows": len(nfo), "nse_rows": len(nse), "expired_dropped": expired,
                   "names_without_eq_row": len(no_eq), "names_without_eq_row_sample": no_eq[:10]}
        return cls(session, calendar, contracts, unders.values(), summary=summary, **opts)

    @classmethod
    def from_cache(cls, cache_dir: str | Path, session: date, calendar: SessionCalendar,
                   **opts: Any) -> InstrumentStore | None:
        """Today's cached dump, or None when it is absent. A cache that is present but bad raises InstrumentsError."""
        rows = load_dump(cache_dir, session)
        if rows is None:
            return None
        return cls.from_rows(rows[0], rows[1], session, calendar, **opts)

    # -- lookups ------------------------------------------------------------------------------------------------
    def by_symbol(self, symbol: str) -> Instrument | None:
        """'NFO:<tradingsymbol>' -> Instrument."""
        return self._by_symbol.get(symbol)

    def by_token(self, token: int) -> Instrument | None:
        return self._by_token.get(token)

    def underlying(self, symbol_or_name: str) -> Underlying | None:
        """'NSE:RELIANCE' or 'RELIANCE'; 'NSE:NIFTY 50' or 'NIFTY'."""
        u = self._unders.get(symbol_or_name)
        if u is None and symbol_or_name in self._name_to_symbol:
            u = self._unders[self._name_to_symbol[symbol_or_name]]
        return u

    def underlying_by_token(self, token: int) -> Underlying | None:
        return self._unders_by_token.get(token)

    def underlyings(self, kind: str | None = None) -> tuple[Underlying, ...]:
        """Every underlying (or one kind), sorted by symbol: the feed subscribes these in quote mode."""
        return tuple(sorted((u for u in self._unders.values() if kind is None or u.kind == kind),
                            key=lambda u: u.symbol))

    def symbol_for_token(self, token: int) -> str | None:
        """The quote symbol for a feed token: an option ('NFO:...') or an underlying ('NSE:...')."""
        inst = self._by_token.get(token)
        if inst is not None:
            return inst.symbol
        u = self._unders_by_token.get(token)
        return u.symbol if u is not None else None

    def expiries(self, name: str) -> tuple[date, ...]:
        return self._expiries.get(name, ())

    def strikes(self, name: str, expiry: date, opt_type: str) -> tuple[float, ...]:
        return self._strikes.get((name, expiry, opt_type), ())

    def contract(self, name: str, expiry: date, opt_type: str, strike: float) -> Instrument | None:
        return self._by_key.get((name, expiry, opt_type, strike))

    # -- expiry and contract selection ---------------------------------------------------------------------------
    def choose_expiry(self, name: str, kind: str, today: date) -> date | None:
        """The first listed expiry e >= today with td(today, e) >= MIN_TD_TO_EXPIRY_<kind> (§11.4), else None.

        Also None when today is not this dump's session: only today's dump is ever used.
        """
        if kind not in (STOCK, INDEX):
            raise ValueError(f"kind must be {STOCK} or {INDEX}, got {kind!r}")
        if not isinstance(today, date) or isinstance(today, datetime):
            raise TypeError(f"today must be a date, got {today!r}")
        u = self.underlying(name)
        if u is not None and (u.kind != kind or u.name != name):
            raise ValueError(f"{name!r} is the {u.kind} {u.name!r}, not a {kind} name")
        if today != self.session:
            return None
        return first_eligible_expiry(self.expiries(name), today, self._min_td[kind], self.calendar)

    def select_contract(self, underlying: str, opt_type: str, pref: StrikePref | str, spot: float,
                        today: date) -> Instrument | Refusal:
        """The entry contract (§11.5): today's eligible expiry, then the pref strike around spot.

        underlying is the symbol ('NSE:RELIANCE', 'NSE:NIFTY 50'). spot is its LTP, no more than 2 s old (the
        caller checks the age). Whatever cannot be selected exactly is Refusal("CONTRACT", ...); nothing raises.
        """
        u = self._unders.get(underlying)
        if u is None:
            return Refusal(CONTRACT, f"UNKNOWN_UNDERLYING: {underlying!r} is not in today's dump")
        if not isinstance(today, date) or isinstance(today, datetime):
            return Refusal(CONTRACT, f"BAD_REQUEST: today {today!r}")
        if today != self.session:
            return Refusal(CONTRACT, f"DUMP_NOT_TODAY: dump {self.session}, today {today}")
        expiry = first_eligible_expiry(self.expiries(u.name), today, self._min_td[u.kind], self.calendar)
        if expiry is None:
            return Refusal(CONTRACT, f"NO_EXPIRY: no {u.name} expiry with >= {self._min_td[u.kind]} sessions left")
        return self._pick(u.name, expiry, opt_type, pref, spot)

    def opposite(self, inst: Instrument, spot: float, pref: StrikePref | str) -> Instrument | Refusal:
        """Same name and expiry, the other option type, chosen by the same rule (the placebo's contract, §10.6)."""
        if self._by_symbol.get(inst.symbol) != inst:
            return Refusal(CONTRACT, f"UNKNOWN_CONTRACT: {inst.symbol} is not in today's dump")
        other = "PE" if inst.opt_type == "CE" else "CE"
        return self._pick(inst.name, inst.expiry, other, pref, spot)

    def _pick(self, name: str, expiry: date, opt_type: str, pref: StrikePref | str,
              spot: float) -> Instrument | Refusal:
        k = pick_strike(self.strikes(name, expiry, opt_type), spot, opt_type, pref)
        if isinstance(k, Refusal):
            return Refusal(k.gate, f"{k.detail} [{name} {expiry} {opt_type}]")
        inst = self._by_key[(name, expiry, opt_type, k)]
        if _contract_problem(inst) is not None:   # unreachable for a checked store; G15 names it, so it stays
            return Refusal(CONTRACT, f"LOT_TICK_INVALID: {inst.symbol}")
        return inst

    # -- eligibility (G08) ---------------------------------------------------------------------------------------
    def ineligible_reason(self, underlying: str, ban_list: Iterable[str] | None, allow: Iterable[str] = (),
                          deny: Iterable[str] = ()) -> str | None:
        """Why an underlying may not be entered today, else None.

        ban_list holds NSE symbols ('SAIL' or 'NSE:SAIL'); None means today's list is missing or unparseable, and
        then no stock is eligible. allow (when non-empty) and deny are the STOCK_ALLOWLIST / STOCK_DENYLIST
        settings. Indices are unaffected by all three.
        """
        u = self.underlying(underlying)
        if u is None:
            return "NOT_IN_UNIVERSE"
        if u.kind == INDEX:
            return None
        return _stock_ineligible(u.name, None if ban_list is None else _names(ban_list), _names(allow),
                                 _names(deny))

    def universe(self, ban_list: Iterable[str] | None, allow: Iterable[str] = (),
                 deny: Iterable[str] = ()) -> frozenset[str]:
        """Eligible stock underlyings today ('NSE:X' symbols): empty when the ban list is None."""
        banned = None if ban_list is None else _names(ban_list)
        allow_n, deny_n = _names(allow), _names(deny)
        return frozenset(u.symbol for u in self._unders.values()
                         if u.kind == STOCK and _stock_ineligible(u.name, banned, allow_n, deny_n) is None)

    def __repr__(self) -> str:
        return (f"InstrumentStore(session={self.session}, contracts={len(self._by_symbol)}, "
                f"stocks={self.summary['stocks']}, indices={self.summary['indices']})")


def _names(items: Iterable[str]) -> frozenset[str]:
    """Upper-case NSE names; an 'NSE:' prefix is dropped, so 'NSE:SAIL' and 'sail' both read as 'SAIL'."""
    if isinstance(items, str):
        raise TypeError("pass a collection of symbols, not one string")
    out = set()
    for s in items:
        if not isinstance(s, str):
            raise TypeError(f"symbol {s!r} is not a string")
        s = s.strip().upper()
        out.add(s[4:] if s.startswith("NSE:") else s)
    return frozenset(out)


def _stock_ineligible(name: str, banned: frozenset[str] | None, allow: frozenset[str],
                      deny: frozenset[str]) -> str | None:
    if banned is None:
        return "BAN_LIST_MISSING"
    if name in banned:
        return "BANNED"
    if allow and name not in allow:
        return "NOT_ALLOWLISTED"
    if name in deny:
        return "DENYLISTED"
    return None


# ---------------------------------------------------------------------------------------------------------------
# Daily dump cache: <cache_dir>/instruments/YYYY-MM-DD/{NFO,NSE}.json.gz (only today's is ever read)
# ---------------------------------------------------------------------------------------------------------------
def _atomic_write(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    with open(tmp, "wb") as fh:
        fh.write(data)
        fh.flush()
        os.fsync(fh.fileno())
    os.replace(tmp, path)


def _json_default(value: Any) -> str:
    if isinstance(value, date):
        return value.isoformat()
    raise TypeError(f"cannot cache {type(value).__name__} {value!r}")


def dump_dir(cache_dir: str | Path, session: date) -> Path:
    return Path(cache_dir) / "instruments" / session.isoformat()


def save_dump(cache_dir: str | Path, session: date, nfo_rows: Sequence[Mapping[str, Any]],
              nse_rows: Sequence[Mapping[str, Any]]) -> Path:
    """Write today's raw rows atomically (dates as ISO text). Call it only with a dump that passed the checks."""
    d = dump_dir(cache_dir, session)
    for exchange, rows in (("NSE", nse_rows), ("NFO", nfo_rows)):
        raw = json.dumps(list(rows), separators=(",", ":"), default=_json_default).encode("utf-8")
        _atomic_write(d / f"{exchange}.json.gz", gzip.compress(raw, compresslevel=6, mtime=0))
    return d


def load_dump(cache_dir: str | Path, session: date) -> tuple[list[Any], list[Any]] | None:
    """(nfo_rows, nse_rows) cached for session, or None when either file is absent. Unreadable -> InstrumentsError."""
    d = dump_dir(cache_dir, session)
    paths = (d / "NFO.json.gz", d / "NSE.json.gz")
    if not all(p.is_file() for p in paths):
        return None
    out = []
    for p in paths:
        try:
            rows = json.loads(gzip.decompress(p.read_bytes()).decode("utf-8"))
        except (OSError, EOFError, ValueError, zlib.error) as exc:
            raise InstrumentsError(f"cached dump {p} is unreadable: {exc}") from exc
        if not isinstance(rows, list):
            raise InstrumentsError(f"cached dump {p} is not a list")
        out.append(rows)
    return out[0], out[1]


def prep_instruments(data: Any, cache_dir: str | Path, session: date, calendar: SessionCalendar,
                     **opts: Any) -> InstrumentStore:
    """Prep (§11.1, on the reads lane): today's cached dump if present and sane, else data.instruments("NFO") and
    ("NSE"), checked, then cached. A previous day's dump is never used.

    Raises InstrumentsError for a bad dump or a failed cache write (-> HALT(NO_INSTRUMENTS) + CRITICAL). Broker
    exceptions from data.instruments() propagate unchanged, so the lane classifies them like any other read.
    """
    try:
        store = InstrumentStore.from_cache(cache_dir, session, calendar, **opts)
    except InstrumentsError as exc:
        log.warning("today's cached instruments dump is unusable; fetching again: %s", exc)
        store = None
    if store is not None:
        return store
    nfo = data.instruments("NFO")
    nse = data.instruments("NSE")
    store = InstrumentStore.from_rows(nfo, nse, session, calendar, **opts)
    try:
        save_dump(cache_dir, session, nfo, nse)
    except OSError as exc:
        raise InstrumentsError(f"cannot cache today's dump in {dump_dir(cache_dir, session)}: {exc}") from exc
    return store


def prune_cache(cache_dir: str | Path, today: date, keep_days: int = 10) -> int:
    """Delete cached dumps and ban lists dated more than keep_days before today. Returns how many were removed."""
    cutoff = today - timedelta(days=keep_days)
    removed = 0
    for sub, suffix in (("instruments", ""), ("ban_list", ".csv")):
        base = Path(cache_dir) / sub
        if not base.is_dir():
            continue
        for p in base.iterdir():
            stem = p.name[:-len(suffix)] if suffix and p.name.endswith(suffix) else p.name
            if not _ISO_DATE.fullmatch(stem):
                continue                         # not a dated cache entry: leave it alone
            if stem < cutoff.isoformat():        # ISO dates order as text
                if p.is_dir():
                    shutil.rmtree(p)
                else:
                    p.unlink()
                removed += 1
    return removed


# ---------------------------------------------------------------------------------------------------------------
# NSE F&O ban list (§11.6)
# ---------------------------------------------------------------------------------------------------------------
DEFAULT_BAN_LIST_URL = "https://nsearchives.nseindia.com/content/fo/fo_secban.csv"   # [V] §17.2 item 8
BAN_FETCH_TRIES = 3
BROWSER_HEADERS: Mapping[str, str] = MappingProxyType({   # nsearchives refuses clients without browser headers
    "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
                   "Chrome/129.0.0.0 Safari/537.36"),
    "Accept": "text/csv,text/plain,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
    "Referer": "https://www.nseindia.com/",
})
OVERRIDE = "OVERRIDE"
CACHE = "CACHE"
NSE = "NSE"
MISSING = "MISSING"

# fo_secban.csv, format [V] (§17.2 item 8). Checked 2026-10-11 against a public quote of the file,
# "Securities in Ban For Trade Date 03-SEP-2026: 1,LICHSGFIN 2,SAIL"
# (https://github.com/kanchankapila/bharat-stock-intelligence/pull/110; NSE page:
# https://www.nseindia.com/static/products-services/equity-derivatives-risk-management-sec-ban):
#     Securities in Ban For Trade Date 03-SEP-2026:
#     1,LICHSGFIN
#     2,SAIL
# A no-ban day is accepted as the header with no rows, or with NIL. Anything else fails the parse (fails closed).
_BAN_HEADER = re.compile(r"securities\s+in\s+ban\s+(?:period\s+)?for\s+trade\s+date\s*:?\s*"
                         r"(\d{1,2}[-/][A-Za-z]{3,9}[-/]\d{4}|\d{1,2}[-/]\d{1,2}[-/]\d{4}|\d{4}-\d{2}-\d{2})"
                         r"\s*:?\s*(.*)", re.IGNORECASE)
_BOM = chr(0xFEFF)                     # a UTF-8 byte-order mark read as text
_BAN_SYMBOL = re.compile(r"[A-Z0-9][A-Z0-9&._-]{0,29}")
_MONTHS = {m: i for i, m in enumerate(("JAN", "FEB", "MAR", "APR", "MAY", "JUN", "JUL", "AUG", "SEP", "OCT", "NOV",
                                       "DEC"), start=1)}


class BanListError(ValueError):
    """The ban list could not be fetched or parsed. load_ban_list turns it into BanList(symbols=None)."""


@dataclass(frozen=True, slots=True)
class BanList:
    session: date
    symbols: frozenset[str] | None     # NSE symbols ('SAIL'); None = missing or unparseable: no stock is eligible
    source: str                        # OVERRIDE | CACHE | NSE | MISSING
    detail: str = ""

    @property
    def ok(self) -> bool:
        return self.symbols is not None


def _ban_date(token: str) -> date:
    """'03-SEP-2026' (NSE's form), also '03-09-2026', '03/09/2026' and '2026-09-03'. Locale-independent."""
    m = re.fullmatch(r"(\d{1,2})[-/]([A-Za-z]{3})[A-Za-z]*[-/](\d{4})", token)
    if m and m.group(2).upper() in _MONTHS:
        return date(int(m.group(3)), _MONTHS[m.group(2).upper()], int(m.group(1)))
    m = re.fullmatch(r"(\d{1,2})[-/](\d{1,2})[-/](\d{4})", token)
    if m:
        return date(int(m.group(3)), int(m.group(2)), int(m.group(1)))
    m = re.fullmatch(r"(\d{4})-(\d{2})-(\d{2})", token)
    if m:
        return date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
    raise BanListError(f"BAD_DATE: {token!r}")


def parse_ban_list(text: str, today: date, *, require_header: bool = True) -> frozenset[str]:
    """The banned NSE symbols for today. Raises BanListError when the text is not a list for today.

    require_header=False is for the operator's override file, whose name carries the date: it may be a bare list
    (one 'SYMBOL' or 'n,SYMBOL' per line, '#' comments allowed). A bare list with no bans must say NIL; an empty
    file is not read as "nothing banned". If an override does carry the NSE header, its date must be today too.
    """
    if not isinstance(text, str):
        raise BanListError(f"ban list is {type(text).__name__}, not text")
    lines = [ln.strip() for ln in text.lstrip(_BOM).splitlines()]
    lines = [ln for ln in lines if ln and not ln.startswith("#")]
    rows = lines
    header_date = None
    if lines:
        m = _BAN_HEADER.fullmatch(lines[0])
        if m:
            header_date = _ban_date(m.group(1))
            rest = m.group(2).strip()
            rows = ([rest] if rest else []) + lines[1:]
    if header_date is None and require_header:
        raise BanListError("NO_HEADER: not an NSE 'Securities in Ban For Trade Date' file")
    if header_date is not None and header_date != today:
        raise BanListError(f"DATE_MISMATCH: list is for {header_date}, today is {today}")
    symbols: set[str] = set()
    nil = False
    for row in rows:
        cells = [c.strip() for c in row.split(",")]
        while cells and not cells[-1]:
            cells.pop()                          # tolerate trailing commas
        if len(cells) == 1 and cells[0].upper() == "NIL":
            nil = True
            continue
        if len(cells) == 2 and cells[0].isdigit():
            sym = cells[1].upper()
        elif len(cells) == 1:
            sym = cells[0].upper()
        else:
            raise BanListError(f"UNPARSEABLE: row {row!r}")
        if not _BAN_SYMBOL.fullmatch(sym):
            raise BanListError(f"UNPARSEABLE: symbol {sym!r}")
        symbols.add(sym)
    if nil and symbols:
        raise BanListError("UNPARSEABLE: NIL together with symbols")
    if header_date is None and not symbols and not nil:
        raise BanListError("EMPTY: a list with no bans must say NIL")
    return frozenset(symbols)


def ban_list_url(env: Mapping[str, str] | None = None) -> str:
    """BAN_LIST_URL from the environment (optional, /etc/titan2/titan2.env), else the NSE default."""
    return (os.environ if env is None else env).get("BAN_LIST_URL") or DEFAULT_BAN_LIST_URL


def fetch_ban_list_text(url: str, *, get: Callable[..., Any] | None = None, tries: int = BAN_FETCH_TRIES,
                        timeout_s: float = 10.0, backoff_s: float = 2.0,
                        sleep: Callable[[float], None] = time.sleep) -> str:
    """GET the ban-list file with browser headers, up to `tries` times (§11.6). Runs on the reads lane.

    Returns the body of the first HTTP 200 with content; raises BanListError with the last failure otherwise.
    """
    if get is None:
        import requests                          # imported lazily: only the reads lane fetches
        get = requests.get
    last = "not attempted"
    for attempt in range(1, tries + 1):
        try:
            resp = get(url, headers=dict(BROWSER_HEADERS), timeout=timeout_s)
            body = resp.content.decode("utf-8-sig", errors="replace")
            if resp.status_code == 200 and body.strip():
                return body
            last = f"HTTP {resp.status_code}, {len(body)} chars"
        except Exception as exc:                 # network errors are retried, then reported with the last one
            last = f"{type(exc).__name__}: {exc}"
        log.warning("ban list fetch %d/%d from %s failed: %s", attempt, tries, url, last)
        if attempt < tries:
            sleep(backoff_s * attempt)
    raise BanListError(f"FETCH_FAILED after {tries} tries: {last}")


def nse_fetcher(url: str | None = None, **kwargs: Any) -> Callable[[], str]:
    """The production fetch for load_ban_list: BAN_LIST_URL (or the NSE default) via fetch_ban_list_text."""
    target = url or ban_list_url()
    return lambda: fetch_ban_list_text(target, **kwargs)


def ban_override_path(config_dir: str | Path, today: date) -> Path:
    return Path(config_dir) / "ban_override" / f"{today.isoformat()}.csv"


def ban_cache_path(cache_dir: str | Path, today: date) -> Path:
    return Path(cache_dir) / "ban_list" / f"{today.isoformat()}.csv"


def load_ban_list(today: date, *, cache_dir: str | Path, config_dir: str | Path,
                  fetch: Callable[[], str] | None) -> BanList:
    """Today's ban list (§11.6). Never raises for missing or bad data: symbols=None then, and the caller WARNs.

    Order: the operator override config/ban_override/<today>.csv (it wins, and a bad one is not bypassed); then
    today's cache; then fetch() (None = no network fetch). Only a list dated today is cached.
    Paper and live use the same list.
    """
    override = ban_override_path(config_dir, today)
    if override.exists():
        try:
            symbols = parse_ban_list(override.read_text(encoding="utf-8-sig"), today, require_header=False)
        except (OSError, ValueError) as exc:     # BanListError and UnicodeDecodeError are ValueErrors
            return BanList(today, None, OVERRIDE, f"{override} is unusable: {exc}")
        return BanList(today, symbols, OVERRIDE, f"{len(symbols)} banned (manual override)")

    cached = ban_cache_path(cache_dir, today)
    if cached.exists():
        try:
            symbols = parse_ban_list(cached.read_text(encoding="utf-8"), today)
            return BanList(today, symbols, CACHE, f"{len(symbols)} banned (cached)")
        except (OSError, ValueError) as exc:
            log.warning("ignoring the unusable cached ban list %s: %s", cached, exc)

    if fetch is None:
        return BanList(today, None, MISSING, "no override, no cache, and no fetch configured")
    try:
        text = fetch()
    except Exception as exc:                     # reported in the result; the caller WARNs and stocks are ineligible
        return BanList(today, None, MISSING, f"fetch failed: {exc}")
    try:
        symbols = parse_ban_list(text, today)
    except BanListError as exc:
        return BanList(today, None, NSE, f"unusable: {exc}")
    try:
        _atomic_write(cached, text.encode("utf-8"))
    except OSError as exc:                       # today's list is still valid; only the restart shortcut is lost
        log.warning("could not cache today's ban list at %s: %s", cached, exc)
    return BanList(today, symbols, NSE, f"{len(symbols)} banned")
