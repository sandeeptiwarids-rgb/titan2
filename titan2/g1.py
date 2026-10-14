"""G1, the gate between PAPER and LIVE, and the live review (spec §10.7-10.9; DETAIL §10.7-10.9).

A strategy version earns a LIVE unlock only on its own PAPER:<s> trades, entered after its trial registered:
- evidence: n >= 100 trades over D >= 30 sessions;
- an edge after costs that survives 1.5x the measured crossing cost: mean r > 0, and the day-clustered bootstrap
  lower bound LB > 0 at alpha_look = 0.05 / (m * 4). m >= 4 counts every trial ever registered and never
  decreases, so every version and every pre-registered look pays for the others (Bonferroni);
- a placebo (the opposite option type, same snapshot) with no edge at 1.0x: n >= 50, coverage >= 0.8, mean u <= 0;
- clean books in the window: zero PAPER mismatches, zero UNRESOLVED/ESTIMATED trades, at most 10% IMPAIRED
  sessions and, for opening_drive, at most 10% of the sample's signal bars mismatched by the bar check (the 20
  control symbols checked each day are reported apart, never pooled in);
- then the next 25 trades with mean r > 0, no mismatch and the same IMPAIRED and signal-bar limits over the confirm
  window (CONFIRM). That certificate's inputs_hash is the hash a human must type for key 2 (§10.8).
FUTILITY retires a hopeless version early. live_review() demotes LIVE strategies on probation, drawdown, the
rolling-50 loss, the X2 paper-model falsifier and any LIVE reconciliation problem.

Per trade: N = net re-priced from the effective fills with the CURRENT RateSet (charges.net_pnl, square-off fees
included), P = risk_basis_rs, C = max(hs_cost, crossing_cost, 0); r = (N - 0.5 C) / P, u = N / P.
Everything is computed from rows; nothing is cached. Bootstraps are seeded from their inputs, so every report can
be reproduced. Writers (reports, trial states, modes, events) run on the core thread inside the caller's
transaction; they return Alert commands for the engine to dispatch after the commit.
"""

from __future__ import annotations

import hashlib
import itertools
import json
import math
import random
import statistics
from collections import defaultdict
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta
from typing import TYPE_CHECKING, Any, Iterable, Mapping, Sequence

from titan2.charges import RateSet, net_pnl, rates_for
from titan2.clock import parse_wall, session_of
from titan2.core.events import Alert
from titan2.ledger import execution_stats, order_values_by_structure, query_in
from titan2.model import PnlStatus
from titan2.settings import (
    DEFAULTS, G1_ALPHA, G1_BOOT_MIN, G1_CONFIRM_TRADES, G1_FAMILY_FLOOR, G1_FUTILITY_ALPHA, G1_FUTILITY_EVERY,
    G1_FUTILITY_MIN_N, G1_LIVE_ROLLING_N, G1_LOOKS, G1_MAX_BAR_MISMATCH_FRAC, G1_MAX_IMPAIRED_FRAC, G1_MIN_SESSIONS,
    G1_MIN_TRADES, G1_PLACEBO_MIN_COVERAGE, G1_PLACEBO_MIN_N, G1_REPORT_MAX_AGE_DAYS, G1_SPREAD_STRESS,
    G1_X2_MAX_FILL_RATE_GAP_PP, G1_X2_MAX_SLIP_GAP_PP, G1_X2_MIN_PAIRS, Settings, canonical_json,
)

if TYPE_CHECKING:
    from titan2.store import Store

__all__ = [
    "Assessment", "Demotion", "G1Run", "LiveReview", "Sample", "UnlockCheck", "X2Result",
    "alpha_look", "assess_confirm", "assess_futility", "assess_look", "boot_rank", "boot_seed", "boot_size",
    "bootstrap_bounds", "count_mismatches", "demote", "demote_all_live", "ensure_trial", "family_m", "inputs_hash",
    "live_review", "next_look", "run_nightly", "scorecards", "trial_summary", "verify_unlock", "x2_falsifier",
]

STRESS_EXTRA = G1_SPREAD_STRESS - 1.0     # N already paid the crossing cost once; r charges it 0.5x more
ACTIVE_STATES = ("COLLECTING", "CONFIRMING", "CERTIFIED")
BAR_CHECK_STRATEGIES = frozenset({"opening_drive"})   # §10.3: its signal bar is checked against Kite's history
# Events that are a G1 mismatch on account PAPER (§9.3: any recon class but CLEAN, PENDING, BROKER_SQUAREOFF;
# §9.8: the ledger must equal the PaperExchange recompute; §10.7's list). Payload conventions: core/recon.py.
MISMATCH_KINDS = ("MISMATCH", "UNKNOWN_POSITION", "SHORT_DETECTED", "FILLS_REPAIRED", "EOD_TRUTH_MISMATCH")
_CONDITIONAL_KINDS = ("EOD_TRUTH_CHECK", "VERIFY_FLAT", "RECON", "CLOSED_EXTERNAL")
LIVE_PROBLEM_KINDS = ("MISMATCH", "UNKNOWN_POSITION", "SHORT_DETECTED", "VERIFY_FLAT")   # §10.9 reconciliation
PAISE = 0.005


def paper_book(s: str) -> str:
    return f"PAPER:{s}"


def placebo_book(s: str) -> str:
    return f"PLACEBO:{s}"


def live_book(s: str) -> str:
    return f"LIVE:{s}"


def shadow_book(s: str) -> str:
    return f"SHADOW:{s}"


def _mean(xs: Sequence[float]) -> float | None:
    return math.fsum(xs) / len(xs) if xs else None


def _pct(x: float | None) -> float | None:
    return None if x is None else round(x * 100.0, 4) + 0.0


# ---------------------------------------------------------------------------------------------------------------
# Multiple testing and the bootstrap
# ---------------------------------------------------------------------------------------------------------------
def family_m(store: Store) -> int:
    """m = max(#trials ever registered, G1_FAMILY_FLOOR). Trials are never deleted, so m never decreases."""
    return max(int(store.trial_count()), G1_FAMILY_FLOOR)


def alpha_look(m: int) -> float:
    """Bonferroni over all trials and all pre-registered looks: 0.05 / (m * 4); 0.3125% at m = 4."""
    return G1_ALPHA / (m * len(G1_LOOKS))


def boot_size(alpha: float) -> int:
    """B = max(10,000, ceil(50 / alpha)): at least 50 replicates lie beyond the bound."""
    return max(G1_BOOT_MIN, math.ceil(50.0 / alpha - 1e-9))


def boot_rank(alpha: float, b: int) -> int:
    """k = floor(alpha * B): LB is the k-th smallest replicate and UB the k-th largest."""
    return max(1, math.floor(alpha * b + 1e-9))


def boot_seed(strategy: str, version: str, max_structure_id: int, kind: str) -> int:
    return int(hashlib.sha256(f"{strategy}|{version}|{max_structure_id}|{kind}".encode("utf-8")).hexdigest()[:16],
               16)


