"""U02: titan2.settings: tighten-only LIMITS, read-once loading, invalid -> defaults, versioned_view, set_setting,
and the code constants (spec §8.3-8.4; the DETAIL §8.4 table is normative)."""

from __future__ import annotations

import dataclasses
import hashlib
import json
import math
import os
from datetime import datetime, time as dtime, timedelta

import pytest

from titan2 import settings as st
from titan2.model import ExitProfile
from titan2.settings import BAND, DEFAULTS, DOWN, LIMITS, SUBSET, UP, Settings, SettingsError

# DETAIL §8.4 "Default" column, in JSON form.
EXPECTED_DEFAULTS = {
    # Risk
    "MAX_LOTS": 1, "MAX_UNITS": 5000, "MAX_LOSS_PER_TRADE_RS": 15000.0, "MAX_PREMIUM_PER_TRADE_RS": 50000.0,
    "MIN_PREMIUM_RS": 20.0, "STOP_SLIP_ALLOW_PCT": 5.0, "DAILY_STOP_RS": 15000.0,
    "MAX_WORST_CASE_DAY_LOSS_RS": 50000.0, "MAX_POSITIONS_PER_BOOK": 2, "MAX_ENTRIES_PER_DAY": 3,
    "MAX_ENTRY_ATTEMPTS_PER_DAY": 6, "MAX_ORDERS_PER_DAY_PER_BOOK": 40, "MAX_ENTRY_ORDERS_PER_MIN": 3,
    "REENTRY_COOLDOWN_MIN": 30.0, "VOID_COOLDOWN_MIN": 5.0, "REJECT_COOLDOWN_MIN": 10.0,
    "STOP_AFTER_FIRST_LOSS": False,
    # Liquidity and freshness
    "LIQ_MAX_SPREAD_PCT": 1.5, "LIQ_DEPTH_MULT": 3.0, "LIQ_MAX_SLIP_VS_MID_PCT": 1.0,
    "LIQ_MAX_LAST_TRADE_AGE_S": 60.0, "MAX_CHARGES_HURDLE_PCT": 1.0, "QUOTE_MAX_AGE_ENTRY_S": 1.5,
    "UNDERLYING_MAX_AGE_S": 2.0, "STALE_REFRESH_S": 3.0, "STALE_PROTECTIVE_S": 5.0,
    # Times
    "ENTRY_START": "09:20:00", "NO_NEW_AFTER": "14:55:00", "EOD_FLATTEN": "15:15:00", "EOD_BACKSTOP": "15:22:00",
    "STOP_SENDING": "15:25:00", "BROKER_SQUAREOFF_AT": "15:26:00", "MIN_TD_TO_EXPIRY_INDEX": 2,
    "MIN_TD_TO_EXPIRY_STOCK": 5, "STOCK_ALLOWLIST": [], "STOCK_DENYLIST": [],
    # Paper
    "PAPER_PARTICIPATION": 0.5, "PAPER_LATENCY_MEDIAN_MS": 250.0, "PAPER_LATENCY_SIGMA": 0.6,
    "PAPER_READ_LATENCY_MEDIAN_MS": 250.0, "PAPER_READ_LATENCY_SIGMA": 0.85,   # decision B1: the paper read delay
    "PAPER_ADVERSE_TICKS": 0, "PAPER_PRICE_BAND_PCT": 20.0, "PAPER_BOOK_MAX_AGE_S": 1.0,
    "PAPER_CAPITAL_RS": 500000.0,
    # Unversioned
    "FEED_MAX_TICK_GAP_S": 5.0, "FEED_WARM_S": 60.0, "RECON_INTERVAL_S": 10.0, "RECON_MAX_AGE_ENTRY_S": 30.0,
    "LIVE_ACCOUNT_MAX_POSITIONS": 2, "LIVE_ACCOUNT_DAILY_STOP_RS": 10000.0, "LIVE_FIRST_WEEK_MAX_ENTRIES": 1,
    "LIVE_FIRST_WEEK_SESSIONS": 5, "LIVE_DD_STOP_RS": 15000.0, "LIVE_PROBATION_TRADES": 20,
    "LIVE_PROBATION_LOSS_RS": 6000.0, "LIVE_MIN_MARGIN_RS": 60000.0,
}

UNVERSIONED = {
    "FEED_MAX_TICK_GAP_S", "FEED_WARM_S", "RECON_INTERVAL_S", "RECON_MAX_AGE_ENTRY_S", "LIVE_ACCOUNT_MAX_POSITIONS",
    "LIVE_ACCOUNT_DAILY_STOP_RS", "LIVE_FIRST_WEEK_MAX_ENTRIES", "LIVE_FIRST_WEEK_SESSIONS", "LIVE_DD_STOP_RS",
    "LIVE_PROBATION_TRADES", "LIVE_PROBATION_LOSS_RS", "LIVE_MIN_MARGIN_RS",
}

