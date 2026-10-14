#!/usr/bin/env bash
# titan2 backups (DETAIL §13.8), run by titan2-backup.service as user titan (weekdays 16:10, Saturday 03:00 IST).
# By hand on the server: sudo -u titan bash /home/ubuntu/titan/current/ops/backup.sh
#   1, 2, 4. `python -m titan2 backup --to DIR --gzip`: SQLite online copies (a reader, never a writer) of
#            titan2.db, paper_exchange.db, commands_cli.db and commands_dash.db, each integrity-checked and gzipped,
#            plus settings.json. DIR = /var/backups/titan2/YYYY-MM-DD (a second run that day: YYYY-MM-DD-HHMMSS).
#   3.       Retention: the newest set of each of the 30 newest days and of each of the 12 newest months is kept;
#            older sets are deleted. Deploy-time copies (/var/lib/titan2/backups/deploy-*) older than 30 days are
#            deleted too; the 3 newest always stay.
#   5.       When /etc/titan2/titan2.env sets BACKUP_S3_URI: `aws s3 cp --recursive` of DIR (instance role). Only
#            that one key is read from the env file; the service has no credentials in its environment.
# Secrets are never copied. This replaces daily_maintenance.sh 37-47, which copied a plaintext .env and never
# backed up a database. Exit code 0 ok; anything else fails the unit and fires its OnFailure alert.
set -euo pipefail
umask 077
shopt -s nullglob

# The TITAN2_* overrides exist for tests/ops only (TITAN2_VAR is also set by the unit).
APP_DIR=${TITAN2_APP_DIR:-/home/ubuntu/titan/agentic_trader}
PY=${TITAN2_PYTHON:-/home/ubuntu/titan/current/venv/bin/python}
VAR=${TITAN2_VAR:-/var/lib/titan2}
DEST_ROOT=${TITAN2_BACKUP_DIR:-/var/backups/titan2}
ENV_FILE=${TITAN2_ENV_FILE:-/etc/titan2/titan2.env}
KEEP_DAILY=30
KEEP_MONTHLY=12
KEEP_DEPLOY_DAYS=30
KEEP_DEPLOY_MIN=3

log() { printf '[titan2 backup] %s\n' "$*"; }

[ -d "$DEST_ROOT" ] || { log "FAILED: $DEST_ROOT does not exist (ops/bootstrap.sh creates it)"; exit 2; }
DAY=$(date +%F)
DEST="$DEST_ROOT/$DAY"
if [ -e "$DEST" ]; then DEST="$DEST_ROOT/$DAY-$(date +%H%M%S)"; fi
log "backing up to $DEST"
(cd -- "$APP_DIR" && "$PY" -m titan2 backup --to "$DEST" --gzip)

# 3. retention of the dated sets (names sort by date; the first seen of a day or month is its newest set)
mapfile -t SETS < <(cd -- "$DEST_ROOT" && find . -mindepth 1 -maxdepth 1 -type d \
    -name '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]*' -printf '%f\n' | LC_ALL=C sort -r)
mapfile -t DROP < <(printf '%s\n' "${SETS[@]}" | awk -v kd="$KEEP_DAILY" -v km="$KEEP_MONTHLY" '
    NF {
        d = substr($1, 1, 10); m = substr($1, 1, 7); keep = 0
        if (!(d in days)) { days[d] = 1; if (++nd <= kd) keep = 1 }
        if (!(m in months)) { months[m] = 1; if (++nm <= km) keep = 1 }
        if (!keep) print
    }')
for s in "${DROP[@]}"; do
    log "retention: removing $s"
    rm -rf -- "${DEST_ROOT:?}/$s"
done

# deploy-time copies made by `titan2 backup --to /var/lib/titan2/backups/deploy-*` (ops/release.sh)
mapfile -t OLD < <(cd -- "$VAR/backups" 2>/dev/null && find . -mindepth 1 -maxdepth 1 -type d -name 'deploy-*' \
    -printf '%T@ %f\n' | sort -rn |
    awk -v keep="$KEEP_DEPLOY_MIN" -v cutoff="$(( $(date +%s) - KEEP_DEPLOY_DAYS * 86400 ))" 'NR > keep && $1 < cutoff { print $2 }')
for s in "${OLD[@]}"; do
    log "retention: removing $VAR/backups/$s"
    rm -rf -- "${VAR:?}/backups/$s"
done

# 5. optional S3 copy
S3=$(sed -n 's/^BACKUP_S3_URI=//p' "$ENV_FILE" 2>/dev/null | tail -n 1 | tr -d "\"'" || true)
if [ -n "$S3" ]; then
    command -v aws >/dev/null 2>&1 || { log "FAILED: BACKUP_S3_URI is set but the aws CLI is not installed"; exit 1; }
    aws s3 cp --recursive --only-show-errors "$DEST" "${S3%/}/${DEST##*/}/"
    log "copied to ${S3%/}/${DEST##*/}/"
fi
log "done: $DEST"