def bootstrap_bounds(by_session: Mapping[str, Sequence[float]], alpha: float, seed: int) -> tuple[float, float] | None:
    """(LB, UB) of the day-clustered bootstrap of the pooled mean (§10.7).

    Each of B replicates draws D sessions with replacement (index = int(random() * D) on random.Random(seed),
    sessions in sorted order) and takes sum(values) / count over the drawn sessions' trades. None without data.
    """
    keys = sorted(k for k, xs in by_session.items() if len(xs))
    if not keys:
        return None
    sums = [math.fsum(by_session[k]) for k in keys]
    counts = [len(by_session[k]) for k in keys]
    d = len(keys)
    b = boot_size(alpha)
    rnd = random.Random(seed).random
    stats = []
    for _ in range(b):
        s = 0.0
        c = 0
        for _ in range(d):
            i = int(rnd() * d)
            s += sums[i]
            c += counts[i]
        stats.append(s / c)
    stats.sort()
    k = boot_rank(alpha, b)
    return stats[k - 1], stats[b - k]


# ---------------------------------------------------------------------------------------------------------------
# Samples and assessments (pure)
# ---------------------------------------------------------------------------------------------------------------
@dataclass(frozen=True, slots=True)
class Sample:
    """One G1 trade (or placebo trade)."""

    structure_id: int
    session: str                     # 'YYYY-MM-DD'
    net: float                       # N: re-priced from the effective fills with the current RateSet
    risk: float                      # P: risk_basis_rs (> 0 by the trades CHECK)
    cost: float                      # C = max(hs_cost, crossing_cost, 0)
    pnl_status: str = "FINAL"
    complete: bool = True            # every order's effective fills explain its filled_qty
    parent_id: int | None = None     # placebos: the PAPER parent's structure_id
    underlying: str | None = None    # its signal bar is bar_checks (session, symbol = underlying)

    @property
    def r(self) -> float:
        return (self.net - STRESS_EXTRA * self.cost) / self.risk

    @property
    def u(self) -> float:
        return self.net / self.risk

    @property
    def resolved(self) -> bool:
        return self.pnl_status == PnlStatus.FINAL.value and self.complete


def inputs_hash(strategy: str, version: str, rates_version: str, trades: Iterable[Sample],
                placebos: Iterable[Sample]) -> str:
    """sha256 of canonical_json({strategy, version, rates_version, sorted (id, N, C), sorted placebo (id, N)})."""
    doc = {"strategy": strategy, "version": version, "rates_version": rates_version,
           "trades": sorted([x.structure_id, round(x.net, 2) + 0.0, round(x.cost, 2) + 0.0] for x in trades),
           "placebos": sorted([p.structure_id, round(p.net, 2) + 0.0] for p in placebos)}
    return hashlib.sha256(canonical_json(doc).encode("utf-8")).hexdigest()


@dataclass(frozen=True, slots=True)
class Assessment:
    """One G1 report before it is written (one g1_reports row)."""

    kind: str                        # PROGRESS | LOOK | CONFIRM | FUTILITY | LIVE_REVIEW
    look_n: int | None
    n: int
    sessions: int
    mean_r: float | None
    mean_u: float | None
    lb: float | None
    ub: float | None
    alpha: float | None
    family_m: int
    placebo_n: int | None
    placebo_coverage: float | None
    placebo_mean_u: float | None
    mismatches: int | None
    unresolved: int
    impaired_frac: float | None
    bar_mismatch_frac: float | None
    passed: bool
    fail_reasons: tuple[str, ...]
    inputs_hash: str
    detail: Mapping[str, Any]

    def row(self, strategy: str, version: str, computed_wall: datetime) -> dict[str, Any]:
        return {"strategy": strategy, "version": version, "kind": self.kind, "look_n": self.look_n,
                "computed_ts": computed_wall, "n": self.n, "sessions": self.sessions, "exp_pct": _pct(self.mean_u),
                "exp_pct_stressed": _pct(self.mean_r), "lb_pct": _pct(self.lb), "ub_pct": _pct(self.ub),
                "alpha": self.alpha, "family_m": self.family_m, "placebo_n": self.placebo_n,
                "placebo_coverage": self.placebo_coverage, "placebo_exp_pct": _pct(self.placebo_mean_u),
                "mismatches": self.mismatches, "unresolved": self.unresolved, "impaired_frac": self.impaired_frac,
                "bar_mismatch_frac": self.bar_mismatch_frac, "passed": self.passed,
                "fail_reasons": ",".join(self.fail_reasons) or None, "inputs_hash": self.inputs_hash,
                "detail": dict(self.detail)}


def _boot(strategy: str, version: str, sample: Sequence[Sample], attr: str, alpha: float,
          kind: str) -> tuple[float | None, float | None, dict[str, Any]]:
    if not sample:
        return None, None, {}
    by: dict[str, list[float]] = defaultdict(list)
    for x in sample:
        by[x.session].append(getattr(x, attr))
    seed = boot_seed(strategy, version, max(x.structure_id for x in sample), kind)
    lb, ub = bootstrap_bounds(by, alpha, seed)
    b = boot_size(alpha)
    return lb, ub, {"boot_b": b, "boot_rank": boot_rank(alpha, b), "boot_seed": str(seed), "boot_of": attr}


def _span(sample: Sequence[Sample]) -> dict[str, Any]:
    if not sample:
        return {"first_structure_id": None, "last_structure_id": None, "first_session": None, "last_session": None}
    return {"first_structure_id": min(x.structure_id for x in sample),
            "last_structure_id": max(x.structure_id for x in sample),
            "first_session": min(x.session for x in sample), "last_session": max(x.session for x in sample)}