# DETAIL §8.4 "Allowed (cannot loosen)" column: key -> (lo, hi, tighten). "sanity" marks a side the table leaves
# open that titan2 closes so nonsense (0 s staleness, overflowing paper latency) is refused.
SPEC_BOUNDS = {
    "MAX_LOTS": (0, 1, DOWN), "MAX_UNITS": (0, 5000, DOWN), "MAX_LOSS_PER_TRADE_RS": (0, 15000, DOWN),
    "MAX_PREMIUM_PER_TRADE_RS": (0, 50000, DOWN), "MIN_PREMIUM_RS": (20, 500, UP),
    "STOP_SLIP_ALLOW_PCT": (5, 15, UP), "DAILY_STOP_RS": (0, 15000, DOWN),
    "MAX_WORST_CASE_DAY_LOSS_RS": (0, 50000, DOWN), "MAX_POSITIONS_PER_BOOK": (0, 2, DOWN),
    "MAX_ENTRIES_PER_DAY": (0, 3, DOWN), "MAX_ENTRY_ATTEMPTS_PER_DAY": (0, 6, DOWN),
    "MAX_ORDERS_PER_DAY_PER_BOOK": (0, 40, DOWN), "MAX_ENTRY_ORDERS_PER_MIN": (0, 3, DOWN),
    "REENTRY_COOLDOWN_MIN": (30, 390, UP), "VOID_COOLDOWN_MIN": (5, 390, UP), "REJECT_COOLDOWN_MIN": (10, 390, UP),
    "STOP_AFTER_FIRST_LOSS": (False, True, UP),
    "LIQ_MAX_SPREAD_PCT": (0.1, 1.5, DOWN), "LIQ_DEPTH_MULT": (3, 20, UP), "LIQ_MAX_SLIP_VS_MID_PCT": (0.1, 1.0, DOWN),
    "LIQ_MAX_LAST_TRADE_AGE_S": (5, 60, DOWN), "MAX_CHARGES_HURDLE_PCT": (0.1, 1.0, DOWN),
    "QUOTE_MAX_AGE_ENTRY_S": (0.2, 1.5, DOWN), "UNDERLYING_MAX_AGE_S": (0.5, 2.0, DOWN),
    "STALE_REFRESH_S": (1.0, 3, DOWN),                  # lo: sanity
    "STALE_PROTECTIVE_S": (2.0, 5, DOWN),               # lo: sanity
    "ENTRY_START": ("09:20:00", None, UP), "NO_NEW_AFTER": (None, "14:55:00", DOWN),
    "EOD_FLATTEN": (None, "15:15:00", DOWN), "EOD_BACKSTOP": (None, "15:22:00", DOWN),
    # BROKER_SQUAREOFF_AT: DETAIL §8.4 ends the band at 15:29 (the old 15:30 close); the F&O close moved to 15:40 on
    # 2026-08-03 and Zerodha's square-off time after it is unverified [V], so any minute before 15:40 is allowed
    "STOP_SENDING": (None, "15:25:00", DOWN), "BROKER_SQUAREOFF_AT": ("15:20:00", "15:39:00", BAND),
    "MIN_TD_TO_EXPIRY_INDEX": (2, 10, UP), "MIN_TD_TO_EXPIRY_STOCK": (5, 15, UP),
    "STOCK_ALLOWLIST": (None, None, SUBSET), "STOCK_DENYLIST": (None, None, SUBSET),
    # paper model 2 (2026-10-14, the owner's delegate's decision): every key that only calibrates the paper exchange
    #   is fixed at its default, lo = hi (DETAIL §8.4 had 0.1-0.5, >=150 / >=0.3, >=0, 5-20 and 0.2-1.0; decision B1
    #   had >=250 / >=0.85 for the read delay). A file can move none of them to a safe side: test_paper_realism.py
    #   shows a lower participation, a wider sigma and a slower median each flattering paper
    "PAPER_PARTICIPATION": (0.5, 0.5, BAND),
    "PAPER_LATENCY_MEDIAN_MS": (250, 250, BAND),
    "PAPER_LATENCY_SIGMA": (0.6, 0.6, BAND),
    "PAPER_READ_LATENCY_MEDIAN_MS": (250, 250, BAND),
    "PAPER_READ_LATENCY_SIGMA": (0.85, 0.85, BAND),
    "PAPER_ADVERSE_TICKS": (0, 0, BAND),
    "PAPER_PRICE_BAND_PCT": (20, 20, BAND), "PAPER_BOOK_MAX_AGE_S": (1.0, 1.0, BAND),
    "PAPER_CAPITAL_RS": (100000, 500000, DOWN),         # the paper account's size, not a calibration
    "FEED_MAX_TICK_GAP_S": (1.0, 5, DOWN),              # lo: sanity
    "FEED_WARM_S": (60, None, UP),
    "RECON_INTERVAL_S": (2.0, 10, DOWN),                # lo: sanity
    "RECON_MAX_AGE_ENTRY_S": (5.0, 30, DOWN),           # lo: sanity
    "LIVE_ACCOUNT_MAX_POSITIONS": (0, 2, DOWN), "LIVE_ACCOUNT_DAILY_STOP_RS": (0, 10000, DOWN),
    "LIVE_FIRST_WEEK_MAX_ENTRIES": (0, 1, DOWN), "LIVE_FIRST_WEEK_SESSIONS": (5, None, UP),
    "LIVE_DD_STOP_RS": (0, 15000, DOWN), "LIVE_PROBATION_TRADES": (20, None, UP),
    "LIVE_PROBATION_LOSS_RS": (0, 6000, DOWN), "LIVE_MIN_MARGIN_RS": (60000, None, UP),
}
# the keys that only calibrate the paper exchange: fixed at their defaults, a file may set nothing else
FIXED_PAPER = {"PAPER_PARTICIPATION", "PAPER_LATENCY_MEDIAN_MS", "PAPER_LATENCY_SIGMA", "PAPER_READ_LATENCY_MEDIAN_MS",
               "PAPER_READ_LATENCY_SIGMA", "PAPER_ADVERSE_TICKS", "PAPER_PRICE_BAND_PCT", "PAPER_BOOK_MAX_AGE_S"}

# sha256 of canonical_json(DEFAULTS.versioned_view()). Every strategy version hashes this view, so a change to any
# versioned default re-versions every strategy (a new G1 trial). Update only on purpose.
DEFAULT_VIEW_SHA256 = "db075d1c49e86282338b0dbe66c3aee79b905554313d73fe4369e811db0e9524"


def write(path, content):
    path.write_text(content if isinstance(content, str) else json.dumps(content), encoding="utf-8")
    return path


def load_obj(path, obj):
    return st.load(write(path, obj))


def check(obj):
    """(settings or None, errors) from from_mapping: the exact validation load() applies once the file is read."""
    return st.from_mapping(json.loads(json.dumps(obj)))


def as_json(v):
    return v.isoformat(timespec="seconds") if isinstance(v, dtime) else v


def nudge(lim, v, direction):
    """v moved one small step up (+1) or down (-1), in JSON form."""
    if lim.kind == "time":
        t = datetime(2026, 1, 1, v.hour, v.minute, v.second) + timedelta(seconds=direction)
        return t.time().isoformat(timespec="seconds")
    if lim.kind == "int":
        return v + direction
    return v + direction * 0.001


def tighter(lim):
    """A valid value one step safer than the default, in JSON form (None for a fixed key: it has no other value)."""
    if lim.kind == "bool":
        return True
    if lim.kind == "list":
        return ["RELIANCE"]
    if lim.fixed:
        return None
    if lim.tighten == BAND:
        return "15:27:00"
    return nudge(lim, lim.default, -1 if lim.tighten == DOWN else +1)


@pytest.fixture
def fresh_once():
    st._reset_load_once()
    yield
    st._reset_load_once()


