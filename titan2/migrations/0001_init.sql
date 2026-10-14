-- titan2.db schema version 1 (spec FINAL_SPEC.md §4.3). Everything below this header is the DDL of
-- FINAL_SPEC_DETAIL.md §4.3, copied verbatim; that DDL is normative.
-- titan2/store.py applies this file once, inside one BEGIN IMMEDIATE transaction that also sets
-- PRAGMA user_version = 1. Never edit an applied migration: schema changes go in a new numbered file
-- (phase 2: 0002_multileg.sql). Migration files must not contain BEGIN/COMMIT of their own.

CREATE TABLE meta(key TEXT PRIMARY KEY, value TEXT NOT NULL);               -- schema_version, install_id, last_session
CREATE TABLE seq(name TEXT PRIMARY KEY, value INTEGER NOT NULL);            -- "leg:<ACCOUNT>:<yymmdd>"

CREATE TABLE events(
  seq INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT NOT NULL, mono REAL NOT NULL, session TEXT NOT NULL, kind TEXT NOT NULL,
  severity TEXT NOT NULL DEFAULT 'INFO' CHECK (severity IN ('DEBUG','INFO','WARN','CRITICAL')),
  account TEXT, book TEXT, strategy TEXT, structure_id INTEGER, leg_id INTEGER, order_pk TEXT, intent_id TEXT,
  payload TEXT NOT NULL CHECK (json_valid(payload)));
CREATE INDEX ev_session_kind ON events(session, kind);
CREATE INDEX ev_structure ON events(structure_id);
CREATE TRIGGER ev_no_upd BEFORE UPDATE ON events BEGIN SELECT RAISE(ABORT,'events append-only'); END;
CREATE TRIGGER ev_no_del BEFORE DELETE ON events BEGIN SELECT RAISE(ABORT,'events append-only'); END;

CREATE TABLE intents(
  intent_id TEXT PRIMARY KEY, session TEXT NOT NULL, strategy TEXT NOT NULL, strategy_version TEXT NOT NULL,
  underlying TEXT NOT NULL, direction TEXT NOT NULL CHECK (direction IN ('UP','DOWN')), structure TEXT NOT NULL,
  strike_pref TEXT NOT NULL CHECK (strike_pref IN ('ATM','ITM1')), expiry_rule TEXT NOT NULL,
  trigger_px REAL NOT NULL CHECK (trigger_px > 0), recheck_lo REAL NOT NULL, recheck_hi REAL NOT NULL,
  created_ts TEXT NOT NULL, ttl_s REAL NOT NULL CHECK (ttl_s > 0 AND ttl_s <= 10),
  reason TEXT NOT NULL CHECK (json_valid(reason)), is_placebo INTEGER NOT NULL DEFAULT 0 CHECK (is_placebo IN (0,1)),
  parent_intent_id TEXT,
  CHECK ((direction='UP' AND structure='LONG_CE') OR (direction='DOWN' AND structure='LONG_PE')));   -- widened in phase 2

CREATE TABLE intent_routes(
  intent_id TEXT NOT NULL REFERENCES intents(intent_id), book TEXT NOT NULL,
  outcome TEXT NOT NULL CHECK (outcome IN ('RESERVED','REFUSED','DIVERTED','EXPIRED')),
  gate TEXT, detail TEXT, structure_id INTEGER, decided_ts TEXT NOT NULL, PRIMARY KEY (intent_id, book));

