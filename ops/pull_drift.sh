#!/usr/bin/env bash
# Copy the server's drifted release files to this machine (DETAIL §13.6 2b). Run on the operator's machine:
#   bash agentic_trader/ops/pull_drift.sh <host> [--release <rel>] [--dest DIR]
# Drift = files in current (or --release) that differ from its MANIFEST.sha256 (M), are missing (D), or are beyond
# it (A): edits made on the server, which a deploy refuses to switch away from. M and A files are copied to
# DIR/<rel>/ (default ./drift/<rel>/) with their release-relative paths. An existing local file is never
# overwritten: it is skipped and named. Compare with `git diff --no-index`, commit what you keep, then deploy.
# Exit codes: 0 copied (or no drift); 2 failed.
set -euo pipefail

usage() { echo "usage: $0 <host> [--release <rel>] [--dest DIR]" >&2; exit 2; }
die() { printf '[titan2 pull_drift] FAILED: %s\n' "$*" >&2; exit 2; }
log() { printf '[titan2 pull_drift] %s\n' "$*"; }

OPS_DIR=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
[ $# -ge 1 ] || usage
HOST=$1
shift
case $HOST in -* | "") usage ;; esac
REL="" DEST=./drift
while [ $# -gt 0 ]; do
    case $1 in
        --release) [[ ${2:-} =~ ^[0-9a-f]{7,40}$ ]] || usage; REL=$2; shift ;;
        --dest) [ -n "${2:-}" ] || usage; DEST=$2; shift ;;
        *) usage ;;
    esac
    shift
done

remote() { ssh -o BatchMode=yes -T "$HOST" "bash -s -- $*" < "$OPS_DIR/release.sh"; }

set +e
LIST=$(remote drift $REL)
RC=$?
set -e
case $RC in 0 | 1) ;; *) die "could not read the drift of $HOST (exit $RC)" ;; esac
REL_NAME=$(printf '%s\n' "$LIST" | sed -n '1s/^release //p')
[[ $REL_NAME =~ ^[0-9a-f]{7,40}$ ]] || die "unexpected answer from $HOST: $(printf '%s' "$LIST" | head -n 1)"
ENTRIES=$(printf '%s\n' "$LIST" | sed -n '2,$p')
if [ -z "$ENTRIES" ]; then
    log "release $REL_NAME on $HOST has no drift"
    exit 0
fi
printf '%s\n' "$ENTRIES"

OUT="$DEST/$REL_NAME"
mkdir -p -- "$OUT"
EXISTING=$(printf '%s\n' "$ENTRIES" | awk '$1 == "M" || $1 == "A" { print substr($0, 3) }' |
    while IFS= read -r p; do if [ -e "$OUT/$p" ] || [ -L "$OUT/$p" ]; then printf '%s\n' "$p"; fi; done)
remote drift-tar "$REL_NAME" | tar -xf - -C "$OUT" --skip-old-files
if [ -n "$EXISTING" ]; then
    log "kept these existing local copies (not overwritten):"
    printf '%s\n' "$EXISTING" | sed 's/^/  /'
fi
log "copied the M and A files of $REL_NAME to $OUT (D files are only listed above)"