# ---------------------------------------------------------------------------------------------------------------
# Acceptance tests
# ---------------------------------------------------------------------------------------------------------------
def test_defaults_when_missing(tmp_path, monkeypatch):
    # no file: valid, code defaults, nothing to hash
    r = st.load(tmp_path / "absent.json")
    assert r.valid and not r.exists and r.errors == () and r.sha256 is None
    assert r.settings == Settings() == DEFAULTS
    assert "code defaults" in r.summary()
    # the default path honours TITAN2_VAR; a missing config directory is "no file", not an error
    monkeypatch.setenv("TITAN2_VAR", str(tmp_path / "var"))
    assert st.default_path() == tmp_path / "var" / "config" / "settings.json"
    r = st.load()
    assert r.valid and not r.exists and r.settings == DEFAULTS
    monkeypatch.delenv("TITAN2_VAR")
    assert st.default_path().as_posix() == "/var/lib/titan2/config/settings.json"

    # an empty object, and a partial file: every absent key takes its default
    p = tmp_path / "settings.json"
    assert load_obj(p, {}).settings == DEFAULTS
    r = load_obj(p, {"MAX_ENTRIES_PER_DAY": 1, "ENTRY_START": "09:45"})
    assert r.valid and r.exists and r.errors == ()
    assert r.sha256 == hashlib.sha256(p.read_bytes()).hexdigest()
    assert r.settings == dataclasses.replace(DEFAULTS, MAX_ENTRIES_PER_DAY=1, ENTRY_START=dtime(9, 45))
    assert "valid" in r.summary()

    # the defaults are exactly the DETAIL §8.4 table, with the declared types
    got = DEFAULTS.to_json()
    assert got == EXPECTED_DEFAULTS and list(got) == list(EXPECTED_DEFAULTS)
    assert all(type(got[k]) is type(v) for k, v in EXPECTED_DEFAULTS.items())
    assert {k for k, lim in LIMITS.items() if not lim.versioned} == UNVERSIONED
    assert [f.name for f in dataclasses.fields(Settings)] == list(LIMITS)
    assert all(lim.default == getattr(DEFAULTS, k) for k, lim in LIMITS.items())
    assert DEFAULTS.problems() == []


BAD_FILES = [
    ('{"MAX_LOTZ": 1}', "'MAX_LOTZ': unknown key"),
    ('{"max_lots": 1}', "did you mean MAX_LOTS?"),
    ('{"MAX_LOTS": "1"}', "MAX_LOTS: '1' is not an integer"),
    ('{"MAX_LOTS": 1.0}', "MAX_LOTS: 1.0 is not an integer"),
    ('{"MAX_UNITS": 1e3}', "MAX_UNITS: 1000.0 is not an integer"),
    ('{"MAX_LOTS": true}', "MAX_LOTS: True is not an integer"),
    ('{"MAX_LOTS": null}', "MAX_LOTS: None is not an integer"),
    ('{"DAILY_STOP_RS": "5000"}', "DAILY_STOP_RS: '5000' is not a finite number"),
    ('{"DAILY_STOP_RS": false}', "DAILY_STOP_RS: False is not a finite number"),
    ('{"DAILY_STOP_RS": [5000]}', "DAILY_STOP_RS: [5000] is not a finite number"),
    ('{"STOP_AFTER_FIRST_LOSS": 1}', "STOP_AFTER_FIRST_LOSS: 1 is not true or false"),
    ('{"STOP_AFTER_FIRST_LOSS": "true"}', "STOP_AFTER_FIRST_LOSS: 'true' is not true or false"),
    ('{"ENTRY_START": 930}', "ENTRY_START: 930 is not a time of day"),
    ('{"ENTRY_START": "9:30"}', "ENTRY_START: '9:30' is not a time of day"),
    ('{"ENTRY_START": "09:30:00.500"}', "ENTRY_START: '09:30:00.500' is not a time of day"),
    ('{"ENTRY_START": "09:30+05:30"}', "ENTRY_START: '09:30+05:30' is not a time of day"),
    ('{"ENTRY_START": "24:00"}', "ENTRY_START: '24:00' is not a time of day"),
    ('{"STOCK_DENYLIST": "YESBANK"}', "STOCK_DENYLIST: 'YESBANK' is not a list of NSE symbol names"),
    ('{"STOCK_DENYLIST": [1]}', "STOCK_DENYLIST: 1 is not an NSE symbol name"),
    ('{"STOCK_DENYLIST": ["YES BANK"]}', "STOCK_DENYLIST: 'YES BANK' is not an NSE symbol name"),
    ('{"STOCK_DENYLIST": ["NFO:YESBANK"]}', "STOCK_DENYLIST: 'NFO:YESBANK' is not an NSE symbol name"),
    ('{"STOCK_DENYLIST": [""]}', "STOCK_DENYLIST: '' is not an NSE symbol name"),
    ('{"DAILY_STOP_RS": NaN}', "NaN is not allowed"),
    ('{"DAILY_STOP_RS": Infinity}', "Infinity is not allowed"),
    ('{"DAILY_STOP_RS": -Infinity}', "-Infinity is not allowed"),
    ('{"DAILY_STOP_RS": 1e999}', "DAILY_STOP_RS: inf is not a finite number"),
    ('{"MAX_LOTS": 1, "MAX_LOTS": 0}', "duplicate key 'MAX_LOTS'"),
    ('[{"MAX_LOTS": 1}]', "must hold one JSON object"),
    ('null', "must hold one JSON object"),
    ('{"MAX_LOTS": 1', "is not valid JSON"),
    ('{"MAX_LOTS": 1} {}', "is not valid JSON"),
    ('', "is not valid JSON"),
]


def test_invalid_unknown_type_nan(tmp_path):
    p = tmp_path / "settings.json"
    for text, fragment in BAD_FILES:
        r = st.load(write(p, text))
        assert not r.valid, text
        assert r.exists and r.sha256 == hashlib.sha256(p.read_bytes()).hexdigest()
        assert r.settings is DEFAULTS, text                  # the whole file is ignored: code defaults
        assert any(fragment in e for e in r.errors), (text, r.errors)
        assert "INVALID" in r.summary()

    # one bad key voids the whole file, including its valid tightenings, and every problem is reported
    r = load_obj(p, {"MAX_ENTRIES_PER_DAY": 1, "BOGUS": 1, "MAX_LOTS": "1", "ENTRY_START": "9:20"})
    assert not r.valid and r.settings.MAX_ENTRIES_PER_DAY == 3
    assert len(r.errors) == 3

    # odd files are invalid, never fatal
    d = tmp_path / "a_dir.json"
    d.mkdir()
    assert any("not a regular file" in e for e in st.load(d).errors)
    big = write(tmp_path / "big.json", '{"MAX_LOTS": 1' + " " * st.MAX_FILE_BYTES + "}")
    assert any("larger than" in e for e in st.load(big).errors)
    latin = tmp_path / "latin.json"
    latin.write_bytes(b'{"STOCK_DENYLIST": ["\xff"]}')
    assert not st.load(latin).valid
    deep = write(tmp_path / "deep.json", '{"STOCK_DENYLIST": ' + "[" * 30000 + "]" * 30000 + "}")
    assert not st.load(deep).valid                           # RecursionError is caught, not raised
    # a UTF-8 byte-order mark is harmless
    bom = tmp_path / "bom.json"
    bom.write_bytes(b'\xef\xbb\xbf{"MAX_ENTRIES_PER_DAY": 1}')
    assert st.load(bom).valid and st.load(bom).settings.MAX_ENTRIES_PER_DAY == 1