def assess_look(strategy: str, version: str, sample: Sequence[Sample], placebos: Mapping[int, Sample], *,
                family_m: int, rates_version: str, mismatches: int = 0, impaired_sessions: int = 0,
                bar_checks: tuple[int, int] | None = None, needs_bar: bool = False, kind: str = "LOOK",
                look_n: int | None = None, extra: Mapping[str, Any] | None = None) -> Assessment:
    """A LOOK (or the nightly PROGRESS, which applies the same criteria and never passes).

    placebos: parent structure_id -> its placebo trade. bar_checks: (checked, mismatched) in the window.
    """
    if kind not in ("LOOK", "PROGRESS"):
        raise ValueError(f"assess_look cannot make a {kind} report")
    n = len(sample)
    sessions = len({x.session for x in sample})
    alpha = alpha_look(family_m)
    mean_r, mean_u = _mean([x.r for x in sample]), _mean([x.u for x in sample])
    lb, ub, boot = _boot(strategy, version, sample, "r", alpha, kind)
    pl = [placebos[x.structure_id] for x in sample if x.structure_id in placebos]
    coverage = len(pl) / n if n else None
    pl_u = _mean([p.u for p in pl])
    unresolved = sum(1 for x in sample if not x.resolved)
    impaired_frac = impaired_sessions / sessions if sessions else None
    bar_frac = bar_checks[1] / bar_checks[0] if bar_checks and bar_checks[0] else None
    reasons = []
    if n < G1_MIN_TRADES:
        reasons.append("MIN_TRADES")
    if sessions < G1_MIN_SESSIONS:
        reasons.append("MIN_SESSIONS")
    if mean_r is None or mean_r <= 0:
        reasons.append("MEAN_R")
    if lb is None or lb <= 0:
        reasons.append("LB")
    if len(pl) < G1_PLACEBO_MIN_N:
        reasons.append("PLACEBO_N")
    if coverage is None or coverage < G1_PLACEBO_MIN_COVERAGE:
        reasons.append("PLACEBO_COVERAGE")
    if pl_u is not None and pl_u > 0:            # judged at 1.0x spreads (u, not r)
        reasons.append("PLACEBO_EDGE")
    if mismatches:
        reasons.append("MISMATCH")
    if unresolved:
        reasons.append("UNRESOLVED")
    if impaired_frac is not None and impaired_frac > G1_MAX_IMPAIRED_FRAC:
        reasons.append("IMPAIRED")
    if needs_bar and bar_frac is None:
        reasons.append("BAR_CHECKS_MISSING")
    elif needs_bar and bar_frac > G1_MAX_BAR_MISMATCH_FRAC:
        reasons.append("BAR_MISMATCH")
    detail: dict[str, Any] = {"alpha_look": alpha, "spread_stress": G1_SPREAD_STRESS, **boot, **_span(sample),
                              "impaired_sessions": impaired_sessions,
                              "bar_checks": list(bar_checks) if bar_checks is not None else None,
                              "placebo_unresolved": sum(1 for p in pl if not p.resolved)}
    if kind == "PROGRESS":
        _, fub, _ = _boot(strategy, version, sample, "u", G1_FUTILITY_ALPHA, kind)
        detail["futility_ub_pct"] = _pct(fub)
    detail.update(extra or {})
    return Assessment(kind, look_n, n, sessions, mean_r, mean_u, lb, ub, alpha, family_m, len(pl), coverage, pl_u,
                      mismatches, unresolved, impaired_frac, bar_frac, kind == "LOOK" and not reasons,
                      tuple(reasons), inputs_hash(strategy, version, rates_version, sample, pl), detail)


def assess_confirm(strategy: str, version: str, confirm: Sequence[Sample], evidence: Sequence[Sample],
                   placebos: Mapping[int, Sample], *, family_m: int, rates_version: str, mismatches: int = 0,
                   impaired_sessions: int = 0, bar_checks: tuple[int, int] | None = None, needs_bar: bool = False,
                   look_n: int | None = None, extra: Mapping[str, Any] | None = None) -> Assessment:
    """CONFIRM: the G1_CONFIRM_TRADES trades after the passing look's sample need mean r > 0 and no mismatch (and,
    like every G1 sample, no UNRESOLVED/ESTIMATED trade). inputs_hash covers the whole evidence (look sample plus
    confirm sample, with their placebos): it is the certificate the LIVE unlock is bound to.

    Spec clarification (§10.7 lists only mean r and mismatches): the confirm window also passes the look's
    operator-selection control (IMPAIRED sessions / D <= G1_MAX_IMPAIRED_FRAC) and, for opening_drive, the
    signal-bar criterion over its own trades, since this is the step that issues the certificate. Without them an
    operator who PAUSEs on bad-looking days (PAPER intents divert to SHADOW, the session is IMPAIRED) would certify
    the strategy on a censored confirm sample. bar_checks: (checked, mismatched) of the confirm trades' signal bars."""
    n = len(confirm)
    sessions = len({x.session for x in confirm})
    mean_r, mean_u = _mean([x.r for x in confirm]), _mean([x.u for x in confirm])
    unresolved = sum(1 for x in confirm if not x.resolved)
    pl = [placebos[x.structure_id] for x in confirm if x.structure_id in placebos]
    impaired_frac = impaired_sessions / sessions if sessions else None
    bar_frac = bar_checks[1] / bar_checks[0] if bar_checks and bar_checks[0] else None
    reasons = []
    if n < G1_CONFIRM_TRADES:
        reasons.append("CONFIRM_N")
    if mean_r is None or mean_r <= 0:
        reasons.append("MEAN_R")
    if mismatches:
        reasons.append("MISMATCH")
    if unresolved:
        reasons.append("UNRESOLVED")
    if impaired_frac is not None and impaired_frac > G1_MAX_IMPAIRED_FRAC:
        reasons.append("IMPAIRED")
    if needs_bar and bar_frac is None:
        reasons.append("BAR_CHECKS_MISSING")
    elif needs_bar and bar_frac > G1_MAX_BAR_MISMATCH_FRAC:
        reasons.append("BAR_MISMATCH")
    ev_pl = [placebos[x.structure_id] for x in evidence if x.structure_id in placebos]
    detail = {**_span(confirm), "look_n": look_n, "evidence_n": len(evidence),
              "evidence_last_structure_id": max((x.structure_id for x in evidence), default=None),
              "impaired_sessions": impaired_sessions,
              "bar_checks": list(bar_checks) if bar_checks is not None else None, **(extra or {})}
    return Assessment("CONFIRM", look_n, n, sessions, mean_r, mean_u, None, None, None,
                      family_m, len(pl), len(pl) / n if n else None, _mean([p.u for p in pl]), mismatches, unresolved,
                      impaired_frac, bar_frac, not reasons, tuple(reasons),
                      inputs_hash(strategy, version, rates_version, evidence, ev_pl), detail)


def assess_futility(strategy: str, version: str, sample: Sequence[Sample], checkpoint: int, *, family_m: int,
                    rates_version: str) -> Assessment:
    """FUTILITY at a checkpoint (n = 50, 75, ...): FUTILE when the 90% upper bound of mean u is below 0.
    passed = the version survives."""
    lb, ub, boot = _boot(strategy, version, sample, "u", G1_FUTILITY_ALPHA, "FUTILITY")
    futile = ub is not None and ub < 0
    return Assessment("FUTILITY", checkpoint, len(sample), len({x.session for x in sample}),
                      _mean([x.r for x in sample]), _mean([x.u for x in sample]), lb, ub, G1_FUTILITY_ALPHA, family_m,
                      None, None, None, None, sum(1 for x in sample if not x.resolved), None, None, not futile,
                      ("FUTILE",) if futile else (), inputs_hash(strategy, version, rates_version, sample, ()),
                      {**boot, **_span(sample)})


# ---------------------------------------------------------------------------------------------------------------
# Loading samples and window facts from the DB
# ---------------------------------------------------------------------------------------------------------------
def _samples(store: Store, rows: Sequence[Any], rates: RateSet) -> list[Sample]:
    vals = order_values_by_structure(store, [r["structure_id"] for r in rows])
    out = []
    for r in rows:
        vs = vals.get(r["structure_id"], [])
        net = net_pnl([of for v in vs if (of := v.order_fills()) is not None], rates).net
        complete = bool(vs) and all(v.status is PnlStatus.FINAL for v in vs)
        cost = max(float(r["hs_cost_rs"]), float(r["crossing_cost_rs"]), 0.0)
        out.append(Sample(r["structure_id"], r["session"], net, float(r["risk_basis_rs"]), cost, r["pnl_status"],
                          complete, r["parent_structure_id"], r["underlying"]))
    return out