CREATE TABLE structures(
  structure_id INTEGER PRIMARY KEY, session TEXT NOT NULL, book TEXT NOT NULL,
  account TEXT NOT NULL CHECK (account IN ('LIVE','PAPER','SHADOW')), policy TEXT NOT NULL CHECK (policy IN ('FULL','QUALITY')),
  strategy TEXT NOT NULL, strategy_version TEXT NOT NULL, intent_id TEXT NOT NULL REFERENCES intents(intent_id),
  type TEXT NOT NULL CHECK (type IN ('LONG_CE','LONG_PE')), underlying TEXT NOT NULL,
  state TEXT NOT NULL CHECK (state IN ('RESERVED','ENTERING','OPEN','EXITING','VERIFY_FLAT','CLOSED','CLOSED_EXTERNAL','VOID')),
  is_placebo INTEGER NOT NULL DEFAULT 0, parent_structure_id INTEGER REFERENCES structures(structure_id), blocked_by TEXT,
  decision_json TEXT CHECK (decision_json IS NULL OR json_valid(decision_json)), placebo_status TEXT,
  exit_params TEXT CHECK (exit_params IS NULL OR json_valid(exit_params)), risk_basis_rs REAL,
  exit_reason TEXT, void_reason TEXT, reserved_ts TEXT NOT NULL, opened_ts TEXT, closed_ts TEXT,
  pnl_status TEXT CHECK (pnl_status IS NULL OR pnl_status IN ('FINAL','ESTIMATED','UNRESOLVED')));
CREATE UNIQUE INDEX one_active_per_book_underlying ON structures(book, underlying)
  WHERE state NOT IN ('CLOSED','CLOSED_EXTERNAL','VOID');                               -- I5 (RESERVED counts)
CREATE UNIQUE INDEX one_active_live_underlying ON structures(account, underlying)
  WHERE account='LIVE' AND state NOT IN ('CLOSED','CLOSED_EXTERNAL','VOID');
CREATE INDEX st_session_book ON structures(session, book);
CREATE TRIGGER st_terminal BEFORE UPDATE OF state ON structures WHEN OLD.state IN ('CLOSED','CLOSED_EXTERNAL','VOID')
  BEGIN SELECT RAISE(ABORT,'terminal structure immutable'); END;

CREATE TABLE legs(
  leg_id INTEGER PRIMARY KEY, structure_id INTEGER NOT NULL REFERENCES structures(structure_id), leg_no INTEGER NOT NULL,
  session TEXT NOT NULL, account TEXT NOT NULL, book TEXT NOT NULL, seq_no INTEGER NOT NULL,
  symbol TEXT NOT NULL, tradingsymbol TEXT NOT NULL, exchange TEXT NOT NULL, token INTEGER NOT NULL, underlying TEXT NOT NULL,
  side TEXT NOT NULL CHECK (side IN ('LONG')), opt_type TEXT NOT NULL CHECK (opt_type IN ('CE','PE')),
  strike REAL NOT NULL CHECK (strike > 0), expiry TEXT NOT NULL, lot_size INTEGER NOT NULL CHECK (lot_size > 0),
  tick REAL NOT NULL CHECK (tick > 0), product TEXT NOT NULL DEFAULT 'MIS' CHECK (product='MIS'),
  state TEXT NOT NULL CHECK (state IN ('PLANNED','ENTERING','OPEN','EXITING','VERIFY_FLAT','CLOSED','CLOSED_EXTERNAL','VOID')),
  stop_px REAL, lock_armed INTEGER NOT NULL DEFAULT 0, peak_rb REAL, peak_ts TEXT, trough_rb REAL, trough_ts TEXT,
  entry_fill_ts TEXT, last_fill_ts TEXT, exit_reason TEXT, exit_urgency TEXT, exit_band_idx INTEGER NOT NULL DEFAULT 0,
  exiting_since_ts TEXT, broker_residual_qty INTEGER NOT NULL DEFAULT 0,
  UNIQUE (structure_id, leg_no), UNIQUE (account, session, seq_no));