def test_cannot_loosen_each_key(tmp_path):
    # the bounds are the DETAIL §8.4 table
    assert {k: (as_json(lim.lo), as_json(lim.hi), lim.tighten) for k, lim in LIMITS.items()} == SPEC_BOUNDS
    p = tmp_path / "settings.json"
    cli_dir = tmp_path / "cli"
    cli_dir.mkdir()
    cli = cli_dir / "settings.json"

    loosened = 0
    for key, lim in LIMITS.items():
        if lim.kind in ("bool", "list"):
            continue
        sides = []
        if lim.tighten in (DOWN, BAND):
            sides.append((lim.hi, +1))
        if lim.tighten in (UP, BAND):
            sides.append((lim.lo, -1))
        for edge, direction in sides:
            looser = nudge(lim, edge, direction)
            r = load_obj(p, {key: looser})
            assert not r.valid and r.settings is DEFAULTS, (key, looser)
            assert r.errors[0].startswith(f"{key}: "), (key, r.errors)
            if lim.tighten != BAND:
                assert r.errors[0].endswith("(cannot loosen)"), r.errors
            if lim.fixed:
                assert r.errors[0].endswith("(fixed: only a code change can change it)"), r.errors
            with pytest.raises(SettingsError, match=key):
                st.set_setting(key, looser, cli)            # the CLI refuses it too and writes nothing
            assert not cli.exists()
            loosened += 1
        # the bound itself is allowed (BAND: its upper end; its lower end is in test_cross_key_rules)
        edge = lim.hi if lim.tighten == BAND else lim.bound
        s, errors = check({key: as_json(edge)})
        assert s is not None and getattr(s, key) == edge, (key, errors)
        # the sanity side is closed as well
        if lim.tighten == DOWN and lim.lo is not None:
            assert check({key: nudge(lim, lim.lo, -1)})[0] is None, key
        if lim.tighten == UP and lim.hi is not None:
            assert check({key: nudge(lim, lim.hi, +1)})[0] is None, key
    # 54 numeric/time keys; the 9 BAND ones (BROKER_SQUAREOFF_AT and the 8 fixed paper keys) on both sides
    assert loosened == 63

    # spot checks in the table's own words
    assert not load_obj(p, {"MAX_LOTS": 2}).valid                     # {0, 1}: the phase-1 code ceiling
    assert load_obj(p, {"MAX_LOTS": 0}).valid                         # 0 is allowed; size() then refuses entries
    assert not load_obj(p, {"ENTRY_START": "09:19:59"}).valid         # no entry before 09:20 (stale open quotes)
    assert not check({"BROKER_SQUAREOFF_AT": "15:40"})[0]              # never the close itself
    assert not check({"MIN_PREMIUM_RS": 19.99})[0]
    assert check({"STOP_AFTER_FIRST_LOSS": True})[0]                   # true is tighter
    assert check({"STOP_AFTER_FIRST_LOSS": False})[0]
    assert check({"STOCK_ALLOWLIST": ["RELIANCE"], "STOCK_DENYLIST": ["YESBANK"]})[0]   # lists only restrict
    # paper model 2: the paper exchange's calibration is fixed. The read delay (decision B1) and the order latency
    #   can be neither faster nor slower: a slower one is no safer (it can land a stop's exit after a gap), and it was
    #   accepted until the review of paper model 2. Nor can the participation go lower (test_every_bound_is_its_default)
    assert not check({"PAPER_READ_LATENCY_MEDIAN_MS": 249.0})[0] and not check({"PAPER_READ_LATENCY_SIGMA": 0.8})[0]
    assert not check({"PAPER_READ_LATENCY_MEDIAN_MS": 400.0})[0] and not check({"PAPER_READ_LATENCY_SIGMA": 1.0})[0]
    assert not check({"PAPER_LATENCY_MEDIAN_MS": 150.0, "PAPER_LATENCY_SIGMA": 0.3})[0]
    assert not check({"PAPER_LATENCY_MEDIAN_MS": 3000.0})[0] and not check({"PAPER_LATENCY_SIGMA": 2.0})[0]
    assert not check({"PAPER_PARTICIPATION": 0.1})[0]
    assert check({"PAPER_LATENCY_MEDIAN_MS": 250.0, "PAPER_PARTICIPATION": 0.5})[0]      # the defaults themselves


def test_every_bound_is_its_default():
    """Tighten-only, for every key (review round 2): the loosest value a file may set is the default, so the low bound
    of every UP key and the high bound of every DOWN key equal its default.

    The keys that only calibrate the paper exchange are not settings to tighten (the review of paper model 2; the
    owner's delegate's decision). 'Higher sigma is tighter' was false: each order draws a fixed seeded z, so a wider
    sigma makes every order with z < 0 faster. So was 'lower participation is tighter' (a lone gap bid that a live
    IOC takes is missed more often) and 'a slower median is tighter' (the stop's IOC can land after the gap):
    test_paper_realism.py shows all three. They are fixed at their defaults, lo = hi = default (BAND, no safer side):
    a file may set nothing else, on either side. Only a code change, with a PAPER_MODEL_VERSION bump, moves one; they
    stay versioned. BROKER_SQUAREOFF_AT and the SUBSET lists have no safer side either."""
    loose = [(k, lim.bound, lim.default) for k, lim in LIMITS.items()
             if lim.tighten in (UP, DOWN) and lim.bound != lim.default]
    assert loose == [], f"keys a settings file can loosen past their default: {loose}"
    assert {k for k, lim in LIMITS.items() if lim.tighten not in (UP, DOWN)} == {
        "BROKER_SQUAREOFF_AT", "STOCK_ALLOWLIST", "STOCK_DENYLIST"} | FIXED_PAPER
    assert {k for k, lim in LIMITS.items() if lim.fixed} == FIXED_PAPER
    for key in sorted(FIXED_PAPER):
        lim = LIMITS[key]
        assert lim.tighten == BAND and lim.lo == lim.hi == lim.default and lim.versioned, key
        assert lim.allowed_text() == f"{lim.default!r} only (fixed in code)", key
        assert check({key: lim.default})[0] is not None, key              # a file may still restate the default
        for v in (nudge(lim, lim.default, -1), nudge(lim, lim.default, +1)):
            s, errors = check({key: v})
            assert s is None and errors == [f"{key}: {v!r} is not {lim.default!r} (fixed: only a code change can "
                                            "change it)"], (key, v, errors)
    # what the review's probes set, all accepted until then as tightenings ('higher' or 'lower is tighter')
    for probe in ({"PAPER_LATENCY_SIGMA": 2.0}, {"PAPER_LATENCY_MEDIAN_MS": 3000.0}, {"PAPER_PARTICIPATION": 0.1},
                  {"PAPER_READ_LATENCY_SIGMA": 2.0}):
        assert check(probe)[0] is None, probe
    # the paper account's size is no calibration: a smaller one only refuses more buys, so it stays tighten-only
    assert LIMITS["PAPER_CAPITAL_RS"].tighten == DOWN and check({"PAPER_CAPITAL_RS": 100000.0})[0] is not None