def _population(store: Store, strategy: str, version: str, registered_ts: str, rates: RateSet) -> list[Sample]:
    """PAPER:<s> trades of this version, non-placebo, entered at or after the trial's registration, in booking order
    (the structure's closed_ts, which close() and close_external() stamp as they book the trade; then
    structure_id). A trade booked after a LOOK therefore comes after that look's sample, never inside it: the look
    prefix pop[:look] stays what the look judged, and the CONFIRM window pop[look:look + 25] is the next 25 trades
    booked, each assessed once. By structure_id (fixed at RESERVED), a structure booked a session late (a missed
    square-off, a leftover) slipped into the judged prefix unassessed and pushed one of its trades into the confirm
    window (review round 3)."""
    rows = store.query("SELECT t.* FROM trades t JOIN structures s ON s.structure_id = t.structure_id "
                       "WHERE t.book = ? AND t.strategy = ? AND t.strategy_version = ? AND t.is_placebo = 0 "
                       "AND t.entry_ts >= ? ORDER BY s.closed_ts, t.structure_id",
                       (paper_book(strategy), strategy, version, registered_ts))
    return _samples(store, rows, rates)


def _placebos(store: Store, strategy: str, parent_ids: Sequence[int], rates: RateSet) -> dict[int, Sample]:
    rows = query_in(store, "SELECT * FROM trades WHERE book = ? AND is_placebo = 1 AND parent_structure_id IN ({}) "
                           "ORDER BY structure_id", list(parent_ids), (placebo_book(strategy),))
    out: dict[int, Sample] = {}
    for p in _samples(store, rows, rates):
        out.setdefault(p.parent_id, p)
    return out


def _counts_against_book(kind: str, ev_book: str | None, p: Mapping[str, Any], book: str, symbols: set[str]) -> bool:
    if kind == "EOD_TRUTH_CHECK":
        if p.get("ok") is not False:
            return False
        if p.get("broker_net") is None or p.get("unattributed"):
            return True                          # the account's ledger cannot be verified: every book
        return any(b.get("book") == book and abs(b.get("diff") or 0.0) > PAISE for b in p.get("books") or ())
    if kind == "EOD_TRUTH_MISMATCH":
        # the alert's record of the failed EOD_TRUTH_CHECK written beside it, which counts that failure once, for
        #   the books it names (review round 2: with no book and no symbol this counted for every book, and twice
        #   for the bad one)
        return False
    if kind == "VERIFY_FLAT" and p.get("outcome") not in ("RESIDUAL", "SHORT"):
        return False
    if kind == "RECON":                          # a failed broker read is a mismatch in paper (§9.3)
        return p.get("status") == "BROKER_ERROR"
    if kind == "CLOSED_EXTERNAL" and (p.get("external_kind") or p.get("kind") or "MANUAL") not in (
            "MANUAL", "EXTERNAL_CLOSE"):
        return False
    sym = p.get("symbol")
    if ev_book is None and sym is None:
        return True                              # an account-wide finding counts for every book
    return ev_book == book or sym in symbols


def count_mismatches(store: Store, book: str, account: str, start: str, end: str) -> int:
    """G1 mismatches of `book` on `account` in sessions [start, end]: MISMATCH, UNKNOWN_POSITION, SHORT_DETECTED,
    FILLS_REPAIRED, a failed truth check, a residual or short at verify-flat, a failed broker read, and every
    manual external close (EXTERNAL orders of kind MANUAL). Attributed by event book or by the symbols the book
    traded in the window; findings with neither count for every book of the account."""
    symbols = {r["symbol"] for r in store.query("SELECT DISTINCT symbol FROM legs WHERE book = ? AND session "
                                                "BETWEEN ? AND ?", (book, start, end))}
    kinds = MISMATCH_KINDS + _CONDITIONAL_KINDS
    rows = store.query(f"SELECT kind, book, payload FROM events WHERE account = ? AND session BETWEEN ? AND ? "
                       f"AND kind IN ({','.join('?' * len(kinds))})", (account, start, end, *kinds))
    n = sum(1 for r in rows if _counts_against_book(r["kind"], r["book"], json.loads(r["payload"]), book, symbols))
    n += int(store.scalar(
        "SELECT COUNT(DISTINCT o.structure_id) FROM orders o JOIN structures s ON s.structure_id = o.structure_id "
        "WHERE s.book = ? AND s.account = ? AND s.session BETWEEN ? AND ? AND o.role = 'EXTERNAL' "
        "AND o.external_kind = 'MANUAL'", (book, account, start, end)) or 0)
    return n


def _window(store: Store, strategy: str, start: str, end: str) -> tuple[int, int]:
    """(mismatches, IMPAIRED sessions) in sessions [start, end]."""
    mism = count_mismatches(store, paper_book(strategy), "PAPER", start, end)
    impaired = int(store.scalar("SELECT COUNT(DISTINCT session) FROM session_flags WHERE strategy = ? AND "
                                "flag = 'IMPAIRED' AND session BETWEEN ? AND ?", (strategy, start, end)) or 0)
    return mism, impaired


def _bar_checks(store: Store, sample: Sequence[Sample], start: str,
                end: str) -> tuple[tuple[int, int], dict[str, Any]]:
    """opening_drive's 15:36 bar check (§10.3) for a sample: (checked, mismatched) over the sample's signal bars, the
    checks of its trades' (session, underlying), which is G1's input ("the sample's signal bars"). The engine also
    checks 20 seeded control symbols a day; those rows (with any other row of [start, end]) are a bar-builder health
    figure, returned in the detail and never pooled in: twenty quiet bars a day would dilute the 1-3 signal bars."""
    signal = {(x.session, x.underlying) for x in sample}
    n = bad = ctl_n = ctl_bad = 0
    seen = set()
    for r in store.query("SELECT session, symbol, ok FROM bar_checks WHERE session BETWEEN ? AND ?", (start, end)):
        miss = 0 if r["ok"] else 1
        if (r["session"], r["symbol"]) in signal:
            n, bad = n + 1, bad + miss
            seen.add((r["session"], r["symbol"]))
        else:
            ctl_n, ctl_bad = ctl_n + 1, ctl_bad + miss
    return (n, bad), {"bar_checks_other": [ctl_n, ctl_bad], "signal_bars_unchecked": len(signal - seen)}


