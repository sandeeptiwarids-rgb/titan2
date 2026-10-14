#!/usr/bin/env bash
# Flip the server's `current` back to the previous release (DETAIL §13.6, playbook 9). Run on the operator's machine:
#   bash agentic_trader/ops/rollback.sh <host> [--to <release>] [--allow-market-hours] [--restart]
# It follows the deploy's market-hours rule, refuses a target release that has drifted or lost its venv, warns (and
# keeps the files) when current itself has drifted, installs the target's changed titan2-* units, restarts
# titan2-dash, and restarts titan2-bot with --restart or when it is not running (120 s wait for READY).
# "Previous" is the release that current replaced at its last switch (releases/HISTORY); --to picks any release.
# On the server, without the laptop: bash /home/ubuntu/titan/current/ops/release.sh rollback [...]
# Exit codes: 0 rolled back; 1 switched but a service did not come back, or titan2-bot opened a position meanwhile
# and was left running on the old release (CRITICAL alert sent either way); 2 refused.
set -euo pipefail

usage() { echo "usage: $0 <host> [--to <release>] [--allow-market-hours] [--restart]" >&2; exit 2; }

OPS_DIR=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
[ $# -ge 1 ] || usage
HOST=$1
shift
case $HOST in -* | "") usage ;; esac
ARGS=()
while [ $# -gt 0 ]; do
    case $1 in
        --allow-market-hours | --restart) ARGS+=("$1") ;;
        --to)
            [[ ${2:-} =~ ^[0-9a-f]{7,40}$ ]] || usage
            ARGS+=(--to "$2")
            shift
            ;;
        *) usage ;;
    esac
    shift
done

exec ssh -o BatchMode=yes -T "$HOST" "bash -s -- rollback ${ARGS[*]}" < "$OPS_DIR/release.sh"