def test_versioned_view_stable(tmp_path):
    base = DEFAULTS.versioned_view()
    assert set(base) == {k for k, lim in LIMITS.items() if lim.versioned} == set(st.VERSIONED_KEYS)
    assert not set(base) & UNVERSIONED
    assert base == Settings().versioned_view()
    # JSON-native values only, so canonical_json never needs its repr() fallback, and the digest is pinned
    text = st.canonical_json(base)
    assert text == json.dumps(base, sort_keys=True, separators=(",", ":"))
    assert json.loads(text) == base
    assert hashlib.sha256(text.encode("utf-8")).hexdigest() == DEFAULT_VIEW_SHA256

    p = tmp_path / "settings.json"
    # the same values spelled differently load to the same view
    restated = {"MAX_LOSS_PER_TRADE_RS": 15000, "LIQ_DEPTH_MULT": 3, "PAPER_CAPITAL_RS": 5e5, "ENTRY_START": "09:20",
                "STOP_SENDING": "15:25:00", "STOCK_ALLOWLIST": [], "MAX_LOTS": 1}
    r = load_obj(p, restated)
    assert r.valid and r.settings == DEFAULTS and r.settings.versioned_view() == base
    # symbol lists: case, "NSE:" prefixes, spaces, duplicates and order do not matter
    a = load_obj(p, {"STOCK_DENYLIST": ["yesbank", "NSE:IDEA", " M&M ", "IDEA"]}).settings
    b = load_obj(p, {"STOCK_DENYLIST": ["M&M", "IDEA", "YESBANK"]}).settings
    assert a == b and a.STOCK_DENYLIST == ("IDEA", "M&M", "YESBANK")
    assert a.versioned_view() == b.versioned_view()
    assert a.versioned_view()["STOCK_DENYLIST"] == ["IDEA", "M&M", "YESBANK"]

    # a versioned key always moves the view (a new G1 trial); an unversioned one never does. A fixed key has no other
    #   value a file may set: only a code change of its default moves the view (test_strategies.py)
    for key, lim in LIMITS.items():
        if lim.fixed:
            continue
        s, errors = check({key: tighter(lim)})
        assert s is not None, (key, errors)
        assert (s.versioned_view() != base) is lim.versioned, key

    # the view is a fresh dict: editing it cannot change the settings
    v = DEFAULTS.versioned_view()
    v["MAX_LOTS"] = 0
    assert DEFAULTS.versioned_view()["MAX_LOTS"] == 1
    # the engine-side version inputs (§10.2)
    assert DEFAULTS.version_basis() == {"settings": base, "engine": st.ENGINE_SEMANTICS_VERSION,
                                        "paper_model": st.PAPER_MODEL_VERSION,
                                        "semantic_constants": st.SEMANTIC_HASH}


# ---------------------------------------------------------------------------------------------------------------
# Supporting tests
# ---------------------------------------------------------------------------------------------------------------
def test_cross_key_rules(tmp_path):
    p = tmp_path / "settings.json"
    bad = [
        ({"STALE_REFRESH_S": 2.0, "STALE_PROTECTIVE_S": 2.0}, "STALE_PROTECTIVE_S"),   # protective > refresh
        ({"ENTRY_START": "14:55"}, "ENTRY_START"),                 # an empty entry window
        ({"EOD_FLATTEN": "14:50"}, "NO_NEW_AFTER"),                # entries would open after the flatten
        ({"EOD_BACKSTOP": "15:15"}, "EOD_FLATTEN"),                # not strictly after the flatten
        ({"STOP_SENDING": "15:22"}, "EOD_BACKSTOP"),
        ({"BROKER_SQUAREOFF_AT": "15:25"}, "STOP_SENDING"),        # the bot must stop before the broker squares off
        ({"BROKER_SQUAREOFF_AT": "15:20"}, "STOP_SENDING"),
    ]
    for obj, fragment in bad:
        r = load_obj(p, obj)
        assert not r.valid and r.settings is DEFAULTS, obj
        assert any(fragment in e for e in r.errors), (obj, r.errors)
    # an earlier timeline is fine, down to the band's lower end for the broker square-off
    early = {"NO_NEW_AFTER": "14:00", "EOD_FLATTEN": "14:00", "EOD_BACKSTOP": "14:30", "STOP_SENDING": "15:00",
             "BROKER_SQUAREOFF_AT": "15:20", "STALE_REFRESH_S": 1.0, "STALE_PROTECTIVE_S": 2.0}
    r = load_obj(p, early)
    assert r.valid, r.errors
    assert r.settings.BROKER_SQUAREOFF_AT == dtime(15, 20) and r.settings.EOD_FLATTEN == dtime(14, 0)


def test_squareoff_after_the_old_close_is_configurable(tmp_path):
    """Late fact: NSE F&O trades until 15:40 since 2026-08-03 and Zerodha's MIS square-off time after it is
    unverified [V]. A verified time after the old 15:30 close (15:35: close minus 5 minutes, as before) must load
    as a valid file, not send the bot to code defaults with HALT(SETTINGS_INVALID)."""
    p = tmp_path / "settings.json"
    for hhmm in ("15:30", "15:35", "15:39"):
        r = load_obj(p, {"BROKER_SQUAREOFF_AT": hhmm})
        assert r.valid and r.settings.BROKER_SQUAREOFF_AT.isoformat(timespec="minutes") == hhmm, (hhmm, r.errors)
        assert Settings(BROKER_SQUAREOFF_AT=r.settings.BROKER_SQUAREOFF_AT).problems() == []
    assert st.set_setting("BROKER_SQUAREOFF_AT", "15:35", p).after.BROKER_SQUAREOFF_AT == dtime(15, 35)
    assert not load_obj(p, {"BROKER_SQUAREOFF_AT": "15:40"}).valid                  # at the close: too late