def _diverted(store: Store, strategy: str, version: str, registered_ts: str) -> dict[str, Any]:
    """Operator-diverted intents traded in SHADOW:<s> (§10.7 selection control): printed, never in the sample."""
    rows = store.query("SELECT net_pct FROM trades WHERE book = ? AND strategy_version = ? AND blocked_by IS NOT "
                       "NULL AND entry_ts >= ?", (shadow_book(strategy), version, registered_ts))
    m = _mean([r["net_pct"] for r in rows])
    return {"n": len(rows), "exp_pct": None if m is None else round(m, 4)}


# ---------------------------------------------------------------------------------------------------------------
# Writers (core thread, inside the caller's transaction)
# ---------------------------------------------------------------------------------------------------------------
@dataclass
class G1Run:
    """What run_nightly() wrote and what the engine must do next (dispatch alerts, reload modes)."""

    reports: list[dict[str, Any]] = field(default_factory=list)
    transitions: list[tuple[str, str, str, str]] = field(default_factory=list)   # (strategy, version, from, to)
    mode_changes: list[tuple[str, str, str, str]] = field(default_factory=list)  # (strategy, from, to, reason)
    alerts: list[Alert] = field(default_factory=list)


def _write_report(store: Store, out: Any, strategy: str, version: str, a: Assessment, now_wall: datetime,
                  now_mono: float) -> int:
    rid = store.insert_g1_report(a.row(strategy, version, now_wall))
    summary = {"report_id": rid, "strategy": strategy, "version": version, "kind": a.kind, "look_n": a.look_n,
               "n": a.n, "sessions": a.sessions, "passed": a.passed, "fail_reasons": list(a.fail_reasons),
               "exp_pct": _pct(a.mean_u), "lb_pct": _pct(a.lb), "ub_pct": _pct(a.ub), "inputs_hash": a.inputs_hash}
    store.append_event("G1_REPORT", wall=now_wall, mono=now_mono, strategy=strategy, payload=summary)
    out.reports.append(summary)
    if a.kind in ("LOOK", "CONFIRM", "FUTILITY"):
        verdict = "passed" if a.passed else "failed: " + ", ".join(a.fail_reasons)
        out.alerts.append(Alert("INFO", "G1_REPORT", f"G1 {a.kind} {strategy} {version} n={a.n} sessions="
                                f"{a.sessions} exp {_pct(a.mean_u)}% stressed {_pct(a.mean_r)}% lb {_pct(a.lb)}% "
                                f"ub {_pct(a.ub)}%: {verdict}", f"g1:{strategy}:{version}:{a.kind}:{a.look_n}"))
    return rid


def _transition(store: Store, strategy: str, version: str, old: str, new: str, now_wall: datetime, now_mono: float,
                **extra: Any) -> tuple[str, str, str, str]:
    store.update_trial(strategy, version, state=new, state_ts=now_wall, **extra)
    store.append_event("TRIAL_STATE", wall=now_wall, mono=now_mono, strategy=strategy,
                       payload={"version": version, "from": old, "to": new, **extra})
    return strategy, version, old, new


def _to_shadow(store: Store, run: G1Run, strategy: str, version: str, reason: str, now_wall: datetime,
               now_mono: float) -> None:
    """A failed or futile trial sends its strategy to SHADOW (an OFF strategy stays OFF)."""
    row = store.strategy_mode(strategy)
    if row is None or row["version"] != version or row["mode"] not in ("PAPER", "LIVE"):
        return
    store.update_row("strategy_modes", {"strategy": strategy},
                     {"mode": "SHADOW", "pending_mode": None, "changed_ts": now_wall, "changed_by": "g1"})
    store.append_event("MODE_CHANGED", wall=now_wall, mono=now_mono, strategy=strategy,
                       payload={"version": version, "from": row["mode"], "to": "SHADOW", "by": "g1",
                                "reason": reason})
    run.mode_changes.append((strategy, row["mode"], "SHADOW", reason))


def _fail(store: Store, run: G1Run, strategy: str, version: str, old: str, new: str, why: str,
          now_wall: datetime, now_mono: float) -> None:
    run.transitions.append(_transition(store, strategy, version, old, new, now_wall, now_mono))
    _to_shadow(store, run, strategy, version, f"G1 {new}: {why}", now_wall, now_mono)
    run.alerts.append(Alert("WARN", f"G1_{new}", f"{strategy} {version}: G1 {new} ({why}); mode SHADOW. Only a new "
                            "version starts a new trial.", f"g1:{strategy}:{version}:{new}"))


def ensure_trial(store: Store, strategy: str, version: str, version_inputs: Mapping[str, Any], now_wall: datetime,
                 now_mono: float) -> tuple[int, bool]:
    """Register (strategy, version) the first time it runs in PAPER (the engine calls this when the strategy's mode
    is PAPER or LIVE). The strategy's other COLLECTING/CONFIRMING/CERTIFIED trials become RETIRED.
    Returns (family_seq, newly registered)."""
    seq, new = store.register_trial(strategy, version, now_wall, version_inputs)
    if new:
        store.append_event("TRIAL_STATE", wall=now_wall, mono=now_mono, strategy=strategy,
                           payload={"version": version, "from": None, "to": "COLLECTING", "family_seq": seq})
        for t in store.trials(strategy):
            if t["version"] != version and t["state"] in ACTIVE_STATES:
                _transition(store, strategy, t["version"], t["state"], "RETIRED", now_wall, now_mono)
    return seq, new


def run_nightly(store: Store, now_wall: datetime, now_mono: float, *,
                strategies: Iterable[str] | None = None) -> G1Run:
    """15:38: for the current version of every strategy with an active trial, evaluate due LOOKs, the CONFIRM and
    the FUTILITY checkpoints, then write the nightly PROGRESS report."""
    run = G1Run()
    rates = rates_for(session_of(now_wall))
    m = family_m(store)
    wanted = None if strategies is None else set(strategies)
    for row in store.strategy_modes():
        if wanted is not None and row["strategy"] not in wanted:
            continue
        trial = store.trial(row["strategy"], row["version"])
        if trial is not None and trial["state"] in ACTIVE_STATES:
            _evaluate_trial(store, run, trial, rates, m, now_wall, now_mono)
    return run