CREATE TABLE orders(
  order_pk TEXT PRIMARY KEY, leg_id INTEGER NOT NULL REFERENCES legs(leg_id), structure_id INTEGER NOT NULL,
  account TEXT NOT NULL, book TEXT NOT NULL,
  role TEXT NOT NULL CHECK (role IN ('ENTRY','EXIT','COVER','SELFTEST','EXTERNAL')),
  side TEXT NOT NULL CHECK (side IN ('BUY','SELL')), symbol TEXT NOT NULL,
  qty INTEGER NOT NULL CHECK (qty > 0), limit_px REAL CHECK (limit_px IS NULL OR limit_px > 0),
  order_type TEXT NOT NULL, validity TEXT NOT NULL, product TEXT NOT NULL, variety TEXT NOT NULL,
  attempt INTEGER NOT NULL, band_idx INTEGER,
  state TEXT NOT NULL CHECK (state IN ('NEW','SUBMITTING','UNKNOWN','WORKING','CANCEL_REQUESTED','COMPLETE','CANCELLED','REJECTED','NOT_PLACED')),
  broker_order_id TEXT UNIQUE, broker_status TEXT, status_msg TEXT, external_kind TEXT,
  filled_qty INTEGER NOT NULL DEFAULT 0, avg_fill_px REAL,
  decision_bid REAL, decision_ask REAL, decision_mid REAL, decision_quote_age_ms INTEGER, decision_quote_src TEXT,
  decision_ltp REAL, decision_walk_px REAL,
  created_ts TEXT NOT NULL, submitted_ts TEXT, acked_ts TEXT, terminal_ts TEXT, stuck INTEGER NOT NULL DEFAULT 0,
  CHECK (filled_qty >= 0 AND filled_qty <= qty),
  CHECK (role='EXTERNAL' OR (order_type='LIMIT' AND validity='IOC' AND product='MIS' AND variety='regular'
         AND limit_px IS NOT NULL AND length(order_pk)=15 AND substr(order_pk,1,2)='T2')),
  CHECK ((role='ENTRY' AND side='BUY') OR (role='EXIT' AND side='SELL') OR (role='COVER' AND side='BUY')
         OR (role='SELFTEST' AND side='BUY') OR role='EXTERNAL'));                        -- I7, widened in phase 2
CREATE UNIQUE INDEX one_working_order_per_leg ON orders(leg_id)
  WHERE role <> 'EXTERNAL' AND state NOT IN ('COMPLETE','CANCELLED','REJECTED','NOT_PLACED');   -- I4
CREATE TRIGGER ord_fill_mono BEFORE UPDATE OF filled_qty ON orders WHEN NEW.filled_qty < OLD.filled_qty
  BEGIN SELECT RAISE(ABORT,'filled_qty decreased'); END;
CREATE TRIGGER ord_terminal BEFORE UPDATE ON orders
  WHEN OLD.state IN ('COMPLETE','CANCELLED','REJECTED','NOT_PLACED')
   AND (NEW.state IS NOT OLD.state OR NEW.filled_qty IS NOT OLD.filled_qty OR NEW.avg_fill_px IS NOT OLD.avg_fill_px)
  BEGIN SELECT RAISE(ABORT,'terminal order immutable'); END;
-- EXTERNAL orders (closing trades not placed by the bot) use order_pk = 'EX' || broker_order_id.

CREATE TABLE fills(
  fill_id INTEGER PRIMARY KEY, order_pk TEXT NOT NULL REFERENCES orders(order_pk), seq_key TEXT NOT NULL,
  source TEXT NOT NULL CHECK (source IN ('CUM','TRADE')), qty INTEGER NOT NULL CHECK (qty > 0),
  px REAL NOT NULL CHECK (px > 0), ts TEXT NOT NULL, exch_ts TEXT, UNIQUE (order_pk, seq_key));
CREATE TRIGGER fills_no_upd BEFORE UPDATE ON fills BEGIN SELECT RAISE(ABORT,'fills append-only'); END;
CREATE TRIGGER fills_no_del BEFORE DELETE ON fills BEGIN SELECT RAISE(ABORT,'fills append-only'); END;
CREATE VIEW effective_fills AS                          -- TRADE rows replace CUM rows once they explain filled_qty
  SELECT f.* FROM fills f JOIN orders o ON o.order_pk = f.order_pk
  WHERE f.source = CASE WHEN o.filled_qty > 0 AND
        (SELECT COALESCE(SUM(t.qty),0) FROM fills t WHERE t.order_pk=f.order_pk AND t.source='TRADE') = o.filled_qty
        THEN 'TRADE' ELSE 'CUM' END;