def test_problems_flag_out_of_limit_objects():
    assert DEFAULTS.problems() == []
    cases = [
        ({"MAX_LOTS": 2}, "MAX_LOTS"), ({"MAX_LOTS": True}, "MAX_LOTS"), ({"MAX_UNITS": 10 ** 400}, "MAX_UNITS"),
        ({"DAILY_STOP_RS": math.nan}, "DAILY_STOP_RS"), ({"DAILY_STOP_RS": math.inf}, "DAILY_STOP_RS"),
        ({"DAILY_STOP_RS": 10 ** 400}, "DAILY_STOP_RS"), ({"ENTRY_START": "09:20"}, "ENTRY_START"),
        ({"STOCK_DENYLIST": ("yesbank",)}, "STOCK_DENYLIST"), ({"STOP_AFTER_FIRST_LOSS": 1}, "STOP_AFTER_FIRST_LOSS"),
        ({"STALE_PROTECTIVE_S": 3.0}, "STALE_PROTECTIVE_S"),
    ]
    for kw, key in cases:
        probs = dataclasses.replace(DEFAULTS, **kw).problems()   # invariant I16 on an object built in code
        assert probs and key in probs[0], (kw, probs)
    # immutable after load, and hashable even when code passes a list of names
    with pytest.raises(dataclasses.FrozenInstanceError):
        DEFAULTS.MAX_LOTS = 0
    s = Settings(STOCK_DENYLIST=["YESBANK", "IDEA", "IDEA"])
    assert s.STOCK_DENYLIST == ("IDEA", "YESBANK") and s.problems() == []
    assert hash(s) == hash(Settings(STOCK_DENYLIST=("YESBANK", "IDEA")))
    assert st.diff(DEFAULTS, s) == {"STOCK_DENYLIST": ([], ["IDEA", "YESBANK"])}
    assert st.diff(DEFAULTS, DEFAULTS) == {}


def test_load_once_reads_file_once(tmp_path, fresh_once):
    p = write(tmp_path / "settings.json", {"MAX_ENTRIES_PER_DAY": 1})
    first = st.load_once(p)
    write(p, {"MAX_ENTRIES_PER_DAY": 0})                     # an edit after start never reaches this process
    assert st.load_once(p) is first and st.load_once() is first
    assert first.settings.MAX_ENTRIES_PER_DAY == 1
    with pytest.raises(RuntimeError, match="read once"):
        st.load_once(tmp_path / "other.json")
    # load() itself stays a pure read (the CLI and the self-check use it)
    assert st.load(p).settings.MAX_ENTRIES_PER_DAY == 0


def test_set_setting_validates_and_writes_atomically(tmp_path):
    p = tmp_path / "settings.json"
    r = st.set_setting("MAX_ENTRIES_PER_DAY", "1", p)
    assert r.changed and r.versioned and (r.old, r.new) == (3, 1)
    assert r.before == DEFAULTS and r.after.MAX_ENTRIES_PER_DAY == 1
    assert "next restart" in r.message() and "new version" in r.message()
    assert json.loads(p.read_text(encoding="utf-8")) == {"MAX_ENTRIES_PER_DAY": 1}
    assert st.load(p).settings.MAX_ENTRIES_PER_DAY == 1
    # CLI text for every kind; unversioned keys say so
    st.set_setting("ENTRY_START", "09:30", p)
    st.set_setting("STOCK_DENYLIST", "yesbank, NSE:idea", p)
    st.set_setting("STOP_AFTER_FIRST_LOSS", "true", p)
    st.set_setting("DAILY_STOP_RS", "7500", p)
    r = st.set_setting("FEED_WARM_S", "120", p)
    assert r.changed and not r.versioned and "new version" not in r.message()
    assert json.loads(p.read_text(encoding="utf-8")) == {
        "MAX_ENTRIES_PER_DAY": 1, "ENTRY_START": "09:30:00", "STOCK_DENYLIST": ["IDEA", "YESBANK"],
        "STOP_AFTER_FIRST_LOSS": True, "DAILY_STOP_RS": 7500.0, "FEED_WARM_S": 120.0}
    loaded = st.load(p)
    assert loaded.valid and loaded.settings == r.after
    # Python values work as well; the same value again writes nothing
    before = p.read_bytes()
    r = st.set_setting("ENTRY_START", dtime(9, 30), p)
    assert not r.changed and "nothing written" in r.message() and p.read_bytes() == before

    # every refusal leaves the file byte-identical
    for key, text in [("MAX_ENTRIES_PER_DAY", "4"), ("MAX_LOTS", "1.0"), ("DAILY_STOP_RS", "NaN"),
                      ("DAILY_STOP_RS", "-1"), ("BOGUS", "1"), ("max_lots", "0"), ("EOD_FLATTEN", "14:00"),
                      ("STOCK_DENYLIST", "YES BANK"), ("ENTRY_START", "9:30")]:
        with pytest.raises(SettingsError) as info:
            st.set_setting(key, text, p)
        assert info.value.errors, key
        assert p.read_bytes() == before, key
    # no temporary files are left behind
    assert sorted(x.name for x in tmp_path.iterdir()) == ["settings.json"]
    assert st.diff(DEFAULTS, loaded.settings).keys() == {
        "MAX_ENTRIES_PER_DAY", "ENTRY_START", "STOCK_DENYLIST", "STOP_AFTER_FIRST_LOSS", "DAILY_STOP_RS",
        "FEED_WARM_S"}


def test_set_setting_failed_write_leaves_file(tmp_path, monkeypatch):
    p = tmp_path / "settings.json"
    st.set_setting("MAX_ENTRIES_PER_DAY", 1, p)
    before = p.read_bytes()

    def boom(src, dst):
        raise OSError("disk full")

    monkeypatch.setattr(os, "replace", boom)
    with pytest.raises(SettingsError, match="disk full"):
        st.set_setting("MAX_ENTRIES_PER_DAY", 0, p)
    monkeypatch.undo()
    assert p.read_bytes() == before
    assert sorted(x.name for x in tmp_path.iterdir()) == ["settings.json"]
    # a missing directory is reported, not created
    with pytest.raises(SettingsError, match="cannot write"):
        st.set_setting("MAX_LOTS", 0, tmp_path / "nope" / "settings.json")