def _evaluate_trial(store: Store, run: G1Run, trial: Mapping[str, Any], rates: RateSet, m: int,
                    now_wall: datetime, now_mono: float) -> None:
    s, v = trial["strategy"], trial["version"]
    reg_ts = trial["registered_ts"]
    start, today = reg_ts[:10], session_of(now_wall).isoformat()
    pop = _population(store, s, v, reg_ts, rates)
    placebos = _placebos(store, s, [x.structure_id for x in pop], rates)
    needs_bar = s in BAR_CHECK_STRATEGIES
    looks_done = sorted(set(trial["looks_done"] or ()))
    state = trial["state"]
    common = {"family_m": m, "rates_version": rates.version}

    if state == "CONFIRMING":
        passed = store.g1_report(trial["pass_report_id"]) if trial["pass_report_id"] is not None else None
        if passed is None or passed["kind"] != "LOOK" or passed["look_n"] is None:
            raise ValueError(f"trial {s} {v} is CONFIRMING without a passing LOOK report")
        look = int(passed["look_n"])
        confirm = pop[look:look + G1_CONFIRM_TRADES]
        if len(confirm) >= G1_CONFIRM_TRADES:
            # from the look sample's last session, so no session between the two samples escapes the check
            lo = max(x.session for x in pop[:look]) if look else min(x.session for x in confirm)
            hi = max(x.session for x in confirm)
            mism, impaired = _window(store, s, lo, hi)
            bar, bar_extra = _bar_checks(store, confirm, lo, hi) if needs_bar else (None, {})
            a = assess_confirm(s, v, confirm, pop[:look + G1_CONFIRM_TRADES], placebos, mismatches=mism,
                               impaired_sessions=impaired, bar_checks=bar, needs_bar=needs_bar, look_n=look,
                               extra=bar_extra, **common)
            rid = _write_report(store, run, s, v, a, now_wall, now_mono)
            if a.passed:
                run.transitions.append(_transition(store, s, v, state, "CERTIFIED", now_wall, now_mono,
                                                   cert_report_id=rid))
                run.alerts.append(Alert("INFO", "G1_CERTIFIED", f"{s} {v} is CERTIFIED (report {rid}, hash "
                                        f"{a.inputs_hash}). Key 1: ALLOW_LIVE=1. Key 2: titan2 live {s} --confirm "
                                        f"{a.inputs_hash} --i-accept-live-risk (valid {G1_REPORT_MAX_AGE_DAYS} "
                                        "days).", f"g1:{s}:{v}:CERTIFIED"))
                state = "CERTIFIED"
            elif any(x not in looks_done for x in G1_LOOKS):
                run.transitions.append(_transition(store, s, v, state, "COLLECTING", now_wall, now_mono))
                state = "COLLECTING"
            else:
                _fail(store, run, s, v, state, "FAILED", "confirmation failed and no look remains", now_wall,
                      now_mono)
                state = "FAILED"

    if state == "COLLECTING":
        for look in G1_LOOKS:
            if look in looks_done or len(pop) < look:
                continue
            sample = pop[:look]
            end = max(x.session for x in sample)
            mism, impaired = _window(store, s, start, end)
            bar, bar_extra = _bar_checks(store, sample, start, end) if needs_bar else (None, {})
            a = assess_look(s, v, sample, placebos, kind="LOOK", look_n=look, mismatches=mism,
                            impaired_sessions=impaired, bar_checks=bar, needs_bar=needs_bar, extra=bar_extra,
                            **common)
            rid = _write_report(store, run, s, v, a, now_wall, now_mono)
            looks_done = sorted(set(looks_done) | {look})
            store.update_trial(s, v, looks_done=looks_done)
            if a.passed:
                run.transitions.append(_transition(store, s, v, state, "CONFIRMING", now_wall, now_mono,
                                                   pass_report_id=rid))
                state = "CONFIRMING"
                break
            if look == G1_LOOKS[-1]:
                _fail(store, run, s, v, state, "FAILED", f"last look {look}: {', '.join(a.fail_reasons)}",
                      now_wall, now_mono)
                state = "FAILED"
                break

    if state == "COLLECTING":
        done = {r["look_n"] for r in store.g1_reports(strategy=s, version=v, kind="FUTILITY")}
        for k in range(G1_FUTILITY_MIN_N, len(pop) + 1, G1_FUTILITY_EVERY):
            if k in done:
                continue
            a = assess_futility(s, v, pop[:k], k, **common)
            _write_report(store, run, s, v, a, now_wall, now_mono)
            if not a.passed:
                _fail(store, run, s, v, state, "FUTILE", f"90% upper bound of u {_pct(a.ub)}% < 0 at n={k}",
                      now_wall, now_mono)
                state = "FUTILE"
                break

    mism, impaired = _window(store, s, start, today)
    bar, bar_extra = _bar_checks(store, pop, start, today) if needs_bar else (None, {})
    a = assess_look(s, v, pop, placebos, kind="PROGRESS", mismatches=mism, impaired_sessions=impaired,
                    bar_checks=bar, needs_bar=needs_bar,
                    extra={"state": state, "diverted": _diverted(store, s, v, reg_ts), **bar_extra}, **common)
    _write_report(store, run, s, v, a, now_wall, now_mono)


# ---------------------------------------------------------------------------------------------------------------
# LIVE unlock check, demotion, live review
# ---------------------------------------------------------------------------------------------------------------
@dataclass(frozen=True, slots=True)
class UnlockCheck:
    ok: bool
    reason: str
    cert_report_id: int | None = None


def verify_unlock(store: Store, strategy: str, version: str, confirm_hash: str, now_wall: datetime) -> UnlockCheck:
    """Key 2's G1 part (§10.8): the current version's trial is CERTIFIED, the hash is its certificate's inputs_hash
    and the certificate is at most G1_REPORT_MAX_AGE_DAYS old. (A demoted trial can never be CERTIFIED again.) The
    operator also checks ALLOW_LIVE and that no GLOBAL ACK halt is active."""
    mode = store.strategy_mode(strategy)
    if mode is None or mode["version"] != version:
        return UnlockCheck(False, "VERSION_NOT_CURRENT")
    trial = store.trial(strategy, version)
    if trial is None:
        return UnlockCheck(False, "NO_TRIAL")
    if trial["state"] != "CERTIFIED" or trial["cert_report_id"] is None:
        return UnlockCheck(False, f"TRIAL_{trial['state']}")
    cert = store.g1_report(trial["cert_report_id"])
    if cert is None or cert["kind"] != "CONFIRM" or not cert["passed"]:
        return UnlockCheck(False, "NO_CERTIFICATE")
    if not isinstance(confirm_hash, str) or confirm_hash.strip().lower() != cert["inputs_hash"]:
        return UnlockCheck(False, "HASH_MISMATCH")
    age = now_wall - parse_wall(cert["computed_ts"])
    if age > timedelta(days=G1_REPORT_MAX_AGE_DAYS) or age < timedelta(0):
        return UnlockCheck(False, "CERTIFICATE_EXPIRED")
    return UnlockCheck(True, "OK", cert["report_id"])


@dataclass(frozen=True, slots=True)
class Demotion:
    strategy: str
    version: str
    reason: str
    old_mode: str
    new_mode: str

    def alert(self) -> Alert:
        return Alert("CRITICAL", "LIVE_DEMOTED", f"{self.strategy} {self.version} demoted ({self.old_mode} -> "
                     f"{self.new_mode}): {self.reason}. Open LIVE legs are still managed; LIVE again needs a new "
                     "version and a fresh G1.", f"live_demoted:{self.strategy}:{self.version}")


