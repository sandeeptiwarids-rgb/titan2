#!/usr/bin/env bash
# Stage 0 of the cutover (DETAIL §13.11): secure and freeze v1 on the server, outside market hours, before any v2.
# DRY-RUN BY DEFAULT: it prints every action and changes nothing. --execute performs them.
#   From the operator's machine: bash agentic_trader/ops/stage0_cutover.sh <host> [--execute] [--allow-market-hours]
#   On the server itself:        bash stage0_cutover.sh --local [--execute] [--allow-market-hours]
#
# Automates steps 2-3 (user ubuntu, with its own sudo; v2's allow-list does not exist yet):
#   2. Disable, stop and mask v1's units: titan-bot, titan-dashboard and every other titan* unit except titan2-*.
#      Unit files under /etc/systemd/system are moved to ~/v1_units_backup first: `systemctl mask` cannot replace a
#      real file, and a merely stopped unit is restarted by kite_token_manager 312-330. Back up and clear the ubuntu
#      crontab (v1's 08:30 token job restarted the bot; also Dhan, OI-backfill, maintenance, log cleanup). Back up
#      /etc/nginx to ~/v1_nginx_backup, remove the titan site, and stop nginx when no other site is enabled.
#   3. Online-backup v1's titan_state.db to ~/titan-v1-state.db; set agentic_trader/.env to 0600; move
#      /home/ubuntu/titan/agentic_trader to v1_frozen_<YYYYMMDD>; tar it without secrets or databases to
#      ~/v1_frozen_<YYYYMMDD>.tgz; make the frozen tree read-only and owner-only. Run from the operator's machine with
#      --execute, it then copies the DB, the tarball and the crontab backup to ./v1_archive/<host>-<YYYYMMDD>/ (never
#      overwriting).
#  3b. Make the rest of v1's files owner-only (chmod -R go-rwx): every v1_frozen_* tree, ~/titan/logs, ~/backups (v1's
#      daily copies of .env and dhan_config.json, and deploy.yml's pre-deploy tarballs of the whole tree), the secret
#      files at the top of ~/titan and ~ (zerodha_token.json lived in ~/titan) and /tmp/.env.backup (deploy.yml's
#      copy of .env). Stage 1's bootstrap.sh opens /home/ubuntu to 0751 so that user titan can reach the releases,
#      and ~/titan is 0755: any user, the dashboard's titandash included, can then read what v1 left readable there.
#      Everything Stage 0 writes itself is owner-only (umask 077).
# Steps 1, 4 and 5 (the CI workflow and EC2_SSH_KEY, secret rotation, Elastic IP and security group) are manual and
# are printed at the end. Every action is reversible: backups are kept, `systemctl unmask`, `crontab FILE`, `mv`.
# Refuses --execute on a weekday between 08:30 and 16:00 IST unless --allow-market-hours.
# Exit codes: 0 done (or dry run); 2 refused or failed.
set -euo pipefail
shopt -s nullglob
umask 077                    # the backups, the tarball and the copies hold v1 data: owner-only from the start

# Paths; the TITAN2_* overrides exist for tests/ops only.
TITAN_HOME=${TITAN2_ROOT:-/home/ubuntu/titan}
UNIT_DIR=${TITAN2_UNIT_DIR:-/etc/systemd/system}
NGINX_DIR=${TITAN2_NGINX_DIR:-/etc/nginx}
TMP_DIR=${TITAN2_TMP_DIR:-/tmp}
V1="$TITAN_HOME/agentic_trader"
V1_UNITS=(titan-bot.service titan-dashboard.service)
TAR_EXCLUDES=(--exclude='.env*' --exclude='.kite_access_token' --exclude='dhan_config.json' --exclude='zerodha_token.json'
              --exclude='*.pem' --exclude='*.key' --exclude='*.db' --exclude='*.db-wal' --exclude='*.db-shm'
              --exclude='__pycache__' --exclude='*.pyc')
# v1's secret files by name, as a find expression: the same names as the first six TAR_EXCLUDES (tests/ops checks)
SECRET_NAMES=(-name '.env*' -o -name '.kite_access_token' -o -name 'dhan_config.json' -o -name 'zerodha_token.json'
              -o -name '*.pem' -o -name '*.key')

EXECUTE=0 ALLOW_MH=0 MODE=remote HOST=""
ARGS=()
ARCHIVE=()

