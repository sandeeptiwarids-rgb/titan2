# titan2

titan2 is a research bot for options on India's NSE (the F&O market), working through Zerodha's Kite Connect API.
It is **PAPER-first**: it reads live market data, but every trade it makes is simulated. No money is at risk.

It can only trade real money if all of these are true:

1. a strategy has passed **G1**, a strict statistical test run on that strategy's own paper trades;
2. it runs on a **server whose static IP address is registered with Zerodha** (SEBI's rule for API orders);
3. a person has **unlocked LIVE by hand**, in two separate steps.

The Windows laptop mode never trades real money, whatever its settings say.

> **Not financial advice.** This is research software, shared as it is, with no warranty of any kind. Options
> trading can lose money fast. The research behind titan2 found **no strategy that makes money after costs**, so the
> expected outcome is that no strategy ever reaches LIVE. If you run this code, every result, and every rupee, is
> your own responsibility.

## What is inside

| Folder or file | What it is |
|---|---|
| `titan2/` | The bot (Python 3.10 or newer). Start with [TITAN_INSTRUCTIONS.md](TITAN_INSTRUCTIONS.md) |
| `titan2/strategies/` | Three strategies, all PAPER, all unproven ideas: `opening_drive` (follow a strong first 5-minute bar), `price_spike_up` (a sharp rise that holds: buy a call) and `price_spike_down` (its mirror: buy a put) |
| `titan2/broker/paper.py` | The paper exchange. It fills paper orders from the real order book at the moment of the fill, never at the last traded price, takes at most half of the shown quantity, adds a delay like a real order's, and keeps its own ledger to check the bot's |
| `titan2/g1.py` | G1, the paper-to-live test: at least 100 trades over 30 sessions, a profit that survives paying the spread 1.5 times, a matching "placebo" trade (the opposite option) that must *not* make money, and clean books |
| `titan2/condor.py` | A paper tracker for one NIFTY weekly iron condor (`titan2 condor`). It never places an order; it checks the backtest in [docs/research/edge_research_verdict.md](docs/research/edge_research_verdict.md) against live prices |
| `titan2/whatif.py` | `titan2 whatif`: what the entries titan2 refused would have made (read-only research aid) |
| `titan2/dash/` | A read-only dashboard (it can only pause, resume or close everything) |
| `ops/windows/` | The Windows laptop kit: `setup.ps1`, `run_today.ps1`, `register_task.ps1` and the env file template |
| `ops/` | The Linux server kit: systemd units, deploy and rollback scripts, backups, a sudo allow-list |
| `tests/` | About 1,400 tests: unit, end-to-end scenarios, random-fault "chaos" runs, architecture rules, ops scripts |
| `docs/` | [LAPTOP.md](docs/LAPTOP.md) (Windows guide), [RUNBOOK.md](docs/RUNBOOK.md) (server guide), the build specification (`docs/spec/`), the research verdicts (`docs/research/`) and the archived docs of titan2's predecessor (`docs/v1/`) |

How titan2 protects you, in short (details in [TITAN_INSTRUCTIONS.md](TITAN_INSTRUCTIONS.md), sections 1 and 8):

- **One order path.** Only one file can send orders to Zerodha, and only limit orders that fill at once or are
  cancelled (IOC).
- **Money comes from real fills.** P&L is computed from fills and Zerodha's exact charges, never from the last price.
- **Exits use the price you could really sell at** (the bids in the order book), from a quote at most 3 seconds old.
- **Loss limits**, by default: 1 lot; each trade sized so that its planned stop-out costs at most ₹15,000; a daily
  stop at a ₹15,000 loss; and a ₹50,000 cap on the day's loss even if every open option went to zero. At most 2 open
  positions and 3 entries a day per strategy. Settings can only be made stricter without a code change.
- **When in doubt, stop entering and keep exiting.** Missing or stale data blocks new trades, never an exit.

## Run it on a Windows laptop (PAPER only)

You need Windows 11, a 64-bit Python 3.10 to 3.13 (3.12 is the tested one; 3.14 does not work yet), git, and a
Zerodha account with a Kite Connect app (developers.kite.trade) for its API key and secret.

```powershell
git clone https://github.com/sandeeptiwarids-rgb/titan2.git "$env:USERPROFILE\titan2"
cd "$env:USERPROFILE\titan2"
powershell -NoProfile -ExecutionPolicy Bypass -File .\ops\windows\setup.ps1
notepad "$env:APPDATA\titan2\titan2.env"
```

`setup.ps1` builds titan2's own Python environment from the hash-checked `requirements.lock`, creates the state and
log folders under `%LOCALAPPDATA%\titan2`, copies the env file template to `%APPDATA%\titan2\titan2.env` (readable
by you only) and runs the laptop self-check. Fill in the env file (see "Configuration" below), then on each trading
morning, between 08:40 and 09:10:

```powershell
cd "$env:USERPROFILE\titan2"
.\ops\windows\run_today.ps1
```

Leave the window open. To stop early, press **Ctrl+C once** and wait: titan2 closes every paper position first.
After the close it writes a daily report to `%LOCALAPPDATA%\titan2\var\reports\<date>.md`. The full guide,
including the optional dashboard and starting it by itself each weekday, is [docs/LAPTOP.md](docs/LAPTOP.md).

## Run it on a server (Linux, systemd)

The server is the only place LIVE is possible, and it still starts on PAPER. Everything is in
[docs/RUNBOOK.md](docs/RUNBOOK.md), section 8, "Cutover stages":

- `ops/bootstrap.sh` prepares the server once: the `titan` and `titandash` users, folders and permissions, time
  sync, log rotation, the `titan2` command and its sudo allow-list. It prints its plan unless given `--execute`.
- The secrets go in `/etc/titan2/titan2.env` and `/etc/titan2/dash.env`, written by hand on the server.
- `ops/deploy.sh <host>`, run from your own machine, runs the tests, builds a fixed release, checks it on the server
  and switches to it in one step, rolling back if the bot does not come up.
- Then a paper burn-in of at least 10 sessions, and months of paper trading before G1 can say anything.

Stage 0 of that section mostly retires titan2's predecessor on the same server: skip what does not apply to you,
but keep its static-IP step if you ever want LIVE.

## Configuration: env files outside the repository

Secrets never go in the repository. Each template below lists the keys with empty values; copy it to its place
outside the repository and fill it in there.

| Template | Copy it to |
|---|---|
| [ops/windows/titan2.env.example](ops/windows/titan2.env.example) | `%APPDATA%\titan2\titan2.env` on the laptop (`setup.ps1` does this) |
| [ops/titan2.env.example](ops/titan2.env.example) | `/etc/titan2/titan2.env` on the server (owner `root:titan`, mode `0640`) |
| [ops/dash.env.example](ops/dash.env.example) | `/etc/titan2/dash.env` on the server, or `%APPDATA%\titan2\dash.env` on the laptop |

| Key | What it is | Where |
|---|---|---|
| `ZERODHA_API_KEY` | Your Kite Connect app's API key | laptop and server |
| `ZERODHA_API_SECRET` | That app's API secret | laptop and server |
| `ZERODHA_USER_ID` | Your Zerodha client id | laptop and server |
| `ZERODHA_PASSWORD` | Your Kite password, for the automatic morning login (optional: leave it empty to log in by hand) | laptop and server |
| `ZERODHA_TOTP_SECRET` | The text key behind your Kite 2FA QR code, for the same automatic login (optional) | laptop and server |
| `TELEGRAM_BOT_TOKEN` | A Telegram bot's token, for alerts and the daily report (optional) | laptop and server |
| `TELEGRAM_CHAT_ID` | Your Telegram chat id (optional) | laptop and server |
| `NTFY_TOPIC` | A long random topic name for phone alerts through the ntfy app; anyone who knows it can read them (optional) | laptop and server |
| `TOKEN_MODE` | `auto` (log in with the password and TOTP) or `manual` | server |
| `ALLOW_LIVE` | LIVE key 1 of 2. Keep `0` | server |
| `REGISTERED_EGRESS_IP` | The static IP registered with Zerodha; LIVE stays off on any day the server's address differs | server |
| `HEALTHCHECK_URL` | A healthchecks.io check URL: it raises an alarm if the bot stops checking in (optional) | server |
| `BAN_LIST_URL`, `BACKUP_S3_URI` | Optional: the NSE F&O ban-list address, and an S3 folder for backups | server |
| `DASH_USER`, `DASH_PASS_SCRYPT`, `DASH_SECRET` | The dashboard login. Make the last two with `python -m titan2.dash.app hash-password` and `python -m titan2.dash.app new-secret` | `dash.env` |

The Kite access token is deliberately not in any env file: titan2 logs in each morning and keeps the session in its
own state folder. Neither is a trading mode: each strategy's mode lives in titan2's database.

## Run the tests

From the repository folder, with Python 3.10 or newer:

```
python -m pip install --require-hashes --only-binary :all: -r requirements-dev.txt
python -m pytest tests -q -p no:cacheprovider
```

The first line, once per environment, installs pytest and the exact, hash-checked versions in `requirements.lock`.
The whole suite takes about half an hour on a laptop. A few tests are skipped on purpose: the checks of the
predecessor's archive (they need the original private repository), an opt-in re-capture of reference data, and a
charges check that waits for a real contract note. On Windows the `tests/ops` scripts need Git for Windows' bash,
and skip without it. Clone titan2 into a folder of its own, not inside another git checkout: a few of those archive
checks look one folder up. CI ([.github/workflows/ci.yml](.github/workflows/ci.yml)) runs the tests only, on Ubuntu
and Windows; it has no secrets and never deploys.

## Safety rules

1. **PAPER until proven.** No strategy trades real money before G1 certifies it, and the honest expectation is that
   none will.
2. **LIVE needs two human keys** after G1, on the server only: `ALLOW_LIVE=1` with `REGISTERED_EGRESS_IP` set, and
   `titan2 live <strategy> --confirm <report hash> --i-accept-live-risk`. Every LIVE session then starts with a
   self-test order priced so that it cannot fill, which must come back cancelled
   ([RUNBOOK, P14](docs/RUNBOOK.md#p14-live-unlock-and-rollback-to-paper)).
3. **Only the bot places orders.** No script, dashboard or second tool does. If the bot is down, use the Kite app.
4. **Secrets stay outside git, chats and cloud drives.** Never commit an env file, a Kite session or token file, a
   database, a log, or a real contract note (`tests/unit/fixtures/contract_note.json` is a placeholder and must
   stay one). [.gitignore](.gitignore) blocks the usual names; do not force-add past it.
5. **One copy at a time.** Run titan2 on the laptop or on the server on a given day, never both: they share the Kite
   app, and a new login by one ends the other's session.
6. **Never loosen a limit to get more trades.** A quiet day is a correct day. Any change to a strategy gives it a new
   version and a fresh G1 test.

## A note on paths in the docs

titan2 was built inside a larger private repository, where this repository's root was the folder
`agentic_trader/`. The docs keep those paths, so read them like this:

- `agentic_trader/ops/deploy.sh`, or "from `agentic_trader/`": `ops/deploy.sh`, from this repository's root.
- `$env:USERPROFILE\MCX\agentic_trader` in [docs/LAPTOP.md](docs/LAPTOP.md): the folder you cloned titan2 into.
- Server paths such as `/home/ubuntu/titan/agentic_trader` are real folders on the server; keep them as they are.
- Titan v1, the predecessor (the `v1-final` tag, its research scripts, trade records and backups), is not part of
  this repository. [docs/v1/](docs/v1/README.md) keeps its old documentation for reference only.

## License

No license has been chosen yet. Until one is added, the usual copyright rules apply: you may read the code here,
but not reuse or redistribute it without permission.