def demote(store: Store, strategy: str, reason: str, now_wall: datetime, now_mono: float, *,
           changed_by: str = "g1") -> Demotion | None:
    """Demote a LIVE (or pending-LIVE) strategy at once: mode PAPER, the pending LIVE cancelled, the current
    version's trial DEMOTED. No-op (None) for a strategy that is not LIVE. Open legs are untouched."""
    row = store.strategy_mode(strategy)
    if row is None or not (row["mode"] == "LIVE" or row["pending_mode"] == "LIVE"):
        return None
    v = row["version"]
    new_mode = "PAPER" if row["mode"] == "LIVE" else row["mode"]
    store.update_row("strategy_modes", {"strategy": strategy}, {
        "mode": new_mode, "pending_mode": None if row["pending_mode"] == "LIVE" else row["pending_mode"],
        "demoted_reason": reason, "changed_ts": now_wall, "changed_by": changed_by})
    trial = store.trial(strategy, v)
    if trial is not None and trial["state"] != "DEMOTED":
        _transition(store, strategy, v, trial["state"], "DEMOTED", now_wall, now_mono)
    store.append_event("LIVE_DEMOTED", wall=now_wall, mono=now_mono, severity="CRITICAL", strategy=strategy,
                       payload={"version": v, "reason": reason, "from": row["mode"], "to": new_mode,
                                "pending_was": row["pending_mode"], "by": changed_by})
    if new_mode != row["mode"]:
        store.append_event("MODE_CHANGED", wall=now_wall, mono=now_mono, strategy=strategy,
                           payload={"version": v, "from": row["mode"], "to": new_mode, "by": changed_by,
                                    "reason": reason})
    return Demotion(strategy, v, reason, row["mode"], new_mode)


def demote_all_live(store: Store, reason: str, now_wall: datetime, now_mono: float, *,
                    changed_by: str = "g1") -> list[Demotion]:
    """KILL, a LIVE reconciliation problem, the X2 falsifier: every LIVE strategy goes to PAPER at once."""
    out = []
    for row in store.strategy_modes():
        d = demote(store, row["strategy"], reason, now_wall, now_mono, changed_by=changed_by)
        if d is not None:
            out.append(d)
    return out


@dataclass(frozen=True, slots=True)
class X2Result:
    """The X2 paper-model falsifier over (LIVE:<s>, PAPER:<s>) twins of the same intent (current versions)."""

    trade_pairs: int                  # twins that both closed with a slip measurement
    median_gap_pp: float | None       # median (live - paper) slip_vs_walk, entry plus exit, % of premium
    order_pairs: int                  # twins that both sent an ENTRY order
    live_fill_rate_pct: float | None  # IOC filled qty / ordered qty (ENTRY and EXIT orders that reached the broker)
    paper_fill_rate_pct: float | None
    reasons: tuple[str, ...]

    @property
    def fired(self) -> bool:
        return bool(self.reasons)


def x2_falsifier(store: Store) -> X2Result:
    pairs = [(r["live_id"], r["paper_id"]) for r in store.query(
        "SELECT l.structure_id AS live_id, p.structure_id AS paper_id FROM structures l "
        "JOIN strategy_modes m ON m.strategy = l.strategy AND m.version = l.strategy_version "
        "JOIN structures p ON p.intent_id = l.intent_id AND p.book = 'PAPER:' || l.strategy "
        "AND p.account = 'PAPER' AND p.strategy_version = l.strategy_version "
        "WHERE l.account = 'LIVE' AND l.book = 'LIVE:' || l.strategy ORDER BY l.structure_id")]
    ids = sorted({i for pair in pairs for i in pair})
    stats = execution_stats(store, ids)
    traded = {r["structure_id"] for r in query_in(store, "SELECT structure_id FROM trades WHERE structure_id IN ({})",
                                                  ids)}
    gaps = []
    for lid, pid in pairs:
        lp, pp = stats[lid].slip_pct, stats[pid].slip_pct
        if lid in traded and pid in traded and lp is not None and pp is not None:
            gaps.append(lp - pp)
    order_pairs = [(lid, pid) for lid, pid in pairs if stats[lid].entry_orders and stats[pid].entry_orders]

    def rate(side_ids: Sequence[int]) -> float | None:
        q = sum(stats[i].ioc_qty for i in side_ids)
        return sum(stats[i].ioc_filled for i in side_ids) / q * 100.0 if q else None

    live_rate = rate([lid for lid, _ in order_pairs])
    paper_rate = rate([pid for _, pid in order_pairs])
    median = statistics.median(gaps) if gaps else None
    reasons = []
    if len(gaps) >= G1_X2_MIN_PAIRS and median > G1_X2_MAX_SLIP_GAP_PP:
        reasons.append("SLIP_GAP")
    if (len(order_pairs) >= G1_X2_MIN_PAIRS and live_rate is not None and paper_rate is not None
            and paper_rate - live_rate > G1_X2_MAX_FILL_RATE_GAP_PP):
        reasons.append("FILL_RATE_GAP")
    return X2Result(len(gaps), _round4(median), len(order_pairs), _round4(live_rate), _round4(paper_rate),
                    tuple(reasons))


def _round4(x: float | None) -> float | None:
    return None if x is None else round(x, 4) + 0.0


@dataclass
class LiveReview:
    x2: X2Result | None = None
    reports: list[dict[str, Any]] = field(default_factory=list)
    triggers: dict[str, tuple[str, ...]] = field(default_factory=dict)
    demotions: list[Demotion] = field(default_factory=list)
    alerts: list[Alert] = field(default_factory=list)


def _live_since(row: Mapping[str, Any]) -> str:
    """The first session that counts as live: live_since_session, else (pending LIVE) the unlock day."""
    return row["live_since_session"] or session_of(parse_wall(row["changed_ts"])).isoformat()


def _live_problems(store: Store, since: str) -> int:
    rows = store.query(f"SELECT kind, payload FROM events WHERE account = 'LIVE' AND session >= ? AND kind IN "
                       f"({','.join('?' * len(LIVE_PROBLEM_KINDS))})", (since, *LIVE_PROBLEM_KINDS))
    return sum(1 for r in rows
               if r["kind"] != "VERIFY_FLAT" or json.loads(r["payload"]).get("outcome") in ("RESIDUAL", "SHORT"))


