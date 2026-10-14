#!/usr/bin/env bash
# Deploy titan2 to the server (DETAIL §13.6). Run on the operator's machine, from the git checkout:
#   bash agentic_trader/ops/deploy.sh <host> [--allow-market-hours] [--restart]
# <host> is an ssh destination such as the `titan-bot` alias (user ubuntu). Needs git, tar, gzip, sha256sum, ssh,
# scp and a Python >= 3.10 with pytest (set PYTHON=... to choose one).
#
#  1. Local: titan2/, ops/ and requirements.lock are committed and clean, and `pytest -q` passes. REL is
#     `git rev-parse --short=12 HEAD`; release-REL.tgz is `git archive` of those paths plus MANIFEST.sha256 (every
#     file) and VERSION.
#  2. Remote preflight (ops/release.sh preflight, no sudo): the market-hours rule, drift of current (abort and list
#     the edited files; ops/pull_drift.sh copies them back), at least 1 GB free.
#  3. Upload to incoming/ (sha256 checked on the server), then ops/release.sh install: unpack, one venv per
#     requirements.lock hash (pip --require-hashes), compileall + chmod -R a-w, `selfcheck --phase deploy` as titan
#     on a fresh DB backup, the atomic switch of current, changed titan2-* units, restart of titan2-dash (always)
#     and titan2-bot (with --restart, or when it is not running) with a 120 s wait for READY and a fresh heartbeat,
#     a rollback to the previous release if that fails, and pruning.
#
# Exit codes: 0 deployed; 1 failed after the switch and rolled back (CRITICAL alert sent); 2 refused, nothing changed.
# The deploy writes nothing outside <root>/{incoming,releases,venvs,current} and /etc/systemd/system/titan2-*:
# never /var/lib/titan2, /etc/titan2, v1_frozen_* or agentic_trader/ (assert_owned in release.sh).
set -euo pipefail

usage() { echo "usage: $0 <host> [--allow-market-hours] [--restart]" >&2; exit 2; }
die() { printf '[titan2 deploy] REFUSED: %b\n' "$*" >&2; exit 2; }
log() { printf '[titan2 deploy] %s\n' "$*"; }

OPS_DIR=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
APP_DIR=$(cd -- "$OPS_DIR/.." && pwd)
REMOTE_ROOT=${TITAN2_ROOT:-/home/ubuntu/titan}     # override for tests/ops only

[ $# -ge 1 ] || usage
HOST=$1
shift
case $HOST in -* | "") usage ;; esac
FLAGS=()
for a in "$@"; do
    case $a in
        --allow-market-hours | --restart) FLAGS+=("$a") ;;
        *) usage ;;
    esac
done

pick_python() {
    local c cands=()
    if [ -n "${PYTHON:-}" ]; then cands+=("$PYTHON"); fi
    cands+=(python3 python)
    for c in "${cands[@]}"; do
        if "$c" -c 'import sys; sys.exit(sys.version_info < (3, 10))' >/dev/null 2>&1; then
            printf '%s\n' "$c"
            return 0
        fi
    done
    die "no Python >= 3.10 found (set PYTHON=/path/to/python)"
}

remote() {   # run ops/release.sh on the server with these arguments
    ssh -o BatchMode=yes -T "$HOST" "bash -s -- $*" < "$OPS_DIR/release.sh"
}

# --- 1. local checks and packaging ------------------------------------------------------------------------------
cd -- "$APP_DIR"
git rev-parse --is-inside-work-tree >/dev/null 2>&1 || die "$APP_DIR is not inside a git checkout"
PREFIX=$(git rev-parse --show-prefix)                 # e.g. "agentic_trader/"
DIRTY=$(git status --porcelain --untracked-files=all -- titan2 ops requirements.lock)
[ -z "$DIRTY" ] || die "the release is built from HEAD; commit or remove these first:\n$DIRTY"

PY=$(pick_python)
log "running the test suite: $PY -m pytest -q"
"$PY" -m pytest -q -p no:cacheprovider || die "pytest failed: nothing was deployed"

REL=$(git rev-parse --short=12 HEAD)
WORK=$(mktemp -d "${TMPDIR:-/tmp}/titan2-deploy.XXXXXX")
trap 'rm -rf -- "$WORK"' EXIT
STAGE="$WORK/release"
mkdir -p -- "$STAGE"
TREE=HEAD
if [ -n "$PREFIX" ]; then TREE="HEAD:${PREFIX%/}"; fi
TOP=$(git rev-parse --show-toplevel)
# From the top level, so the paths are relative to TREE (git prefixes pathspecs with the current subdirectory).
# core.autocrlf=false: the release holds the committed bytes, whatever this checkout's line-ending settings are.
git -C "$TOP" -c core.autocrlf=false archive --format=tar "$TREE" titan2 ops requirements.lock | tar -xf - -C "$STAGE" ||
    die "git archive of $TREE failed"
[ -f "$STAGE/requirements.lock" ] && [ -d "$STAGE/titan2" ] && [ -f "$STAGE/ops/release.sh" ] ||
    die "HEAD has no titan2/, ops/release.sh or requirements.lock"

UNSAFE=$(cd -- "$STAGE" && { find . ! -type d ! -type f; find . | grep -Ev '^\.(/[A-Za-z0-9._@+-]+)*$'; } || true)
[ -z "$UNSAFE" ] || die "a release may hold only plain files and directories with simple names; found:\n$UNSAFE"

# VERSION is a function of the commit alone, so a re-deploy of the same commit has an identical MANIFEST (reused).
printf 'rel=%s\ncommit=%s\ncommitted=%s\nlockhash=%s\n' "$REL" "$(git rev-parse HEAD)" \
    "$(git show -s --format=%cI HEAD)" "$(sha256sum "$STAGE/requirements.lock" | cut -c1-16)" > "$STAGE/VERSION"
# MANIFEST.sha256: "<sha256>  <path>" for every file, sorted; `sha256sum -c` reads it from the release directory.
(cd -- "$STAGE" && find . -type f ! -name MANIFEST.sha256 -print0 | LC_ALL=C sort -z | xargs -0 sha256sum --) |
    sed -E 's#^([0-9a-f]{64}) [ *]\./#\1  #' > "$WORK/MANIFEST.sha256"
mv -- "$WORK/MANIFEST.sha256" "$STAGE/MANIFEST.sha256"
TARBALL="$WORK/release-$REL.tgz"
tar -czf "$TARBALL" -C "$STAGE" .
SHA=$(sha256sum "$TARBALL" | cut -c1-64)
log "release $REL: $(wc -l < "$STAGE/MANIFEST.sha256") files, sha256 $SHA"

# --- 2. remote preflight ----------------------------------------------------------------------------------------
log "preflight on $HOST"
remote preflight "${FLAGS[@]}" || exit 2

# --- 3. upload, then steps 3-11 on the server ---------------------------------------------------------------------
log "uploading release-$REL.tgz to $HOST:$REMOTE_ROOT/incoming/"
scp -q -o BatchMode=yes "$TARBALL" "$HOST:$REMOTE_ROOT/incoming/release-$REL.tgz" || die "upload failed"
log "installing $REL on $HOST"
set +e
remote install "$REL" "$SHA" "${FLAGS[@]}"
RC=$?
set -e
case $RC in
    0) log "deployed $REL to $HOST" ;;
    1) log "deploy of $REL FAILED after the switch and was rolled back (see above)" ;;
    255) log "lost the ssh connection during the install: check the server (readlink current, releases/HISTORY)"
         RC=1 ;;
    *) log "deploy of $REL refused or failed before the switch: the server is unchanged"; RC=2 ;;
esac
exit "$RC"
