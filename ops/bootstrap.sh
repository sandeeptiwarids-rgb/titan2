#!/usr/bin/env bash
# Stage 1 bootstrap of the titan2 server (DETAIL §13.1, §13.11 Stage 1 step 1). Idempotent.
# DRY-RUN BY DEFAULT: it prints every change it would make (a unified diff for each file) and changes nothing;
# --execute applies them. Run it as root on the server, from a copy of ops/ (no release exists yet):
#   scp -r agentic_trader/ops titan-bot:/tmp/titan2-ops
#   ssh titan-bot 'sudo bash /tmp/titan2-ops/bootstrap.sh'             # review the plan
#   ssh titan-bot 'sudo bash /tmp/titan2-ops/bootstrap.sh --execute'   # apply it
# It:
#   - requires Python >= 3.10 and installs python3-venv and chrony when missing (deadsnakes stays manual [V]);
#   - creates the system users titan (home /var/lib/titan2) and titandash, without login shells;
#   - creates the directories, owners and modes of DETAIL §13.1 and sets /home/ubuntu to 0751;
#   - creates /home/ubuntu/titan/agentic_trader/titan2 -> ../current/titan2 (refused while v1's tree is there);
#   - installs /usr/local/bin/titan2, /etc/sudoers.d/titan2 (visudo-checked), /etc/logrotate.d/titan2, the journald
#     cap and the Amazon Time Sync source for chrony, and sets the timezone to Asia/Kolkata.
# It never writes /etc/titan2/*.env (written by hand from ops/*.env.example), never installs or enables a unit
# (deploy installs them; Stage 1 step 6 enables them), never touches v1_frozen_* or anything inside the state dirs.
# Exit codes: 0 done or planned; 2 refused.
set -euo pipefail
shopt -s nullglob

OPS=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
SYS=${TITAN2_SYSROOT:-}      # tests/ops only: a prefix for every system path
EXECUTE=0
case ${1:-} in
    --execute) EXECUTE=1 ;;
    "") ;;
    *) echo "usage: $0 [--execute]" >&2; exit 2 ;;
esac
CHANGES=0

log() { printf '[bootstrap] %s\n' "$*"; }
die() { printf '[bootstrap] REFUSED: %s\n' "$*" >&2; exit 2; }
plan() {   # plan "what" command...: print it, and run it only with --execute
    CHANGES=$((CHANGES + 1))
    if (( EXECUTE )); then
        printf 'APPLY: %s\n' "$1"
        shift
        "$@"
    else
        printf 'PLAN: %s\n' "$1"
    fi
}

if [ -z "$SYS" ] && [ "$(id -u)" != 0 ]; then die "run it as root: sudo bash $0 ${1:-}"; fi
log "$( (( EXECUTE )) && echo EXECUTE || echo 'DRY RUN (nothing changes; --execute applies)')"

# --- Python and packages -----------------------------------------------------------------------------------------
python3 -c 'import sys; sys.exit(sys.version_info < (3, 10))' 2>/dev/null ||
    die "python3 is missing or older than 3.10. Ubuntu 22.04+ ships 3.10+; elsewhere install python3.10 and python3.10-venv (deadsnakes PPA [V]) so that python3.10 is on PATH, then re-run"
PKGS=()
dpkg -s python3-venv >/dev/null 2>&1 || PKGS+=(python3-venv)
dpkg -s chrony >/dev/null 2>&1 || PKGS+=(chrony)
if [ "${#PKGS[@]}" -gt 0 ]; then plan "apt-get install ${PKGS[*]}" apt-get install -y "${PKGS[@]}"; fi

# --- users ---------------------------------------------------------------------------------------------------------
ensure_user() {   # NAME HOME
    if id -u "$1" >/dev/null 2>&1; then
        log "user $1 exists"
    else
        plan "create the system user $1 (home $2, no login shell)" \
            useradd --system --user-group --home-dir "$2" --no-create-home --shell /usr/sbin/nologin "$1"
    fi
}
ensure_user titan /var/lib/titan2
ensure_user titandash /nonexistent
for pair in "titandash titan" "titan titandash"; do
    u=${pair% *}
    g=${pair#* }
    case " $(id -nG "$u" 2>/dev/null || true) " in
        *" $g "*) log "WARNING: user $u is in group $g; remove it: gpasswd -d $u $g (the two users stay separate)" ;;
    esac
