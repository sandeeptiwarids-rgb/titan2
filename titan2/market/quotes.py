"""QuoteCache: the newest depth snapshot per symbol (spec §3.2, §3.4; DETAIL §2, §5.5, §6.3).

Shared by kite-ws (WS ticks), lane-reads (REST quotes, through its quote_sink), paper-sim (fill-time depth) and the
core (exits, entry decisions). Values are immutable Quote objects replaced under a short lock.

- Newest wins: a snapshot replaces the cached one only if it was received at the same time or later (recv_mono),
  so a slow REST response never overwrites a newer WS tick and an old tick never overwrites a REST refresh.
- fresh(symbol, max_age_s) returns the quote only while its receipt age is at most max_age_s, else None. It never
  returns a stale quote and never calls the broker: the caller requests FetchQuotes and waits for the result.
  KiteTicker sends nothing for an option that does not trade, so a cached quote can freeze for minutes; only the
  receipt age tells (v1's SONACOMS phantom P&L, 2026-05-14).
- last(symbol) returns the newest quote whatever its age. It exists for protective exit pricing from the last
  known bid (§5.5) and for display; never use it where a fresh quote is required.
"""

# Ported from agentic_trader/kite_ticker.py:53-94 @b4694f3 — changes: one immutable Quote per symbol replaces the
#   token-keyed _ltp_cache/_quote_cache/_last_update dicts; receipt time is monotonic, not time.time()
# Ported from agentic_trader/kite_ticker.py:491-589 @b4694f3 — changes: fresh() never returns a stale quote (v1 fell
#   back to the stale cache when REST failed, 547-551) and makes no REST call itself

from __future__ import annotations

import logging
import math
import threading
import time
from typing import Callable

from titan2.model import Quote

log = logging.getLogger(__name__)


def _finite(x: object) -> bool:
    return isinstance(x, (int, float)) and not isinstance(x, bool) and math.isfinite(x)


class QuoteCache:
    """Thread-safe map symbol -> newest Quote. Any thread may call any method."""

    def __init__(self, mono: Callable[[], float] = time.monotonic) -> None:
        self._mono = mono
        self._lock = threading.Lock()
        self._quotes: dict[str, Quote] = {}

    def put(self, q: Quote) -> bool:
        """Store q unless a quote received later is already cached. True when q was stored."""
        if not isinstance(q, Quote):
            raise TypeError(f"QuoteCache.put needs a Quote, got {type(q).__name__}")
        if not isinstance(q.symbol, str) or not q.symbol:
            raise ValueError(f"quote without a symbol: {q!r}")
        if not _finite(q.recv_mono):
            raise ValueError(f"quote {q.symbol}: recv_mono {q.recv_mono!r} is not a finite number")
        with self._lock:
            cur = self._quotes.get(q.symbol)
            if cur is not None and cur.recv_mono > q.recv_mono:
                return False
            self._quotes[q.symbol] = q
        return True

    def fresh(self, symbol: str, max_age_s: float, now_mono: float | None = None) -> Quote | None:
        """The quote if it was received at most max_age_s ago, else None (also None for a bad max_age_s)."""
        if not (_finite(max_age_s) and max_age_s >= 0):
            log.error("QuoteCache.fresh(%s): bad max_age_s %r; treating the quote as stale", symbol, max_age_s)
            return None
        now = self._mono() if now_mono is None else now_mono
        if not _finite(now):
            log.error("QuoteCache.fresh(%s): bad now_mono %r; treating the quote as stale", symbol, now)
            return None
        with self._lock:
            q = self._quotes.get(symbol)
        if q is None or q.age(now) > max_age_s:     # Quote.age(): inf for a receipt > RECEIPT_SKEW_S in the future
            return None
        return q

    def last(self, symbol: str) -> Quote | None:
        """The newest quote whatever its age: protective pricing from the last known bid and display only."""
        with self._lock:
            return self._quotes.get(symbol)

    def age(self, symbol: str, now_mono: float | None = None) -> float | None:
        """Receipt age in seconds of the newest quote, or None when there is none."""
        with self._lock:
            q = self._quotes.get(symbol)
        if q is None:
            return None
        return q.age(self._mono() if now_mono is None else now_mono)

    def symbols(self) -> frozenset[str]:
        with self._lock:
            return frozenset(self._quotes)

    def __len__(self) -> int:
        with self._lock:
            return len(self._quotes)