def live_review(store: Store, now_wall: datetime, now_mono: float, settings: Settings = DEFAULTS) -> LiveReview:
    """§10.9, after every LIVE close and nightly. Every LIVE (or pending-LIVE) strategy gets a LIVE_REVIEW report and
    is demoted on: PROBATION (cumulative net <= -LIVE_PROBATION_LOSS_RS within its first LIVE_PROBATION_TRADES
    trades), DRAWDOWN (cumulative net since unlock <= -LIVE_DD_STOP_RS), ROLLING_50 (mean of the last 50 < 0),
    PAPER_MODEL_OPTIMISTIC (X2, demotes every LIVE strategy) or LIVE_RECON (a LIVE MISMATCH, UNKNOWN_POSITION or
    short since it went live). The global halt for a LIVE recon problem is the engine's (on ReconResult.live_demote)."""
    review = LiveReview()
    rows = [r for r in store.strategy_modes() if r["mode"] == "LIVE" or r["pending_mode"] == "LIVE"]
    if not rows:
        return review
    m = family_m(store)
    x2 = review.x2 = x2_falsifier(store)
    for row in rows:
        s, v = row["strategy"], row["version"]
        since = _live_since(row)
        trades = store.query("SELECT structure_id, session, net, net_pct, pnl_status FROM trades WHERE book = ? "
                             "AND strategy_version = ? AND session >= ? ORDER BY exit_ts, structure_id",
                             (live_book(s), v, since))
        nets = [float(t["net"]) for t in trades]
        cum = [round(x, 2) for x in itertools.accumulate(nets)]
        first = cum[:settings.LIVE_PROBATION_TRADES]
        rolling = (math.fsum(nets[-G1_LIVE_ROLLING_N:]) / G1_LIVE_ROLLING_N
                   if len(nets) >= G1_LIVE_ROLLING_N else None)
        problems = _live_problems(store, since)
        trig = []
        if first and min(first) <= -settings.LIVE_PROBATION_LOSS_RS:
            trig.append("PROBATION")
        if cum and min(cum) <= -settings.LIVE_DD_STOP_RS:
            trig.append("DRAWDOWN")
        if rolling is not None and rolling < 0:
            trig.append("ROLLING_50")
        if x2.fired:
            trig.append("PAPER_MODEL_OPTIMISTIC")
        if problems:
            trig.append("LIVE_RECON")
        review.triggers[s] = tuple(trig)
        doc = {"strategy": s, "version": v, "trades": [[t["structure_id"], round(float(t["net"]), 2) + 0.0]
                                                       for t in trades]}
        detail = {"since": since, "cum_net": cum[-1] if cum else 0.0, "min_cum_net": min(cum) if cum else None,
                  "probation_min_cum_net": min(first) if first else None, "rolling_mean_net": rolling,
                  "x2": {**asdict(x2), "fired": x2.fired}, "live_problems": problems}
        a = Assessment("LIVE_REVIEW", None, len(trades), len({t["session"] for t in trades}), None,
                       _mean([float(t["net_pct"]) / 100.0 for t in trades]), None, None, None, m, None, None, None,
                       problems, sum(1 for t in trades if t["pnl_status"] != PnlStatus.FINAL.value), None, None,
                       not trig, tuple(trig), hashlib.sha256(canonical_json(doc).encode("utf-8")).hexdigest(),
                       detail)
        _write_report(store, review, s, v, a, now_wall, now_mono)
        if trig:
            d = demote(store, s, ",".join(trig), now_wall, now_mono, changed_by="live_review")
            if d is not None:
                review.demotions.append(d)
                review.alerts.append(d.alert())
    if x2.fired and any("PAPER_MODEL_OPTIMISTIC" in d.reason for d in review.demotions):
        payload = {**asdict(x2), "demoted": [d.strategy for d in review.demotions]}
        store.append_event("PAPER_MODEL_OPTIMISTIC", wall=now_wall, mono=now_mono, severity="CRITICAL",
                           payload=payload)
        review.alerts.append(Alert("CRITICAL", "PAPER_MODEL_OPTIMISTIC", f"X2 falsifier fired "
                                   f"({', '.join(x2.reasons)}): median live-paper slip gap {x2.median_gap_pp} pp over "
                                   f"{x2.trade_pairs} pairs; IOC fill rate live {x2.live_fill_rate_pct}% vs paper "
                                   f"{x2.paper_fill_rate_pct}%. Every LIVE strategy is demoted; recalibrate PAPER_* "
                                   "(bumps every version).", "g1:x2"))
    return review


# ---------------------------------------------------------------------------------------------------------------
# Scorecards (pub/g1.json, status.json)
# ---------------------------------------------------------------------------------------------------------------
def next_look(trial: Mapping[str, Any] | None) -> int | None:
    if trial is None or trial["state"] != "COLLECTING":
        return None
    done = set(trial["looks_done"] or ())
    return next((x for x in G1_LOOKS if x not in done), None)


def _latest(store: Store, strategy: str, version: str, kind: str | None = None) -> dict[str, Any] | None:
    sql, params = "SELECT * FROM g1_reports WHERE strategy = ? AND version = ?", [strategy, version]
    if kind is not None:
        sql, params = sql + " AND kind = ?", params + [kind]
    row = store.query_one(sql + " ORDER BY report_id DESC LIMIT 1", params)
    if row is None:
        return None
    d = dict(row)
    d["detail"] = json.loads(d["detail"])
    return d


def trial_summary(store: Store, strategy: str, version: str) -> dict[str, Any] | None:
    """status.json strategies[].trial: {state, n, sessions, exp_pct, lb_pct, next_look} (progress, not a decision)."""
    trial = store.trial(strategy, version)
    if trial is None:
        return None
    prog = _latest(store, strategy, version, "PROGRESS")
    return {"state": trial["state"], "n": prog["n"] if prog else 0, "sessions": prog["sessions"] if prog else 0,
            "exp_pct": prog["exp_pct"] if prog else None, "lb_pct": prog["lb_pct"] if prog else None,
            "next_look": next_look(trial)}


_CARD_FIELDS = ("n", "sessions", "exp_pct", "exp_pct_stressed", "lb_pct", "ub_pct", "alpha", "family_m", "placebo_n",
                "placebo_coverage", "placebo_exp_pct", "mismatches", "unresolved", "impaired_frac",
                "bar_mismatch_frac", "fail_reasons")


def scorecards(store: Store) -> list[dict[str, Any]]:
    """One card per strategy (current version) for pub/g1.json: trial state, the latest PROGRESS numbers, the latest
    report of any kind, the next look and the certificate (if any)."""
    cards = []
    for row in store.strategy_modes():
        s, v = row["strategy"], row["version"]
        trial = store.trial(s, v)
        prog = _latest(store, s, v, "PROGRESS")
        last = _latest(store, s, v)
        cert = store.g1_report(trial["cert_report_id"]) if trial and trial["cert_report_id"] is not None else None
        card: dict[str, Any] = {
            "strategy": s, "version": v, "mode": row["mode"], "pending_mode": row["pending_mode"],
            "demoted_reason": row["demoted_reason"], "state": trial["state"] if trial else None,
            "family_seq": trial["family_seq"] if trial else None,
            "looks_done": list(trial["looks_done"]) if trial else [], "next_look": next_look(trial),
            "kind": last["kind"] if last else None, "computed_ts": prog["computed_ts"] if prog else None,
            "futility_ub_pct": prog["detail"].get("futility_ub_pct") if prog else None,
            "last_report": None if last is None else {k: last[k] for k in (
                "report_id", "kind", "look_n", "computed_ts", "passed", "fail_reasons", "inputs_hash")},
            "certificate": None if cert is None else {"report_id": cert["report_id"],
                                                      "inputs_hash": cert["inputs_hash"],
                                                      "computed_ts": cert["computed_ts"]},
        }
        for k in _CARD_FIELDS:
            card[k] = prog[k] if prog else None
        cards.append(card)
    return cards