done
for u in titan titandash; do                 # an account left over from v1's setup_server.sh may be privileged
    groups=" $(id -nG "$u" 2>/dev/null || true) "
    for g in sudo admin wheel adm docker lxd; do
        case $groups in *" $g "*) log "WARNING: user $u is in the privileged group $g; remove it: gpasswd -d $u $g" ;; esac
    done
done

# --- directories (DETAIL §13.1) -------------------------------------------------------------------------------------
set_dir() { mkdir -p -- "$1" && chown "$2:$3" -- "$1" && chmod "$4" -- "$1"; }
ensure_dir() {   # PATH OWNER GROUP MODE
    local p="$SYS$1" want="$2:$3 $4" have="(missing)"
    if [ -d "$p" ]; then have=$(stat -c '%U:%G %a' -- "$p"); fi
    if [ "$have" != "$want" ]; then plan "directory $1: $have -> $want" set_dir "$p" "$2" "$3" "$4"; fi
}
if [ -d "$SYS/home/ubuntu" ] && [ "$(stat -c '%a' -- "$SYS/home/ubuntu")" != 751 ]; then
    plan "chmod 0751 /home/ubuntu (titan may traverse it; nothing becomes readable)" chmod 0751 -- "$SYS/home/ubuntu"
fi
while read -r path owner group mode; do
    ensure_dir "$path" "$owner" "$group" "$mode"
done <<'EOF'
/home/ubuntu/titan ubuntu ubuntu 755
/home/ubuntu/titan/agentic_trader ubuntu ubuntu 755
/home/ubuntu/titan/releases ubuntu ubuntu 755
/home/ubuntu/titan/venvs ubuntu ubuntu 755
/home/ubuntu/titan/incoming ubuntu ubuntu 750
/etc/titan2 root titan 750
/var/lib/titan2 titan titandash 710
/var/lib/titan2/db titan titan 700
/var/lib/titan2/dash titandash titan 2750
/var/lib/titan2/pub titan titandash 2750
/var/lib/titan2/session titan titan 700
/var/lib/titan2/config titan titan 750
/var/lib/titan2/config/ban_override titan titan 750
/var/lib/titan2/cache titan titan 750
/var/lib/titan2/run titan titan 750
/var/lib/titan2/reports titan titan 750
/var/lib/titan2/exports titan titan 750
/var/lib/titan2/backups titan titan 700
/var/log/titan2 titan titan 750
/var/backups/titan2 titan titan 700
EOF

# --- the WorkingDirectory: only the titan2 symlink --------------------------------------------------------------------
APP="$SYS/home/ubuntu/titan/agentic_trader"
if [ -d "$APP" ]; then
    OTHER=$(cd -- "$APP" && ls -A | grep -vx titan2 || true)
    if [ -n "$OTHER" ]; then
        OTHER=$(printf '%s ' $OTHER)
        die "$APP holds more than the titan2 symlink (v1's tree?): ${OTHER:0:300}. Run ops/stage0_cutover.sh first"
    fi
fi
if [ -L "$APP/titan2" ] && [ "$(readlink -- "$APP/titan2")" = ../current/titan2 ]; then
    log "symlink agentic_trader/titan2 -> ../current/titan2 exists"
else
    plan "symlink /home/ubuntu/titan/agentic_trader/titan2 -> ../current/titan2" ln -sfn ../current/titan2 "$APP/titan2"
fi