usage() { echo "usage: $0 <host> [--execute] [--allow-market-hours]   |   $0 --local [--execute] [--allow-market-hours]" >&2; exit 2; }
die() { printf '[stage0] REFUSED: %s\n' "$*" >&2; exit 2; }
log() { printf '[stage0] %s\n' "$*"; }
run() {   # print the action; perform it only with --execute
    if (( EXECUTE )); then
        printf '+ %s\n' "$*"
        "$@"
    else
        printf 'DRY-RUN: %s\n' "$*"
    fi
}

ist_now() {   # "YYYY-MM-DD HHMM" in IST (UTC+05:30)
    if [ -n "${TITAN2_OPS_NOW:-}" ]; then
        [ -n "${TITAN2_ROOT:-}" ] || die "TITAN2_OPS_NOW is a test clock: refused unless TITAN2_ROOT points at a test layout"
        printf '%s\n' "$TITAN2_OPS_NOW"
        return
    fi
    date -u -d "@$(( $(date -u +%s) + 19800 ))" '+%F %H%M'
}

while [ $# -gt 0 ]; do
    case $1 in
        --execute) EXECUTE=1; ARGS+=("$1") ;;
        --allow-market-hours) ALLOW_MH=1; ARGS+=("$1") ;;
        --local) MODE=local ;;
        -*) usage ;;
        *) [ -z "$HOST" ] || usage; HOST=$1 ;;
    esac
    shift
done
if [ "$MODE" = remote ] && [ -z "$HOST" ]; then usage; fi
if [ "$MODE" = local ] && [ -n "$HOST" ]; then usage; fi

NOW=$(ist_now)
DAY=${NOW%% *}
STAMP=${DAY//-/}

# ---------------------------------------------------------------------------------------------------------------
# On the server
# ---------------------------------------------------------------------------------------------------------------
v1_unit_list() {
    {
        printf '%s\n' "${V1_UNITS[@]}"
        systemctl list-unit-files --no-legend --plain 'titan*' 2>/dev/null | awk '{ print $1 }'
    } | grep -E '^titan[^2]' | LC_ALL=C sort -u
}

unit_enabled() {   # masked | enabled | disabled | static | ... | "" when the unit does not exist
    local s
    s=$(systemctl is-enabled "$1" 2>/dev/null || true)
    case $s in not-found | "") printf '' ;; *) printf '%s\n' "$s" ;; esac
}

step_units() {
    local u state reload=0 todo=()
    log "step 2a: v1 systemd units"
    mapfile -t UNITS < <(v1_unit_list)
    for u in "${UNITS[@]}"; do
        state=$(unit_enabled "$u")
        if [ -z "$state" ] && [ ! -e "$UNIT_DIR/$u" ]; then
            log "$u: not installed"
            continue
        fi
        if [ "$state" = masked ] && ! systemctl is-active --quiet "$u"; then
            log "$u: already masked and inactive"
            continue
        fi
        log "$u: is-enabled=${state:-?}, is-active=$(systemctl is-active "$u" 2>/dev/null || true)"
        run sudo systemctl disable --now "$u"
        # mask needs /etc/systemd/system/<unit> free: a copied or linked unit file is backed up and removed first
        if { [ -f "$UNIT_DIR/$u" ] || [ -L "$UNIT_DIR/$u" ]; } && [ "$(readlink -- "$UNIT_DIR/$u" || true)" != /dev/null ]; then
            run mkdir -p "$HOME/v1_units_backup"
            run sudo cp -a "$UNIT_DIR/$u" "$HOME/v1_units_backup/$u"
            run sudo rm -f "$UNIT_DIR/$u"
            reload=1
        fi
        todo+=("$u")
    done
    if (( reload )); then run sudo systemctl daemon-reload; fi
    for u in "${todo[@]}"; do
        run sudo systemctl mask "$u"
    done
}

