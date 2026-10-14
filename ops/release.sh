#!/usr/bin/env bash
# titan2 server-side release management (DETAIL §13.6). ops/deploy.sh, ops/rollback.sh and ops/pull_drift.sh send
# this file over ssh (`ssh <host> bash -s -- <command> ...`); it runs as user ubuntu and uses sudo only for the
# commands listed in ops/sudoers.titan2. On the server it can also be run directly:
#   bash /home/ubuntu/titan/current/ops/release.sh <command> ...
#
# Commands
#   preflight [--allow-market-hours]      read-only: the market-hours rule, drift of current, >= 1 GB free
#   install REL SHA256 [--allow-market-hours] [--restart]
#                                         steps 3-11: unpack incoming/release-REL.tgz, venv, freeze, deploy
#                                         self-check, atomic switch, units, restarts, rollback on failure, prune
#   rollback [--to REL] [--allow-market-hours] [--restart]
#                                         flip current to the previous release (or REL); same market-hours rule
#   drift [REL]                           drift of current (or REL): M modified, D missing, A beyond the manifest
#   drift-tar [REL]                       a tar stream of those M and A files (ops/pull_drift.sh)
#   assert-owned PATH...                  "owned: P" or "REFUSED: P" per path; exit 0 only if deploy may write all
#
# Exit codes: 0 ok; 1 failed after the switch (rolled back and a CRITICAL alert sent), or drift found;
# 2 refused or failed before anything changed.
#
# Market-hours rule (never looser than DETAIL §13.6 2a): while titan2-bot is running, `titan2 status --json` must
# show 0 open legs and 0 non-terminal orders in titan2.db, and between 08:40 and 15:45 IST the deploy also needs
# --allow-market-hours. When the bot is not running there is nothing for a deploy to disturb. An install checks the
# rule three times: at the start, just before the switch and just before it restarts titan2-bot, because the venv
# build, the backup and the self-check take minutes, during which a strategy may enter or the bot may start. A
# rollback checks it at the start and just before the restart.
#
# Guarantee G12: every write goes through an owned_* function, and each of those calls assert_owned() first: a
# realpath prefix check that allows only <root>/{incoming,releases,venvs}/..., the <root>/current and current.new
# symlinks and, through sudo, <unit dir>/titan2-*. /var/lib/titan2, /etc/titan2, <root>/agentic_trader and
# <root>/v1_frozen_* are refused even if the root is misconfigured. Nothing here uses rsync or copies an env file.
set -euo pipefail
shopt -s nullglob
umask 022

# Paths and timings. The TITAN2_* overrides exist for tests/ops; production sets none of them.
ROOT=${TITAN2_ROOT:-/home/ubuntu/titan}
UNIT_DIR=${TITAN2_UNIT_DIR:-/etc/systemd/system}
STATE_DIR=${TITAN2_VAR:-/var/lib/titan2}
SECRETS_DIR=${TITAN2_ETC:-/etc/titan2}
READY_TIMEOUT_S=${TITAN2_READY_TIMEOUT_S:-120}
POLL_S=${TITAN2_POLL_S:-2}
DASH_SETTLE_S=${TITAN2_DASH_SETTLE_S:-3}

BOT=titan2-bot
DASH=titan2-dash
MIN_FREE_KB=1048576          # 1 GB (§13.6 2c)
KEEP_RELEASES=5              # newest releases kept by prune, plus current and the previous release (§13.6 11)
HEARTBEAT_MAX_S=10
MH_START=0840                # market-hours window of the deploy rule, IST
MH_END=1545

log() { printf '[titan2 release] %s\n' "$*"; }
warn() { printf '[titan2 release] WARNING: %s\n' "$*" >&2; }
fail() { FAILED_WHY=$*; printf '[titan2 release] FAILED: %s\n' "$*" >&2; exit 2; }   # the rollback alert quotes it

for _v in TITAN2_ROOT TITAN2_UNIT_DIR TITAN2_VAR TITAN2_ETC TITAN2_OPS_NOW TITAN2_READY_TIMEOUT_S TITAN2_POLL_S \
          TITAN2_DASH_SETTLE_S; do
    if [ -n "${!_v:-}" ]; then warn "override in effect: $_v=${!_v}"; fi
done
if [ -n "${TITAN2_OPS_NOW:-}" ] && [ -z "${TITAN2_ROOT:-}" ]; then   # a fake clock would bypass the market-hours rule
    fail "TITAN2_OPS_NOW is a test clock: it is refused unless TITAN2_ROOT points at a test layout"
fi