CREATE TABLE trades(                                    -- the ledger: one immutable row per closed structure
  structure_id INTEGER PRIMARY KEY REFERENCES structures(structure_id),
  session TEXT NOT NULL, book TEXT NOT NULL, account TEXT NOT NULL, mode TEXT NOT NULL, strategy TEXT NOT NULL,
  strategy_version TEXT NOT NULL, intent_id TEXT NOT NULL, is_placebo INTEGER NOT NULL, parent_structure_id INTEGER, blocked_by TEXT,
  underlying TEXT NOT NULL, symbol TEXT NOT NULL, structure_type TEXT NOT NULL, direction TEXT NOT NULL,
  opt_type TEXT, strike REAL, expiry TEXT, lot_size INTEGER, qty INTEGER NOT NULL,
  entry_ts TEXT NOT NULL, exit_ts TEXT NOT NULL, hold_s REAL NOT NULL, entry_avg REAL NOT NULL, exit_avg REAL,
  risk_basis_rs REAL NOT NULL CHECK (risk_basis_rs > 0),
  gross REAL NOT NULL, brokerage REAL NOT NULL, stt REAL NOT NULL, txn REAL NOT NULL, sebi REAL NOT NULL, ipft REAL NOT NULL,
  gst REAL NOT NULL, stamp REAL NOT NULL, sqoff_fee REAL NOT NULL, charges REAL NOT NULL, net REAL NOT NULL, net_pct REAL NOT NULL,
  rates_version TEXT NOT NULL, n_entry_orders INTEGER NOT NULL, n_exit_orders INTEGER NOT NULL, n_executed_orders INTEGER NOT NULL,
  entry_bid REAL, entry_ask REAL, entry_mid REAL, entry_spread_pct REAL, entry_slip_vs_mid_pct REAL, entry_walk_px REAL,
  exit_mid_w REAL, exit_spread_pct_w REAL, exit_slip_vs_mid_pct REAL,
  hs_cost_rs REAL NOT NULL, crossing_cost_rs REAL NOT NULL,
  mfe_pct REAL, mfe_ts TEXT, mae_pct REAL, mae_ts TEXT, lock_armed INTEGER NOT NULL,
  exit_reason TEXT NOT NULL, pnl_status TEXT NOT NULL CHECK (pnl_status IN ('FINAL','ESTIMATED','UNRESOLVED')),
  ltp_entry REAL, ltp_exit REAL, thin_book INTEGER NOT NULL DEFAULT 0, exit_profile TEXT NOT NULL CHECK (json_valid(exit_profile)));
CREATE INDEX tr_book_session ON trades(book, session);
CREATE INDEX tr_strategy_version ON trades(strategy, strategy_version);
CREATE TRIGGER tr_no_upd BEFORE UPDATE ON trades BEGIN SELECT RAISE(ABORT,'ledger immutable'); END;
CREATE TRIGGER tr_no_del BEFORE DELETE ON trades BEGIN SELECT RAISE(ABORT,'ledger immutable'); END;

CREATE TABLE quote_checks(id INTEGER PRIMARY KEY, leg_id INTEGER NOT NULL, ts TEXT NOT NULL,
  phase TEXT NOT NULL CHECK (phase IN ('HOLD','POST')), bid REAL, ask REAL, bid_qty INTEGER, ask_qty INTEGER, ltp REAL,
  rb REAL, age_ms INTEGER, src TEXT, stop_px REAL, lock_armed INTEGER, decision TEXT);
CREATE INDEX qc_leg_ts ON quote_checks(leg_id, ts);

CREATE TABLE halts(halt_id INTEGER PRIMARY KEY, scope TEXT NOT NULL, kind TEXT NOT NULL, reason TEXT NOT NULL,
  operator INTEGER NOT NULL DEFAULT 0, clears TEXT NOT NULL CHECK (clears IN ('NEXT_SESSION','ACK','AUTO','RESUME')),
  created_ts TEXT NOT NULL, session TEXT NOT NULL, cleared_ts TEXT, cleared_by TEXT, clear_note TEXT);
CREATE UNIQUE INDEX one_active_halt ON halts(scope, kind) WHERE cleared_ts IS NULL;

CREATE TABLE strategy_modes(strategy TEXT PRIMARY KEY, version TEXT NOT NULL,
  mode TEXT NOT NULL CHECK (mode IN ('OFF','SHADOW','PAPER','LIVE')),
  pending_mode TEXT CHECK (pending_mode IS NULL OR pending_mode IN ('OFF','SHADOW','PAPER','LIVE')),
  live_unlock_report_id INTEGER, live_since_session TEXT, demoted_reason TEXT, changed_ts TEXT NOT NULL, changed_by TEXT NOT NULL);