step_crontab() {
    local cron bak hits
    log "step 2b: crontab"
    cron=$(crontab -l 2>/dev/null || true)
    if [ -z "$cron" ]; then
        log "ubuntu crontab: empty"
    else
        printf '%s\n' "$cron" | sed 's/^/    | /'
        bak="$HOME/v1-crontab.bak"
        if [ -e "$bak" ]; then bak="$HOME/v1-crontab.bak.$STAMP-$$"; fi
        if (( EXECUTE )); then
            printf '%s\n' "$cron" > "$bak"
            printf '+ crontab -l > %s\n' "$bak"
        else
            printf 'DRY-RUN: crontab -l > %s\n' "$bak"
        fi
        run crontab -r
        ARCHIVE+=("$bak")
    fi
    hits=$(grep -ls agentic_trader /etc/crontab /etc/cron.d/* 2>/dev/null || true)
    if [ -n "$hits" ]; then log "MANUAL: these system cron files mention agentic_trader: $hits"; fi
    cron=$(sudo -n crontab -l -u root 2>/dev/null || true)
    case $cron in
        *agentic_trader*) log "MANUAL: root's crontab mentions agentic_trader (sudo crontab -e -u root)" ;;
    esac
}

step_nginx() {
    local sites=() others=() s
    log "step 2c: nginx"
    sites=("$NGINX_DIR"/sites-enabled/titan* "$NGINX_DIR"/sites-available/titan* "$NGINX_DIR"/conf.d/titan*)
    if [ "${#sites[@]}" -eq 0 ]; then
        log "nginx: no titan site"
        return 0
    fi
    run sudo cp -a "$NGINX_DIR" "$HOME/v1_nginx_backup"
    for s in "${sites[@]}"; do
        run sudo rm -f "$s"
    done
    for s in "$NGINX_DIR"/sites-enabled/* "$NGINX_DIR"/conf.d/*.conf; do
        case ${s##*/} in titan*) ;; *) others+=("$s") ;; esac
    done
    if [ "${#others[@]}" -eq 0 ]; then
        run sudo systemctl disable --now nginx
    else
        log "nginx serves other sites (${others[*]}): reloading it instead of stopping it"
        run sudo nginx -t
        run sudo systemctl reload nginx
    fi
}

step_archive() {
    local frozen="$TITAN_HOME/v1_frozen_$STAMP" db="$V1/titan_state.db" out tgz="$HOME/v1_frozen_$STAMP.tgz"
    log "step 3: archive the v1 tree"
    if [ ! -d "$V1" ]; then
        log "$V1 is gone already (frozen trees: $(printf '%s ' "$TITAN_HOME"/v1_frozen_*))"
        return 0
    fi
    if [ -L "$V1/titan2" ]; then die "$V1 already holds v2's titan2 symlink: Stage 0 was done"; fi
    [ ! -e "$frozen" ] || die "$frozen already exists"
    [ ! -e "$tgz" ] || die "$tgz already exists"
    if [ -f "$db" ]; then
        out="$HOME/titan-v1-state.db"
        if [ -e "$out" ]; then out="$HOME/titan-v1-state-$STAMP-$$.db"; fi
        if (( EXECUTE )); then
            printf '+ sqlite online backup %s -> %s\n' "$db" "$out"
            python3 - "$db" "$out" <<'PY'
import sqlite3, sys
src = sqlite3.connect(f"file:{sys.argv[1]}?mode=ro", uri=True)
dst = sqlite3.connect(sys.argv[2])
src.backup(dst)
ok = dst.execute("PRAGMA integrity_check").fetchone()[0]
dst.close()
src.close()
sys.exit(0 if ok == "ok" else f"integrity_check of the copy: {ok}")
PY
        else
            printf 'DRY-RUN: sqlite online backup %s -> %s\n' "$db" "$out"
        fi
        ARCHIVE+=("$out")
    else
        log "no $db (nothing to back up)"
    fi
    if [ -f "$V1/.env" ]; then run chmod 600 "$V1/.env"; fi
    run mv -T "$V1" "$frozen"
    if (( EXECUTE )); then
        printf '+ tar -czf %s (without secrets and databases) -C %s %s\n' "$tgz" "$TITAN_HOME" "${frozen##*/}"
        tar -czf "$tgz" "${TAR_EXCLUDES[@]}" -C "$TITAN_HOME" "${frozen##*/}"
    else
        printf 'DRY-RUN: tar -czf %s %s -C %s %s\n' "$tgz" "${TAR_EXCLUDES[*]}" "$TITAN_HOME" "${frozen##*/}"
    fi
    ARCHIVE+=("$tgz")
    run chmod -R go-rwx,a-w "$frozen"
}