ROOT=$(realpath -m -- "$ROOT")
UNIT_DIR=$(realpath -m -- "$UNIT_DIR")
mapfile -t DENY < <(realpath -m -- /var/lib/titan2 /etc/titan2 "$STATE_DIR" "$SECRETS_DIR" "$ROOT/agentic_trader")

CMD="" REL="" ROLLBACK_TO="" CHANGED=0 BOT_RESTARTED=0 LOCK="" PYBIN=python3 REFUSAL="" FAILED_WHY=""

# ---------------------------------------------------------------------------------------------------------------
# Ownership: the only paths a deploy may write
# ---------------------------------------------------------------------------------------------------------------
_owned() {   # a normalized absolute path
    local q=$1 d
    for d in "${DENY[@]}"; do
        case $q in "$d" | "$d"/*) return 1 ;; esac
    done
    case $q in
        "$ROOT"/v1_frozen_*) return 1 ;;
        "$ROOT"/incoming/* | "$ROOT"/releases/* | "$ROOT"/venvs/*) return 0 ;;
        "$ROOT"/current | "$ROOT"/current.new) return 0 ;;
        "$UNIT_DIR"/titan2-*) return 0 ;;
    esac
    return 1
}

assert_owned() {   # both the path itself (a symlink names the link) and what it resolves to must be owned
    local p r
    for p in "$@"; do
        case $p in /*) ;; *) fail "assert_owned: '$p' is not an absolute path" ;; esac
        case $p in */) fail "assert_owned: '$p' ends in a slash" ;; esac
        mapfile -t r < <(realpath -m -- "${p%/*}" "$p")
        if [ "${#r[@]}" -ne 2 ] || ! _owned "${r[0]}/${p##*/}" || ! _owned "${r[1]}"; then
            fail "assert_owned: refusing to write $p (resolves to ${r[1]:-?}): not a deploy-owned path"
        fi
    done
}

owned_mkdir() { assert_owned "$@"; mkdir -p -- "$@"; }
owned_mv() { assert_owned "$1" "$2"; mv -T -- "$1" "$2"; }
owned_append() { assert_owned "$1"; printf '%s\n' "$2" >> "$1"; }
owned_untar() { assert_owned "$2"; tar -xzf "$1" -C "$2" --no-same-owner; }
owned_freeze() { assert_owned "$1"; chmod -R a-w -- "$1"; }
owned_compile() { assert_owned "$1"; "$2" -m compileall -q "$1"; }
owned_install_unit() { assert_owned "$2"; sudo -n install -m 0644 "$1" "$2"; }

owned_rm() {
    local p
    for p in "$@"; do
        assert_owned "$p"
        if [ -L "$p" ]; then
            rm -f -- "$p"
        elif [ -e "$p" ]; then
            chmod -R u+w -- "$p"
            rm -rf -- "$p"
        fi
    done
}

owned_link() {   # owned_link TARGET LINK: point LINK at TARGET atomically (ln -sfn LINK.new, then mv -T over LINK)
    assert_owned "$2" "$2.new"
    ln -sfn -- "$1" "$2.new"
    mv -T -- "$2.new" "$2"
}

owned_venv() {   # owned_venv DIR LOCKFILE: build the venv from the hash-locked requirements, wheels only
    assert_owned "$1"
    "$PYBIN" -m venv "$1"
    "$1/bin/python" -m pip install --no-input --disable-pip-version-check --no-cache-dir --require-hashes \
        --only-binary=:all: -r "$2"
    "$1/bin/python" -m pip check
    : > "$1/.complete"
}

owned_lock() {   # one deploy or rollback at a time
    [ -d "$ROOT/incoming" ] && [ -d "$ROOT/releases" ] && [ -d "$ROOT/venvs" ] ||
        fail "$ROOT/{incoming,releases,venvs} are missing: run ops/bootstrap.sh first"
    assert_owned "$ROOT/incoming/.lock"
    mkdir -- "$ROOT/incoming/.lock" 2>/dev/null ||
        fail "another deploy or rollback holds $ROOT/incoming/.lock (remove that directory if none is running)"
    LOCK="$ROOT/incoming/.lock"
}

owned_unlock() {
    if [ -n "$LOCK" ]; then
        assert_owned "$LOCK"
        rmdir -- "$LOCK" 2>/dev/null || true
        LOCK=""
    fi
}

# ---------------------------------------------------------------------------------------------------------------
# Small helpers (read-only)
# ---------------------------------------------------------------------------------------------------------------
sha256_of() { local h; h=$(sha256sum -- "$1"); printf '%s\n' "${h:0:64}"; }