CREATE TABLE g1_trials(strategy TEXT NOT NULL, version TEXT NOT NULL, family_seq INTEGER NOT NULL UNIQUE, registered_ts TEXT NOT NULL,
  state TEXT NOT NULL CHECK (state IN ('COLLECTING','CONFIRMING','CERTIFIED','FAILED','FUTILE','RETIRED','DEMOTED')),
  version_inputs TEXT NOT NULL CHECK (json_valid(version_inputs)), looks_done TEXT NOT NULL DEFAULT '[]',
  pass_report_id INTEGER, cert_report_id INTEGER, state_ts TEXT NOT NULL, PRIMARY KEY (strategy, version));
CREATE TRIGGER trials_no_del BEFORE DELETE ON g1_trials BEGIN SELECT RAISE(ABORT,'trial registry append-only'); END;
CREATE TABLE g1_reports(report_id INTEGER PRIMARY KEY, strategy TEXT NOT NULL, version TEXT NOT NULL,
  kind TEXT NOT NULL CHECK (kind IN ('PROGRESS','LOOK','CONFIRM','FUTILITY','LIVE_REVIEW')), look_n INTEGER, computed_ts TEXT NOT NULL,
  n INTEGER, sessions INTEGER, exp_pct REAL, exp_pct_stressed REAL, lb_pct REAL, ub_pct REAL, alpha REAL, family_m INTEGER,
  placebo_n INTEGER, placebo_coverage REAL, placebo_exp_pct REAL, mismatches INTEGER, unresolved INTEGER,
  impaired_frac REAL, bar_mismatch_frac REAL, passed INTEGER NOT NULL, fail_reasons TEXT, inputs_hash TEXT NOT NULL,
  detail TEXT NOT NULL CHECK (json_valid(detail)));
CREATE TABLE bars_5m(symbol TEXT NOT NULL, start_ts TEXT NOT NULL, source TEXT NOT NULL CHECK (source IN ('LIVE','HIST')),
  open REAL, high REAL, low REAL, close REAL, volume INTEGER, valid INTEGER NOT NULL, PRIMARY KEY (symbol, start_ts, source));
CREATE TABLE bar_checks(session TEXT NOT NULL, symbol TEXT NOT NULL, start_ts TEXT NOT NULL, ok INTEGER NOT NULL,
  detail TEXT NOT NULL, PRIMARY KEY (session, symbol, start_ts));
CREATE TABLE broker_snapshots(account TEXT NOT NULL, kind TEXT NOT NULL CHECK (kind IN ('POSITIONS','ORDERS','TRADES','MARGINS')),
  req_ts TEXT NOT NULL, ok INTEGER NOT NULL, payload TEXT CHECK (payload IS NULL OR json_valid(payload)),
  PRIMARY KEY (account, kind, req_ts));        -- ≤1/min per (account, kind) + every non-OK + every recon finding
CREATE TABLE foreign_acks(session TEXT NOT NULL, account TEXT NOT NULL, symbol TEXT NOT NULL, product TEXT NOT NULL,
  qty INTEGER NOT NULL, acked_by TEXT NOT NULL, acked_ts TEXT NOT NULL, PRIMARY KEY (session, account, symbol, product));
CREATE TABLE command_acks(source TEXT NOT NULL CHECK (source IN ('DASH','CLI')), cmd_id INTEGER NOT NULL, kind TEXT NOT NULL,
  status TEXT NOT NULL CHECK (status IN ('ACCEPTED','DONE','REJECTED','FAILED','EXPIRED')), result TEXT,
  picked_ts TEXT NOT NULL, done_ts TEXT, PRIMARY KEY (source, cmd_id));
CREATE TABLE session_flags(session TEXT NOT NULL, strategy TEXT NOT NULL, flag TEXT NOT NULL CHECK (flag IN ('IMPAIRED')),
  detail TEXT NOT NULL, PRIMARY KEY (session, strategy, flag));