# --- files ---------------------------------------------------------------------------------------------------------
put_file() {   # SRC DEST MODE OWNER:GROUP: write a temporary file next to DEST, then rename it over DEST
    mkdir -p -- "${2%/*}"
    install -m "$3" -- "$1" "$2.titan2-new"
    chown "$4" -- "$2.titan2-new"
    mv -f -- "$2.titan2-new" "$2"
}
CHANGED_FILES=" "
ensure_file() {   # SRC (in ops/) DEST MODE OWNER:GROUP
    local src="$OPS/$1" dest="$SYS$2" have
    if [ -f "$dest" ] && cmp -s -- "$src" "$dest"; then
        have=$(stat -c '%U:%G %a' -- "$dest")
        if [ "$have" = "$4 $3" ]; then return 0; fi
        log "$2: content ok, owner/mode $have -> $4 $3"
    elif [ -f "$dest" ]; then
        diff -u -- "$dest" "$src" || true
    else
        printf -- '--- %s (new file)\n' "$2"
        sed 's/^/+/' -- "$src"
    fi
    plan "install ops/$1 -> $2 ($4 0$3)" put_file "$src" "$dest" "$3" "$4"
    CHANGED_FILES+="$2 "
}
command -v visudo >/dev/null 2>&1 || die "visudo is missing (package sudo)"
visudo -cf "$OPS/sudoers.titan2" >/dev/null || die "ops/sudoers.titan2 does not pass visudo -c"
ensure_file titan2-cli /usr/local/bin/titan2 755 root:root
ensure_file sudoers.titan2 /etc/sudoers.d/titan2 440 root:root
ensure_file logrotate.titan2 /etc/logrotate.d/titan2 644 root:root
ensure_file journald-titan2.conf /etc/systemd/journald.conf.d/titan2.conf 644 root:root
ensure_file chrony-aws.sources /etc/chrony/sources.d/titan2-aws.sources 644 root:root
case $CHANGED_FILES in
    *" /etc/systemd/journald.conf.d/titan2.conf "*) plan "restart systemd-journald (the 200M cap)" systemctl restart systemd-journald ;;
esac
case $CHANGED_FILES in
    *" /etc/chrony/sources.d/titan2-aws.sources "*) plan "chronyc reload sources" chronyc reload sources ;;
esac

# --- timezone ------------------------------------------------------------------------------------------------------
TZNOW=$(timedatectl show -p Timezone --value 2>/dev/null || true)
if [ "$TZNOW" != Asia/Kolkata ]; then plan "timezone ${TZNOW:-?} -> Asia/Kolkata" timedatectl set-timezone Asia/Kolkata; fi

# --- summary ---------------------------------------------------------------------------------------------------------
if grep -Eqs '^[[:space:]]*ubuntu[[:space:]].*NOPASSWD:[[:space:]]*ALL' "$SYS/etc/sudoers" "$SYS"/etc/sudoers.d/*; then
    cat <<'EOF'
[bootstrap] NOTE: user ubuntu still has passwordless sudo for everything (cloud-init's default). The guarantee
  "ubuntu cannot write /var/lib/titan2 or /etc/titan2" holds only once that grant needs a password:
    sudo passwd ubuntu
    sudoedit /etc/sudoers.d/90-cloud-init-users     # "ubuntu ALL=(ALL) NOPASSWD:ALL" -> "ubuntu ALL=(ALL) ALL"
  Keep a second session open while you change it. The titan2 allow-list (/etc/sudoers.d/titan2) stays NOPASSWD.
EOF
fi
if (( CHANGES == 0 )); then
    log "nothing to do: the server already matches"
elif (( EXECUTE )); then
    log "applied $CHANGES change(s)"
else
    log "$CHANGES change(s) planned; re-run with --execute to apply them"
fi
cat <<'EOF'
[bootstrap] next (DETAIL 13.11, Stage 1):
  2. write /etc/titan2/titan2.env and /etc/titan2/dash.env by hand from ops/titan2.env.example and ops/dash.env.example
  3. from the laptop: agentic_trader/ops/deploy.sh titan-bot
  4. titan2 token            5. titan2 selfcheck --phase cutover
  6. sudo systemctl enable titan2-bot.service titan2-dash.service titan2-bot.timer titan2-token.timer \
       titan2-selfcheck.timer titan2-backup.timer && sudo systemctl start titan2-dash titan2-bot.timer \
       titan2-token.timer titan2-selfcheck.timer titan2-backup.timer
EOF