lock_down() {   # PATH: no access for group and others, recursively. sudo never follows a symlink here.
    if [ -L "$1" ]; then
        log "MANUAL: $1 is a symlink to $(readlink -- "$1"): make that owner-only by hand (chmod -R go-rwx)"
    elif [ -e "$1" ]; then
        run sudo chmod -R go-rwx "$1"
    fi
}

step_lockdown() {   # step 3b (header): what v1 left under /home/ubuntu, before bootstrap opens it to 0751
    local p
    log "step 3b: v1's files under $HOME become owner-only"
    for p in "$TITAN_HOME"/v1_frozen_* "$TITAN_HOME/logs" "$HOME/backups" "$TMP_DIR/.env.backup"; do
        lock_down "$p"
    done
    while IFS= read -r -d '' p; do
        lock_down "$p"
    done < <(find "$TITAN_HOME" "$HOME" -maxdepth 1 -type f \( "${SECRET_NAMES[@]}" \) -print0 2>/dev/null)
}

summary() {
    local u listen a
    echo
    log "=== summary ==="
    if (( ! EXECUTE )); then log "DRY RUN: nothing was changed. Re-run with --execute to apply the actions above."; fi
    for u in "${UNITS[@]}"; do
        log "$u: is-enabled=$(unit_enabled "$u") is-active=$(systemctl is-active "$u" 2>/dev/null || true)"
    done
    listen=$(ss -ltnH 2>/dev/null | awk '{ print $4 }' | grep -E ':(80|5000)$' || true)
    if [ -n "$listen" ]; then log "still listening: $listen"; fi
    for a in "${ARCHIVE[@]}"; do printf 'ARCHIVE: %s\n' "$a"; done
    cat <<'EOF'
[stage0] MANUAL steps (DETAIL 13.11, Stage 0):
  1. Before anything is pushed to main: gh workflow disable "Deploy Titan Bot"; delete the EC2_SSH_KEY repo secret;
     merge the commit that deletes .github/workflows/deploy.yml and adds ci.yml.
  4. Rotate the leaked secrets: regenerate the Kite API secret, change the Zerodha password, re-enrol TOTP, revoke
     the Dhan token (.kite_access_token and dhan_config.json were committed and pushed).
  5. Allocate and associate an Elastic IP, update ~/.ssh/config (titan-bot), register the IP in the Kite developer
     console [V]; remove the security-group inbound rules for ports 80 and 5000.
EOF
}

local_main() {
    local dow hm
    log "Stage 0 on $(hostname 2>/dev/null || echo this host) at $NOW IST ($([ "$EXECUTE" = 1 ] && echo EXECUTE || echo DRY RUN))"
    if (( EXECUTE && ! ALLOW_MH )); then
        dow=$(date -u -d "$DAY" +%u)
        hm=${NOW#* }
        if (( dow <= 5 && 10#$hm >= 830 && 10#$hm < 1600 )); then
            die "it is $NOW IST on a weekday: run Stage 0 outside market hours (or pass --allow-market-hours)"
        fi
    fi
    UNITS=()
    step_units
    step_crontab
    step_nginx
    step_archive
    step_lockdown
    summary
}

# ---------------------------------------------------------------------------------------------------------------
# On the operator's machine
# ---------------------------------------------------------------------------------------------------------------
OUT_FILE=""
remote_main() {
    local rc dest f name
    OUT_FILE=$(mktemp "${TMPDIR:-/tmp}/titan2-stage0.XXXXXX")
    trap 'rm -f -- "$OUT_FILE"' EXIT
    set +e
    ssh -o BatchMode=yes -T "$HOST" "bash -s -- --local ${ARGS[*]}" < "${BASH_SOURCE[0]}" | tee "$OUT_FILE"
    rc=${PIPESTATUS[0]}
    set -e
    (( rc == 0 )) || exit "$rc"
    if (( ! EXECUTE )); then return 0; fi
    dest="./v1_archive/$HOST-$STAMP"
    mkdir -p -- "$dest"
    while IFS= read -r f; do
        name=${f##*/}
        if [ -e "$dest/$name" ]; then
            log "kept the existing $dest/$name (not overwritten)"
            continue
        fi
        scp -q -o BatchMode=yes "$HOST:$f" "$dest/$name"
        log "copied $f -> $dest/$name"
    done < <(sed -n 's/^ARCHIVE: //p' "$OUT_FILE")
}

if [ "$MODE" = local ]; then local_main; else remote_main; fi