def test_unset_repairs_an_invalid_file(tmp_path):
    p = write(tmp_path / "settings.json", {"MAX_ENTRIES_PER_DAY": 1, "BOGUS": 7})
    assert not st.load(p).valid
    # a set cannot write while another key is invalid...
    with pytest.raises(SettingsError, match="BOGUS"):
        st.set_setting("MAX_LOTS", 0, p)
    # ...but unset can drop the unknown key
    r = st.unset_setting("BOGUS", p)
    assert r.changed and r.old == 7 and r.new is None and not r.versioned
    assert st.load(p).valid and st.load(p).settings.MAX_ENTRIES_PER_DAY == 1
    # unset returns a key to its default
    r = st.unset_setting("MAX_ENTRIES_PER_DAY", p)
    assert (r.old, r.new) == (1, 3) and r.versioned
    assert json.loads(p.read_text(encoding="utf-8")) == {}
    assert not st.unset_setting("MAX_ENTRIES_PER_DAY", p).changed
    # unset of an absent file creates nothing
    assert not st.unset_setting("MAX_LOTS", tmp_path / "absent.json").changed
    assert not (tmp_path / "absent.json").exists()
    # a corrupt file is restored by hand, never overwritten blind
    write(p, "{oops")
    with pytest.raises(SettingsError, match="backup"):
        st.set_setting("MAX_LOTS", 0, p)
    assert p.read_text(encoding="utf-8") == "{oops"


def test_a_file_with_several_now_fixed_paper_keys_is_repaired_one_key_at_a_time(tmp_path):
    """Review round 4 (probe test_probe_unset_two_fixed_keys): paper model 2 fixed the paper calibration keys at their
    defaults, and the CHANGELOG's remedy for a file that sets one is `titan2 settings unset KEY`. With two such keys
    neither `unset` nor `set KEY <default>` could write: each validated the whole result, and removing one key left
    the other's error. A change to a file that is already invalid is now written when it removes some of its problems
    and adds none; the message lists what is left, and the bot stays on the code defaults until the last one."""
    p = write(tmp_path / "settings.json", {"PAPER_LATENCY_MEDIAN_MS": 300.0, "PAPER_LATENCY_SIGMA": 0.8,
                                           "MAX_ENTRIES_PER_DAY": 2})
    loaded = st.load(p)
    assert not loaded.valid and len(loaded.errors) == 2               # both were valid before paper model 2
    (sigma,) = [e for e in loaded.errors if e.startswith("PAPER_LATENCY_SIGMA: 0.8 is not 0.6 (fixed")]
    r = st.unset_setting("PAPER_LATENCY_MEDIAN_MS", p)
    assert r.changed and r.still_invalid == (sigma,) and not r.versioned and r.after == DEFAULTS
    assert (r.old, r.new) == (250.0, 250.0)
    assert "the file is still invalid" in r.message() and r.message().endswith(sigma)
    assert json.loads(p.read_text(encoding="utf-8")) == {"PAPER_LATENCY_SIGMA": 0.8, "MAX_ENTRIES_PER_DAY": 2}
    assert st.load(p).errors == (sigma,)
    r = st.set_setting("PAPER_LATENCY_SIGMA", "0.6", p)                 # `set KEY <default>` repairs the last one
    assert r.changed and r.still_invalid == () and "still invalid" not in r.message()
    assert r.versioned and "new version" in r.message()                  # MAX_ENTRIES_PER_DAY 2 now applies
    loaded = st.load(p)
    assert loaded.valid and loaded.settings.MAX_ENTRIES_PER_DAY == 2 and loaded.settings.PAPER_LATENCY_SIGMA == 0.6
    assert sorted(x.name for x in tmp_path.iterdir()) == ["settings.json"]
    # the same two keys alone: the last repair leaves the defaults the bot ran on meanwhile, so no new versions
    write(p, {"PAPER_LATENCY_MEDIAN_MS": 300.0, "PAPER_LATENCY_SIGMA": 0.8})
    assert st.unset_setting("PAPER_LATENCY_SIGMA", p).still_invalid[0].startswith("PAPER_LATENCY_MEDIAN_MS: 300.0")
    r = st.unset_setting("PAPER_LATENCY_MEDIAN_MS", p)
    assert r.changed and not r.versioned and "new version" not in r.message() and r.after == DEFAULTS
    assert json.loads(p.read_text(encoding="utf-8")) == {} and st.load(p).valid


def test_a_change_to_an_invalid_file_must_remove_a_problem_and_add_none(tmp_path):
    p = write(tmp_path / "settings.json", {"PAPER_LATENCY_SIGMA": 0.8, "STALE_PROTECTIVE_S": 9.0})
    before = p.read_bytes()
    for key, value in [("PAPER_LATENCY_SIGMA", "0.9"),     # another wrong value: no problem removed
                       ("MAX_LOTS", "0"),                   # another key: waits for a valid file, as before
                       ("ENTRY_START", "15:00"),            # a new problem between keys, behind the old ones
                       ("STALE_PROTECTIVE_S", "2.0")]:      # cures its bound, breaks the rule with STALE_REFRESH_S
        with pytest.raises(SettingsError) as info:
            st.set_setting(key, value, p)
        assert info.value.errors[-1].endswith("is already invalid: a change is written only when it removes one of "
                                              "its problems and adds none (`titan2 settings unset KEY` removes a key)")
        assert p.read_bytes() == before, key
    r = st.set_setting("STALE_PROTECTIVE_S", "4.0", p)
    assert r.new == 4.0 and r.still_invalid == (st.load(p).errors[0],) and st.load(p).errors[0].startswith(
        "PAPER_LATENCY_SIGMA: 0.8")
    assert json.loads(p.read_text(encoding="utf-8")) == {"PAPER_LATENCY_SIGMA": 0.8, "STALE_PROTECTIVE_S": 4.0}
    assert st.unset_setting("PAPER_LATENCY_SIGMA", p).after.STALE_PROTECTIVE_S == 4.0 and st.load(p).valid