ist_now() {   # "YYYY-MM-DD HHMM" in IST (UTC+05:30, no DST), whatever the host's TZ
    if [ -n "${TITAN2_OPS_NOW:-}" ]; then
        printf '%s\n' "$TITAN2_OPS_NOW"
    else
        date -u -d "@$(( $(date -u +%s) + 19800 ))" '+%F %H%M'
    fi
}

in_market_window() {
    local hm
    hm=$(ist_now)
    hm=${hm#* }
    (( 10#$hm >= 10#$MH_START && 10#$hm < 10#$MH_END ))
}

unit_state() { systemctl is-active "$1" 2>/dev/null || true; }
unit_prop() { systemctl show -p "$2" --value "$1" 2>/dev/null || true; }

is_running_state() {
    case $1 in active | activating | deactivating | reloading | refreshing) return 0 ;; esac
    return 1
}

unit_masked() {   # the operator stopped the unit on purpose: a deploy neither starts nor unmasks it
    case $(systemctl is-enabled "$1" 2>/dev/null || true) in masked | masked-runtime) return 0 ;; esac
    return 1
}

current_release() {   # the release current points at, or nothing
    local t
    if [ -L "$ROOT/current" ]; then
        t=$(readlink -- "$ROOT/current")
        printf '%s\n' "${t##*/}"
    elif [ -e "$ROOT/current" ]; then
        fail "$ROOT/current exists but is not a symlink"
    fi
}

previous_release() {   # the release current replaced at its last switch (an automatic rollback is not a switch)
    local cur
    cur=$(current_release)
    if [ -z "$cur" ] || [ ! -f "$ROOT/releases/HISTORY" ]; then return 0; fi
    awk -v cur="$cur" '$2 != "autorollback" && $4 == cur { prev = $3 } END { if (prev != "" && prev != "-") print prev }' \
        "$ROOT/releases/HISTORY"
}

history_append() {   # ACTION FROM TO
    owned_append "$ROOT/releases/HISTORY" "$(date -u +%FT%TZ) $1 ${2:--} $3"
}

alert_critical() {
    log "CRITICAL alert: $1"
    titan2 alert --critical "$1" || warn "could not send the CRITICAL alert"
}

# The JSON of `titan2 status --json` (titan2/__main__.py status_info) is read with the system python3.
status_counts() {   # "OPEN_LEGS NONTERMINAL_ORDERS" from titan2.db, or fail when the CLI cannot tell
    local js
    js=$(titan2 status --json 2>/dev/null) || return 1
    printf '%s' "$js" | python3 -c '
import json, sys
db = (json.load(sys.stdin) or {}).get("db") or {}
legs, orders = db.get("open_legs"), db.get("nonterminal_orders")
if db.get("ok") is not True or type(legs) is not int or type(orders) is not int:
    sys.exit(1)
print(legs, orders)'
}

db_exists() {   # yes | no | ? : does the live titan2.db exist (the CLI reads it as titan; ubuntu cannot)
    local js
    js=$(titan2 status --json 2>/dev/null) || { echo "?"; return 0; }
    printf '%s' "$js" | python3 -c '
import json, sys
db = (json.load(sys.stdin) or {}).get("db") or {}
print("?" if db.get("ok") is not True else "yes" if db.get("exists") is True else "no" if db.get("exists") is False else "?")'
}

heartbeat_fresh() {   # T0: the bot published a status after T0 whose core heartbeat is < HEARTBEAT_MAX_S old
    local js
    js=$(titan2 status --json 2>/dev/null) || true
    printf '%s' "$js" | python3 -c '
import datetime, json, sys, time
try:
    st = json.loads(sys.stdin.read() or "{}").get("status") or {}
    ts = datetime.datetime.fromisoformat(str(st["ts"]).replace("Z", "+00:00")).timestamp()
    hb = float(st["heartbeat_age_s"])
except (KeyError, TypeError, ValueError, AttributeError):
    sys.exit(1)
t0, limit = float(sys.argv[1]), float(sys.argv[2])
sys.exit(0 if ts >= t0 - 1 and time.time() - ts < limit and hb < limit else 1)' "$1" "$HEARTBEAT_MAX_S"
}

drift_list() {   # DIR: "M path" (content differs), "D path" (missing), "A path" (beyond the manifest), sorted
    local dir=$1
    if [ ! -f "$dir/MANIFEST.sha256" ]; then
        echo "D MANIFEST.sha256"
        return 0
    fi
    {
        (cd -- "$dir" && find . -type f ! -path ./MANIFEST.sha256 -print0 | xargs -0 -r sha256sum --) |
            awk -v manifest="$dir/MANIFEST.sha256" '
                BEGIN { while ((getline line < manifest) > 0) want[substr(line, 67)] = substr(line, 1, 64) }
                {
                    h = substr($0, 1, 64); p = substr($0, 67); sub(/^\.\//, "", p)
                    if (p in want) { if (want[p] != h) print "M " p; seen[p] = 1 }
                    else if (p !~ /^titan2\/(.*\/)?__pycache__\/[^\/]+\.pyc$/) print "A " p
                }
                END { for (p in want) if (!(p in seen)) print "D " p }'
        (cd -- "$dir" && find . -type l ! -path ./venv | sed 's|^\./|A |')
    } | LC_ALL=C sort -k2
}

# ---------------------------------------------------------------------------------------------------------------
# Guards (read-only; each guard_* refuses with exit 2)
# ---------------------------------------------------------------------------------------------------------------
market_hours_ok() {   # $1 = 1 with --allow-market-hours, $2 = when (for the log). 0 when the rule lets a deploy
    local state counts legs orders    # disturb titan2-bot now; else 1 with the reason in REFUSAL
    REFUSAL=""
    state=$(unit_state "$BOT")
    if ! is_running_state "$state"; then
        log "market-hours rule$2: $BOT is ${state:-unknown}: ok"
        return 0
    fi
    if ! counts=$(status_counts); then
        REFUSAL="$BOT is $state and \`titan2 status --json\` gave no titan2.db counts: cannot prove it is flat (retry, or stop the bot first)"
        return 1
    fi
    read -r legs orders <<<"$counts"
    if (( legs > 0 || orders > 0 )); then
        REFUSAL="$BOT is $state with $legs open leg(s) and $orders non-terminal order(s): deploy when it is flat"
        return 1
    fi
    if in_market_window; then
        if [ "$1" != 1 ]; then
            REFUSAL="$BOT is $state during market hours ($(ist_now) IST, window ${MH_START}-${MH_END}): pass --allow-market-hours to proceed while it is flat"
            return 1
        fi
        log "market-hours rule$2: market hours, --allow-market-hours, and $BOT is flat (0 open legs, 0 working orders): ok"
    else
        log "market-hours rule$2: $BOT is $state and flat, outside ${MH_START}-${MH_END} IST: ok"
    fi
}

guard_market_hours() {   # $1 = 1 with --allow-market-hours, [$2 = when]
    market_hours_ok "$1" "${2:-}" || fail "$REFUSAL"
}

guard_drift() {
    local cur out
    cur=$(current_release)
    if [ -z "$cur" ]; then
        log "drift check: no current release yet"
        return 0
    fi
    out=$(drift_list "$ROOT/releases/$cur")
    if [ -n "$out" ]; then
        printf '%s\n' "$out" >&2
        fail "current ($cur) has drifted from its MANIFEST (above: M modified, D missing, A extra). These are edits made on the server: copy them with ops/pull_drift.sh <host>, commit what you keep, then deploy"
    fi
    log "drift check: current ($cur) matches its MANIFEST"
}

guard_disk() {
    local kb
    kb=$(df -Pk -- "$ROOT" | awk 'NR == 2 { print $4 }')
    [[ $kb =~ ^[0-9]+$ ]] || fail "cannot read the free disk space under $ROOT"
    (( kb >= MIN_FREE_KB )) || fail "only $(( kb / 1024 )) MB free under $ROOT: need at least 1 GB"
    log "disk: $(( kb / 1024 )) MB free"
}

# ---------------------------------------------------------------------------------------------------------------
# Install steps
# ---------------------------------------------------------------------------------------------------------------
check_members() {   # the tarball holds only regular files and directories, with no absolute or '..' names
    local bad
    bad=$(tar -tvzf "$1" | awk '{ t = substr($1, 1, 1); n = $NF }
        (t != "-" && t != "d") || n ~ /^\// || n ~ /(^|\/)\.\.(\/|$)/ { print }')
    [ -z "$bad" ] || fail "the release tarball has unsafe members: $bad"
}

lock_hash() { local h; h=$(sha256_of "$1/requirements.lock"); printf '%s\n' "${h:0:16}"; }

prepare_release() {   # TARBALL DIR: steps 3-5 in DIR.tmp (unpack, verify, venv, link, compile, freeze), then rename.
    local tb=$1 rel=$2 name=${2##*/} tmp="$2.tmp" bad h   # DIR exists only once complete, so it can be reused
    if [ -e "$rel" ]; then
        tar -xzOf "$tb" ./MANIFEST.sha256 | cmp -s - "$rel/MANIFEST.sha256" ||
            fail "releases/$name exists with a different MANIFEST: releases are immutable"
        bad=$(drift_list "$rel")
        if [ -n "$bad" ]; then
            printf '%s\n' "$bad" >&2
            fail "releases/$name exists with this MANIFEST but has drifted from it"
        fi
        h=$(lock_hash "$rel")
        [ "$(readlink -- "$rel/venv")" = "../../venvs/$h" ] || fail "releases/$name/venv does not point at ../../venvs/$h"
        ensure_venv "$h" "$rel/requirements.lock"
        log "releases/$name already exists with an identical MANIFEST: reusing it"
        return 0
    fi
    check_members "$tb"
    owned_rm "$tmp"
    owned_mkdir "$tmp"
    owned_untar "$tb" "$tmp"
    bad=$(drift_list "$tmp")
    if [ -n "$bad" ]; then
        printf '%s\n' "$bad" >&2
        fail "the unpacked release does not match its own MANIFEST"
    fi
    grep -qx "rel=$name" "$tmp/VERSION" || fail "VERSION does not name release $name"
    h=$(lock_hash "$tmp")
    ensure_venv "$h" "$tmp/requirements.lock"
    owned_link "../../venvs/$h" "$tmp/venv"
    owned_compile "$tmp/titan2" "$tmp/venv/bin/python"
    owned_freeze "$tmp"
    owned_mv "$tmp" "$rel"
    log "releases/$name: unpacked, verified, venv venvs/$h, bytecode compiled, frozen read-only"
}

pick_python() {
    local c
    for c in python3 python3.12 python3.11 python3.10; do
        if command -v "$c" >/dev/null 2>&1 && "$c" -c 'import sys; sys.exit(sys.version_info < (3, 10))' 2>/dev/null; then
            PYBIN=$c
            return 0
        fi
    done
    fail "no Python >= 3.10 on this server (see ops/bootstrap.sh)"
}

ensure_venv() {   # H LOCKFILE: venvs/H, built once per requirements.lock hash
    local v="$ROOT/venvs/$1"
    if [ -f "$v/.complete" ]; then
        log "venv: venvs/$1 exists"
        return 0
    fi
    if [ -e "$v" ]; then
        warn "venvs/$1 is incomplete (an interrupted build): rebuilding it"
        owned_rm "$v"
    fi
    pick_python
    log "venv: building venvs/$1 with $PYBIN (pip --require-hashes, wheels only)"
    owned_venv "$v" "$2"
}

deploy_selfcheck() {   # REL_DIR: the new release's `selfcheck --phase deploy` as titan, on a fresh copy of the DB
    local rel=$1 dest
    local args=(--phase deploy)
    if [ -n "$(current_release)" ]; then
        case $(db_exists) in
            yes)
                dest="$STATE_DIR/backups/deploy-${rel##*/}-$(date -u +%Y%m%dT%H%M%SZ)"
                log "backing up the live databases to $dest"
                titan2 backup --to "$dest" || fail "titan2 backup --to $dest failed"
                args+=(--db-copy "$dest/titan2.db")
                ;;
            no) log "no titan2.db yet: the migration dry run uses an empty database" ;;
            *) fail "\`titan2 status --json\` could not say whether titan2.db exists" ;;
        esac
    fi
    log "deploy self-check of ${rel##*/} (as titan): ${args[*]}"
    (cd -- "$rel" && sudo -n -u titan "$rel/venv/bin/python" -m titan2 selfcheck "${args[@]}") ||
        fail "selfcheck --phase deploy failed for ${rel##*/}: current is unchanged"
}

install_changed_units() {   # REL_DIR: copy changed titan2-* unit files, then daemon-reload (§13.6 step 8)
    local h f name dest changed=0
    local -A installed=()
    local present=("$UNIT_DIR"/titan2-*) shipped=("$1"/ops/systemd/titan2-*)
    [ "${#shipped[@]}" -gt 0 ] || fail "releases/${1##*/} ships no titan2-* unit files"
    if [ "${#present[@]}" -gt 0 ]; then         # (with no file argument sha256sum would read stdin: the script)
        while read -r h f; do                   # sha256sum prints "<hash>  <path>" (or "<hash> *<path>")
            f=${f#\*}
            installed[${f##*/}]=$h
        done < <(sha256sum -- "${present[@]}" 2>/dev/null </dev/null || true)
    fi
    while read -r h f; do
        name=${f#\*}
        name=${name##*/}
        dest="$UNIT_DIR/$name"
        if [ -L "$dest" ]; then
            warn "$name is a symlink in $UNIT_DIR (masked or linked): left as it is"
            continue
        fi
        if [ "${installed[$name]:-}" = "$h" ]; then continue; fi
        owned_install_unit "$1/ops/systemd/$name" "$dest"
        log "unit $name installed"
        changed=1
    done < <(sha256sum -- "${shipped[@]}" </dev/null)
    if (( changed )); then sudo -n systemctl daemon-reload; fi
}

restart_dash() {
    local s
    if unit_masked "$DASH"; then
        warn "$DASH is masked: not restarted"
        return 0
    fi
    if [ "$(unit_state "$DASH")" = failed ]; then sudo -n systemctl reset-failed "$DASH" || true; fi
    sudo -n systemctl restart "$DASH" || { warn "systemctl restart $DASH failed"; return 1; }
    if [ "$DASH_SETTLE_S" != 0 ]; then sleep "$DASH_SETTLE_S"; fi     # let a crash-looping gunicorn show itself
    s=$(unit_state "$DASH")
    if [ "$s" != active ]; then
        warn "$DASH is ${s:-unknown} after its restart"
        return 1
    fi
    log "$DASH restarted"
}

restart_bot_and_wait() {   # restart, then wait for READY and a fresh core heartbeat (or a clean exit 0)
    local t0 start=$SECONDS state result
    printf -v t0 '%(%s)T' -1
    if [ "$(unit_state "$BOT")" = failed ]; then sudo -n systemctl reset-failed "$BOT" || true; fi
    log "restarting $BOT; waiting up to ${READY_TIMEOUT_S}s for READY and a heartbeat under ${HEARTBEAT_MAX_S}s"
    if ! timeout "$READY_TIMEOUT_S" sudo -n systemctl restart "$BOT"; then
        warn "$BOT did not reach READY (systemctl restart failed or timed out)"
        return 1
    fi
    while :; do
        state=$(unit_state "$BOT")
        case $state in
            active)
                if heartbeat_fresh "$t0"; then
                    log "$BOT is READY with a fresh heartbeat"
                    return 0
                fi
                ;;
            inactive | failed)
                result=$(unit_prop "$BOT" Result)
                if [ "$state" = inactive ] && [ "$result" = success ]; then
                    log "$BOT reached READY and exited 0 (nothing to trade now)"
                    return 0
                fi
                warn "$BOT stopped after its restart (state $state, result ${result:-?})"
                return 1
                ;;
        esac
        if (( SECONDS - start >= READY_TIMEOUT_S )); then
            warn "$BOT showed no heartbeat under ${HEARTBEAT_MAX_S}s within ${READY_TIMEOUT_S}s (state ${state:-unknown})"
            return 1
        fi
        sleep "$POLL_S"
    done
}

restart_services() {   # $1 = 1 with --restart. The dashboard always; the bot with --restart or when it is down.
    local state                       # Returns 1 with REFUSAL set when the market-hours rule kept the bot as it was.
    BOT_RESTARTED=0
    CHANGED=1
    REFUSAL=""
    restart_dash || return 1
    state=$(unit_state "$BOT")
    if unit_masked "$BOT"; then
        warn "$BOT is masked: not started (unmask it to run the new release)"
    elif [ "$1" = 1 ] || ! is_running_state "$state"; then
        if ! market_hours_ok "$ALLOW_MH" " (again, just before restarting $BOT)"; then
            warn "$BOT NOT restarted: $REFUSAL"
            return 1
        fi
        BOT_RESTARTED=1
        restart_bot_and_wait || return 1
    else
        log "$BOT is $state and --restart was not given: it keeps its loaded code until its next start"
    fi
}

auto_rollback() {   # called from on_exit after a failure that followed the switch
    local rc=$1 back=$ROLLBACK_TO dash=ok bot=""
    ROLLBACK_TO=""
    if [ "$back" = "-" ]; then
        warn "deploy of $REL failed after the switch (exit $rc) and there is no previous release: current stays at $REL"
        alert_critical "titan2 deploy $REL FAILED after the switch (exit $rc); no previous release to roll back to"
        return
    fi
    warn "deploy of $REL failed after the switch (exit $rc): rolling back to $back"
    owned_link "releases/$back" "$ROOT/current"
    history_append autorollback "$REL" "$back"
    install_changed_units "$ROOT/releases/$back"
    restart_dash || dash=FAILED
    if (( BOT_RESTARTED )); then
        if restart_bot_and_wait; then bot="; bot READY"; else bot="; bot NOT READY"; fi
    fi
    alert_critical "titan2 deploy $REL FAILED (exit $rc${FAILED_WHY:+: $FAILED_WHY}): current rolled back to $back (dashboard $dash$bot)"
}

prune() {   # keep the 5 newest releases plus current and previous; drop unreferenced venvs and stale uploads
    local keep=" " used=" " r v
    keep+="$(current_release) $(previous_release) "
    while read -r r; do
        keep+="$r "
    done < <(cd -- "$ROOT/releases" && find . -mindepth 1 -maxdepth 1 -type d ! -name '*.tmp' -printf '%T@ %f\n' |
             sort -rn | head -n "$KEEP_RELEASES" | cut -d' ' -f2)
    for r in "$ROOT"/releases/*; do
        if [ -L "$r" ] || [ ! -d "$r" ]; then continue; fi
        case $keep in *" ${r##*/} "*) continue ;; esac
        log "prune: releases/${r##*/}"
        owned_rm "$r"
    done
    for r in "$ROOT"/releases/*/venv; do
        if [ -L "$r" ]; then
            v=$(readlink -- "$r")
            used+="${v##*/} "
        fi
    done
    for v in "$ROOT"/venvs/*; do
        case $used in *" ${v##*/} "*) continue ;; esac
        log "prune: venvs/${v##*/}"
        owned_rm "$v"
    done
    for r in "$ROOT"/incoming/release-*.tgz; do
        owned_rm "$r"
    done
}

# ---------------------------------------------------------------------------------------------------------------
# Commands
# ---------------------------------------------------------------------------------------------------------------
cmd_preflight() {
    guard_market_hours "$ALLOW_MH"
    guard_drift
    guard_disk
    log "preflight ok"
}

cmd_install() {   # REL SHA256
    local tb rel prev why
    REL=$1
    [[ $REL =~ ^[0-9a-f]{7,40}$ ]] || fail "bad release id '$REL'"
    [[ $2 =~ ^[0-9a-f]{64}$ ]] || fail "bad sha256 '$2'"
    owned_lock
    guard_market_hours "$ALLOW_MH"
    guard_drift
    guard_disk
    tb="$ROOT/incoming/release-$REL.tgz"
    rel="$ROOT/releases/$REL"
    [ -f "$tb" ] || fail "$tb is missing (upload it first)"
    [ "$(sha256_of "$tb")" = "$2" ] || fail "$tb does not match sha256 $2: a corrupt upload"
    prev=$(current_release)
    # 3-5. unpack, venv (one per requirements.lock hash), compile and freeze; or reuse an identical release
    prepare_release "$tb" "$rel"
    owned_rm "$tb"
    # 6. verify
    deploy_selfcheck "$rel"
    # the rule again: steps 3-6 took minutes, and the bot may have entered a trade or started meanwhile
    guard_market_hours "$ALLOW_MH" " (again, just before the switch)"
    # 7. the atomic switch; from here a failure rolls back (on_exit)
    if [ "$prev" = "$REL" ]; then
        log "$REL is already current: no switch"
    else
        ROLLBACK_TO=${prev:--}
        CHANGED=1
        owned_link "releases/$REL" "$ROOT/current"
        history_append deploy "$prev" "$REL"
        log "current -> releases/$REL (was ${prev:-none})"
    fi
    # 8. units; 9. restarts
    install_changed_units "$rel"
    if ! restart_services "$WANT_RESTART"; then
        why=${REFUSAL:+"$BOT was not restarted: $REFUSAL"}
        if [ -n "$ROLLBACK_TO" ]; then fail "${why:-the restart after the switch failed}"; fi
        alert_critical "titan2 deploy of $REL (already current): ${why:-the restart failed}"
        exit 1
    fi
    ROLLBACK_TO=""
    # 11. prune
    prune
    log "deployed $REL"
}

cmd_rollback() {
    local cur target bad
    owned_lock
    guard_market_hours "$ALLOW_MH"
    cur=$(current_release)
    [ -n "$cur" ] || fail "there is no current release"
    target=$TO
    if [ -z "$target" ]; then target=$(previous_release); fi
    [ -n "$target" ] || fail "releases/HISTORY names no release before $cur: pass --to <release>"
    [[ $target =~ ^[0-9a-f]{7,40}$ ]] || fail "bad release id '$target'"
    [ "$target" != "$cur" ] || fail "$target is already current"
    [ -d "$ROOT/releases/$target" ] || fail "releases/$target does not exist (pruned?)"
    bad=$(drift_list "$ROOT/releases/$target")
    if [ -n "$bad" ]; then
        printf '%s\n' "$bad" >&2
        fail "releases/$target has drifted from its MANIFEST"
    fi
    [ -f "$ROOT/releases/$target/venv/.complete" ] || fail "releases/$target has no complete venv"
    bad=$(drift_list "$ROOT/releases/$cur")
    if [ -n "$bad" ]; then
        printf '%s\n' "$bad" >&2
        warn "current ($cur) has drifted (above); its files stay in releases/$cur: copy them with ops/pull_drift.sh <host> --release $cur before it is pruned"
    fi
    CHANGED=1
    owned_link "releases/$target" "$ROOT/current"
    history_append rollback "$cur" "$target"
    log "current -> releases/$target (was $cur)"
    install_changed_units "$ROOT/releases/$target"
    if ! restart_services "$WANT_RESTART"; then
        if [ -n "$REFUSAL" ]; then
            alert_critical "titan2 rollback $cur -> $target: current switched, but $BOT was NOT restarted and keeps running $cur until its next start: $REFUSAL"
        else
            alert_critical "titan2 rollback $cur -> $target: the services did not come back (see journalctl)"
        fi
        exit 1
    fi
    log "rolled back to $target"
}

release_dir_arg() {   # [REL]: the release dir to inspect (default: current)
    local r=${1:-}
    if [ -z "$r" ]; then r=$(current_release); fi
    [ -n "$r" ] || fail "there is no current release"
    [[ $r =~ ^[0-9a-f]{7,40}$ ]] || fail "bad release id '$r'"
    [ -d "$ROOT/releases/$r" ] || fail "releases/$r does not exist"
    printf '%s\n' "$r"
}

cmd_drift() {
    local r out
    r=$(release_dir_arg "${1:-}")
    printf 'release %s\n' "$r"
    out=$(drift_list "$ROOT/releases/$r")
    if [ -n "$out" ]; then
        printf '%s\n' "$out"
        exit 1
    fi
}

cmd_drift_tar() {
    local r
    r=$(release_dir_arg "${1:-}")
    cd -- "$ROOT/releases/$r"
    drift_list . | awk '$1 == "M" || $1 == "A" { print substr($0, 3) }' | tr '\n' '\0' | tar --null -T - -cf -
}

on_exit() {
    local rc=$?
    trap - EXIT
    set +e
    if [ -n "$ROLLBACK_TO" ]; then
        auto_rollback "$rc"
        rc=1
    elif (( rc != 0 && rc != 2 && ! CHANGED )) && [ "$CMD" != drift ]; then
        printf '[titan2 release] FAILED (exit %s) before anything changed: current is unchanged\n' "$rc" >&2
        rc=2
    fi
    owned_unlock
    exit "$rc"
}

# ---------------------------------------------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------------------------------------------
CMD=${1:-}
if [ $# -gt 0 ]; then shift; fi
ALLOW_MH=0 WANT_RESTART=0 TO=""
POS=()
while [ $# -gt 0 ]; do
    case $1 in
        --allow-market-hours) ALLOW_MH=1 ;;
        --restart) WANT_RESTART=1 ;;
        --to) TO=${2:-}; shift ;;
        --*) fail "unknown option $1" ;;
        *) POS+=("$1") ;;
    esac
    shift
done

trap on_exit EXIT
trap 'exit 129' HUP
trap 'exit 130' INT
trap 'exit 143' TERM

case $CMD in
    preflight) cmd_preflight ;;
    install)
        [ "${#POS[@]}" -eq 2 ] || fail "usage: release.sh install REL SHA256 [--allow-market-hours] [--restart]"
        cmd_install "${POS[@]}"
        ;;
    rollback) cmd_rollback ;;
    drift) cmd_drift "${POS[@]}" ;;
    drift-tar) cmd_drift_tar "${POS[@]}" ;;
    assert-owned)                     # one line per path; exit 2 if any path is refused
        [ "${#POS[@]}" -gt 0 ] || fail "usage: release.sh assert-owned PATH..."
        _rc=0
        for _p in "${POS[@]}"; do
            if (assert_owned "$_p") 2>/dev/null; then log "owned: $_p"; else log "REFUSED: $_p"; _rc=2; fi
        done
        exit "$_rc"
        ;;
    *) fail "usage: release.sh preflight | install REL SHA256 | rollback | drift [REL] | drift-tar [REL] | assert-owned PATH... [--allow-market-hours] [--restart] [--to REL]" ;;
esac