def test_code_constants_and_semantic_hash():
    # PAPER_MODEL_VERSION 2: paper model v2 (2026-10-14, the seeded fraction of a lot and the unusable fill books);
    #   ENGINE_SEMANTICS_VERSION 2 in the same release (the pending-entry wait, the quote retry, rounds 2-3's fixes)
    assert (st.ENGINE_SEMANTICS_VERSION, st.PAPER_MODEL_VERSION, st.SCHEMA_VERSION) == (2, 2, 1)
    # exit pricing (§5.7, §7)
    assert st.BANDS == (0.0, 0.005, 0.01, 0.02, 0.04, 0.08, 0.12)
    assert list(st.BANDS) == sorted(st.BANDS) and st.BANDS[-1] == st.MAX_EXIT_DISCOUNT == 0.12
    assert st.THIN_BOOK_PENALTY == 0.02 and st.PROTECTIVE_BAND == 3 and st.PRICE_EDGE_TICKS == 2
    # order, recon, loop and limit timers (DETAIL §8.4)
    assert (st.ORDER_POLL_AFTER_S, st.ORDER_POLL_EVERY_S, st.ORDER_CANCEL_AFTER_S, st.ORDER_STUCK_AFTER_S,
            st.LOOKUP_AT_S) == (3, 1, 6, 10, (1, 3))
    assert (st.FRESH_FILL_GRACE_S, st.POSITIONS_POST_FILL_LAG_S, st.RECON_CONFIRM_GAP_S, st.POSITIONS_WAIT_MAX_S,
            st.BROKER_READS_FAIL_UNCAP_S, st.VERIFY_FLAT_RECHECK_S, st.VERIFY_FLAT_TIMEOUT_S) == (5, 2, 3, 5, 10, 2, 10)
    assert (st.QUOTE_REQUEST_TIMEOUT_S, st.REST_BATCH, st.INTENT_MAX_PER_MIN, st.TIMER_DT,
            st.HEARTBEAT_EVERY_S) == (3, 500, 30, 0.25, 60)
    assert (st.EXC_LIMIT, st.EXC_WINDOW_S, st.GATE_ERROR_LIMIT, st.GATE_ERROR_WINDOW_S,
            st.POST_EXIT_TAIL_MIN) == (20, 60, 5, 300, 45)
    # G1 (§10.7)
    assert (st.G1_MIN_TRADES, st.G1_MIN_SESSIONS, st.G1_LOOKS, st.G1_ALPHA, st.G1_FAMILY_FLOOR,
            st.G1_SPREAD_STRESS) == (100, 30, (100, 150, 200, 300), 0.05, 4, 1.5)
    assert (st.G1_PLACEBO_MIN_N, st.G1_PLACEBO_MIN_COVERAGE, st.G1_MAX_IMPAIRED_FRAC, st.G1_MAX_BAR_MISMATCH_FRAC,
            st.G1_CONFIRM_TRADES) == (50, 0.8, 0.10, 0.10, 25)
    assert (st.G1_FUTILITY_MIN_N, st.G1_FUTILITY_EVERY, st.G1_FUTILITY_ALPHA, st.G1_REPORT_MAX_AGE_DAYS,
            st.G1_BOOT_MIN) == (50, 25, 0.10, 7, 10_000)

    # the semantic hash covers exactly SEMANTIC_CONSTANTS, which are JSON-native module constants, not settings
    sc = st.SEMANTIC_CONSTANTS
    assert st.SEMANTIC_HASH == hashlib.sha256(st.canonical_json(sc).encode("utf-8")).hexdigest()
    assert st.canonical_json(sc) == json.dumps(sc, sort_keys=True, separators=(",", ":"))
    assert all(getattr(st, name) == value for name, value in sc.items())
    assert not set(sc) & set(LIMITS)
    assert sc["OPENING_QUOTE_GRACE_S"] == 30.0               # late fact: no paper fill before 09:15:30
    for name in ("BANDS", "EXIT_BOUNDS", "POSITIONS_POST_FILL_LAG_S", "G1_ALPHA", "PAPER_LATENCY_CLIP_MS"):
        assert name in sc
    # decision B1's exit-cap rule times every exit (strict: one positions round trip per attempt): changing it changes
    #   the semantic hash, so every strategy version (review of the B1 batch: it sat in recon, outside every guard;
    #   tests/arch checks that no module keeps its own copy)
    assert sc["EXIT_CAP_PRIOR_READ_S"] == st.EXIT_CAP_PRIOR_READ_S == 0.0
    # entry-gate guards (risk G03/G14/G19) and the 15:36 bar-check tolerances behind G1's bar-mismatch input
    assert (sc["MONO_SKEW_S"], sc["LAST_TRADE_FUTURE_S"]) == (1.0, 5.0)
    assert (sc["BAR_CHECK_PRICE_TOL"], sc["BAR_CHECK_VOLUME_TOL"], sc["BAR_CHECK_SAMPLE"]) == (0.001, 0.05, 20)
    for name in ("ENGINE_SEMANTICS_VERSION", "SCHEMA_VERSION", "REST_BATCH", "HEARTBEAT_EVERY_S"):
        assert name not in sc
    # canonical_json is the §10.2 form, with repr() for anything that is not JSON
    assert st.canonical_json({"b": 1, "a": [1.5, "x"]}) == '{"a":[1.5,"x"],"b":1}'
    assert st.canonical_json({"t": dtime(9, 20)}) == '{"t":"datetime.time(9, 20)"}'


def test_exit_profile_bounds():
    assert st.exit_profile_problems(ExitProfile()) == []                   # the X4-X7 defaults
    assert set(st.EXIT_BOUNDS) | {"time_stop_when_locked"} == {f.name for f in dataclasses.fields(ExitProfile)}
    assert st.exit_profile_problems(ExitProfile(lock_arm_pct=4.0, lock_floor_pct=3.5)) == []
    bad = [
        {"hard_sl_pct": 16.0}, {"hard_sl_pct": 4.9}, {"lock_arm_pct": 1.0}, {"lock_arm_pct": 10.5},
        {"lock_floor_pct": 1.5}, {"lock_floor_pct": 3.6},                     # floor <= arm - 0.5
        {"trail_retain": 0.95}, {"trail_retain": 0.4}, {"tp_pct": 7.0}, {"tp_pct": 51.0},
        {"lock_arm_pct": 10.0, "tp_pct": 9.0},                                 # tp must be above arm
        {"time_stop_min": 5.0}, {"time_stop_min": 121.0}, {"stop_confirm_ms": 1001}, {"stop_confirm_ms": -1},
        {"stop_confirm_max_ms": 400}, {"stop_confirm_max_ms": 3001}, {"stop_confirm_bypass_pct": 0.5},
        {"stop_confirm_bypass_pct": 11.0}, {"no_bid_exit_s": 2.0}, {"no_bid_exit_s": 31.0},
        {"hard_sl_pct": math.nan}, {"tp_pct": True}, {"time_stop_min": "45"}, {"time_stop_when_locked": 1},
    ]
    for kw in bad:
        assert st.exit_profile_problems(dataclasses.replace(ExitProfile(), **kw)), kw
