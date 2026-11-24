#!/usr/bin/env bash
# aprs_ctl.sh - run, stop and supervise the APRS object services.
#
#   ./aprs_ctl.sh init                 create aprs.env (settings) and the data/run/logs folders
#   ./aprs_ctl.sh start   [service]    start the supervisor (and all services), or one service
#   ./aprs_ctl.sh stop    [service]    stop everything (supervisor too), or one service
#   ./aprs_ctl.sh restart [service]    stop, then start
#   ./aprs_ctl.sh status               table of services; exit code 1 if any is not healthy
#   ./aprs_ctl.sh logs [service] [-f]  show (or follow) logs; no service = supervisor log
#   ./aprs_ctl.sh monitor              run the supervisor in the foreground (for systemd/tmux)
#   ./aprs_ctl.sh symbols              add the APRS symbol settings to an existing aprs.env
#
# Changes to aprs.env take effect the next time a service starts (restart <service>).
# Services: send montco chesco bucks nj511 pa511 nws web   (web = map/editor on WEB_PORT, default 8080)
#   send = aprs_send.py, the one program that logs in to APRS-IS; the others hand their packets to it.
#   It is always enabled, because without it nothing reaches APRS-IS.
#
# The supervisor is the only thing that starts or kills service processes, so
# commands typed here and the auto-restarter can never fight each other.
# A crashed service is restarted with back-off (5s, 10s, 20s ... up to 5 min).
# A service that crashes MAX_CRASHES times within CRASH_WINDOW seconds, or that
# dies from an obvious configuration problem (bad API key, bad login, robots.txt
# refusal), is marked FAILED and left alone until you run "start <service>".

set -u
umask 077

SELF="$(readlink -f "${BASH_SOURCE[0]}")"
BASE="$(dirname "$SELF")"
CONF="${APRS_CONF:-$BASE/aprs.env}"
RUN="${APRS_RUN_DIR:-$BASE/run}"
LOGS="${APRS_LOG_DIR:-$BASE/logs}"
DATA="${APRS_DATA_DIR:-$BASE/data}"
SERVICES=(send montco chesco bucks nj511 pa511 nws web)   # send first: the others hand packets to it
RETIRED=(njta)                 # old services: stopped if still running, never started

# ---- defaults (environment variables win over these; aprs.env wins over both) ---------------------------------------
PYTHON="${PYTHON:-python3}"
CALLSIGN="${CALLSIGN:-N0CALL}"          # your real callsign-SSID; required when SEND=1
SEND="${SEND:-0}"                     # 0 = print packets only, 1 = send to APRS-IS
COMMENT_LENGTH="${COMMENT_LENGTH:-67}"          # use 43 if clients truncate/drop objects
ENABLED="${ENABLED:-send montco chesco bucks nj511 pa511 nws web}"
INTERVAL_send="${INTERVAL_send:-0}"
INTERVAL_montco="${INTERVAL_montco:-120}"
INTERVAL_chesco="${INTERVAL_chesco:-120}"
INTERVAL_bucks="${INTERVAL_bucks:-120}"
INTERVAL_nj511="${INTERVAL_nj511:-120}"
INTERVAL_pa511="${INTERVAL_pa511:-120}"
INTERVAL_nws="${INTERVAL_nws:-120}"
INTERVAL_web="${INTERVAL_web:-0}"
EXTRA_send="${EXTRA_send:-}"              # e.g. --resend-minutes 26
EXTRA_montco="${EXTRA_montco:-}"            # extra arguments appended for that service
EXTRA_chesco="${EXTRA_chesco:-}"
EXTRA_bucks="${EXTRA_bucks:-}"
EXTRA_nj511="${EXTRA_nj511:-}"
EXTRA_pa511="${EXTRA_pa511:-}"
EXTRA_nws="${EXTRA_nws:-}"
EXTRA_web="${EXTRA_web:-}"
WEB_PORT="${WEB_PORT:-8080}"               # web UI port
CHECK_EVERY="${CHECK_EVERY:-5}"              # seconds between health checks
MAX_CRASHES="${MAX_CRASHES:-5}"              # this many crashes ...
CRASH_WINDOW="${CRASH_WINDOW:-600}"           # ... within this many seconds => FAILED
BACKOFF_MIN="${BACKOFF_MIN:-5}"
BACKOFF_MAX="${BACKOFF_MAX:-300}"
HEALTHY_AFTER="${HEALTHY_AFTER:-120}"          # running this long resets the back-off
STALL_SECS="${STALL_SECS:-0}"               # >0: restart a service whose log is silent this long (0 = off)
STOP_GRACE="${STOP_GRACE:-10}"              # seconds to wait after SIGTERM before SIGKILL
LOG_STDOUT="${LOG_STDOUT:-0}"   # 1 = also copy service output to the supervisor's stdout (Docker)
LOG_MAX_KB="${LOG_MAX_KB:-5120}"            # rotate a log when it exceeds this size (keeps 3 old copies)
GOOGLE_MAPS_API_KEY="${GOOGLE_MAPS_API_KEY:-}"
PENNDOT_RCRS_USER="${PENNDOT_RCRS_USER:-}"
PENNDOT_RCRS_PASSWORD="${PENNDOT_RCRS_PASSWORD:-}"
NWS_CONTACT="${NWS_CONTACT:-}"             # e-mail/website for the NWS User-Agent (nws service)
APRS_HOME="${APRS_HOME:-}"               # lat,lon centre for the nws service (blank = script default)

load_conf() {  # read aprs.env (again); called at start and before every service launch
    # Windows line endings in aprs.env (edited on a PC) would end up inside every value: drop them.
    if [ -f "$CONF" ]; then
        local _err
        _err="$(bash -n <(tr -d '\r' < "$CONF") 2>&1)"
        if [ -n "$_err" ]; then                    # report it against aprs.env, not /dev/fd/NN
            printf '%s\n' "$_err" | sed "s|/dev/fd/[0-9]*|$CONF|g; s|^|aprs.env problem: |" >&2
            echo "aprs.env problem: settings after that line are NOT being used - fix the line, then: $0 restart" >&2
            [ -n "${FOREGROUND:-}${_IN_SUPERVISOR:-}" ] && echo "$(date '+%Y-%m-%d %H:%M:%S') $(printf '%s' "$_err" | sed "s|/dev/fd/[0-9]*|$CONF|g" | head -1)" >> "$LOGS/supervisor.log" 2>/dev/null
        fi
        . <(tr -d '\r' < "$CONF") 2>/dev/null
    fi
    export GOOGLE_MAPS_API_KEY PENNDOT_RCRS_USER PENNDOT_RCRS_PASSWORD NWS_CONTACT WEB_PORT
    export APRS_DATA_DIR="$DATA" APRS_RUN_DIR="$RUN" APRS_LOG_DIR="$LOGS"
    for _v in "${!SYM_@}"; do export "$_v"; done                 # APRS symbol choices (see: aprs_ctl.sh symbols)
    [ -n "${EINK_SYMBOLS:-}" ] && export EINK_SYMBOLS            # 0 = maps never download the symbol pictures
    [ -n "$APRS_HOME" ] && export APRS_HOME
    [ -n "${APRS_LISTEN:-}" ] && export APRS_LISTEN              # 0 = web map does not listen to APRS-IS
    [ -n "${APRS_LISTEN_MILES:-}" ] && export APRS_LISTEN_MILES  # radius for other people's objects (default 50)
    [ -n "${APRS_TRAIL_MINUTES:-}" ] && export APRS_TRAIL_MINUTES  # how much path history to keep (default 180)
    [ -n "${APRS_BACKFILL:-}" ] && export APRS_BACKFILL            # 0 = never fetch recent APRS history at start
    [ -n "${EINK_OSM:-}" ] && export EINK_OSM                    # 0 = no OpenStreetMap roads/rivers on /eink.png
    ENABLED="${ENABLED//njta/nj511}"          # aprs_njta.py was retired: 511NJ covers the Turnpike and Parkway
    case " $ENABLED " in *" send "*) ;; *) ENABLED="send $ENABLED" ;; esac   # an aprs.env from before aprs_send.py existed
}
load_conf

script_for() {
    case "$1" in
        send)   echo aprs_send.py   ;;
        montco) echo aprs_montco.py ;; chesco) echo aprs_chesco.py ;;
        bucks)  echo aprs_bucks.py  ;;
        nj511)  echo aprs_nj511.py  ;; pa511)  echo aprs_pa511.py  ;;
        njta)   echo aprs_njta.py   ;;
        nws)    echo aprs_nws.py    ;; web)    echo aprs_web.py    ;;
    esac
}
valid_service() { local s; for s in "${SERVICES[@]}"; do [ "$s" = "$1" ] && return 0; done; return 1; }
now() { date +%s; }
stamp() { date '+%Y-%m-%d %H:%M:%S'; }
die() { echo "$*" >&2; exit 1; }
alive() { [ -n "${1:-}" ] && kill -0 "$1" 2>/dev/null; }
readpid() { [ -f "$1" ] && cat "$1" 2>/dev/null || true; }

mkdirs() { mkdir -p "$RUN" "$LOGS" "$DATA"; }

# ============================ supervisor =====================================
sup_log() { echo "$(stamp) $*" >> "$LOGS/supervisor.log"; [ -n "${FOREGROUND:-}" ] && echo "$(stamp) $*"; return 0; }

rotate_log() {  # rotate_log file
    local f="$1" kb
    [ -f "$f" ] || return 0
    kb=$(( $(stat -c %s "$f") / 1024 ))
    [ "$kb" -lt "$LOG_MAX_KB" ] && return 0
    mv -f "$f.2" "$f.3" 2>/dev/null; mv -f "$f.1" "$f.2" 2>/dev/null; mv -f "$f" "$f.1"
    : > "$f"
}

declare -A PID STARTED NEXT_TRY BACKOFF CRASHES STATE NOTE RESTARTS
TAILERS=()

write_state() {  # write_state svc  -> run/svc.state (read by "status")
    local s="$1"
    printf 'state=%s\npid=%s\nstarted=%s\nrestarts=%s\nnote=%s\nnext=%s\n' \
        "${STATE[$s]}" "${PID[$s]:-}" "${STARTED[$s]:-0}" "${RESTARTS[$s]:-0}" \
        "${NOTE[$s]:-}" "${NEXT_TRY[$s]:-0}" > "$RUN/$s.state.tmp"
    mv -f "$RUN/$s.state.tmp" "$RUN/$s.state"
}

config_problem() {  # config_problem svc -> prints a reason if it can't run
    local s="$1"
    if [ "$SEND" = 1 ] && { [ -z "$CALLSIGN" ] || [[ "${CALLSIGN^^}" == N0CALL* ]]; }; then
        echo "SEND=1 needs a real CALLSIGN in aprs.env"; return
    fi
    case "$s" in
        montco|chesco) [ -z "$GOOGLE_MAPS_API_KEY" ] && echo "GOOGLE_MAPS_API_KEY not set in aprs.env" ;;
        pa511) { [ -z "$PENNDOT_RCRS_USER" ] || [ -z "$PENNDOT_RCRS_PASSWORD" ]; } &&
               echo "PENNDOT_RCRS_USER / PENNDOT_RCRS_PASSWORD not set in aprs.env" ;;
        nws) [ -z "$NWS_CONTACT" ] && echo "NWS_CONTACT not set in aprs.env" ;;
    esac
    [ -f "$BASE/$(script_for "$s")" ] || echo "$(script_for "$s") not found next to aprs_ctl.sh"
}

launch() {  # launch svc
    load_conf                     # pick up any change to aprs.env since the supervisor started
    local s="$1" iv=INTERVAL_$1 ex=EXTRA_$1 args
    local -a argv
    argv=(-u "$BASE/$(script_for "$s")" --callsign "$CALLSIGN" --interval "${!iv}" --comment-length "$COMMENT_LENGTH")
    [ "$SEND" = 1 ] && argv+=(--send)
    # shellcheck disable=SC2206
    argv+=(${!ex})
    rotate_log "$LOGS/$s.log"
    echo "$(stamp) --- supervisor: starting $s ---" >> "$LOGS/$s.log"
    ( cd "$DATA" && exec "$PYTHON" "${argv[@]}" >> "$LOGS/$s.log" 2>&1 ) &
    PID[$s]=$!
    STARTED[$s]=$(now)
    echo "${PID[$s]}" > "$RUN/$s.pid"
    STATE[$s]=RUNNING; NOTE[$s]=""
    sup_log "$s started (pid ${PID[$s]})"
}

kill_service() {  # kill_service svc : TERM, wait, KILL
    local s="$1" p="${PID[$1]:-}" i
    if alive "$p"; then
        kill "$p" 2>/dev/null
        for ((i = 0; i < STOP_GRACE * 5; i++)); do alive "$p" || break; sleep 0.2; done
        alive "$p" && { kill -9 "$p" 2>/dev/null; sup_log "$s did not exit in ${STOP_GRACE}s, killed"; }
        wait "$p" 2>/dev/null
    fi
    rm -f "$RUN/$s.pid"
    PID[$s]=""
}

FATAL_RE='REQUEST_DENIED|robots\.txt|disallows automated|API key|credentials|unauthori[sz]ed|HTTP Error 401|not set'

on_exit_of() {  # on_exit_of svc : service process is gone
    local s="$1" rc=0 up t fatal="" win_start c
    wait "${PID[$s]}" 2>/dev/null; rc=$?          # 127 if it was adopted, not our child
    [ "$rc" = 127 ] && rc="?"
    t=$(now); up=$(( t - ${STARTED[$s]:-$t} ))
    rm -f "$RUN/$s.pid"; PID[$s]=""
    if tail -n 6 "$LOGS/$s.log" 2>/dev/null | grep -Eqi "$FATAL_RE" && [ "$rc" != 0 ]; then
        fatal=$(tail -n 6 "$LOGS/$s.log" | grep -Ei "$FATAL_RE" | tail -n1 | cut -c1-120)
    fi
    if [ -n "$fatal" ]; then
        STATE[$s]=FAILED; NOTE[$s]="config/auth problem: $fatal"
        sup_log "$s exited (rc=$rc) with a configuration problem - NOT restarting: $fatal"
        return
    fi
    # crash-loop accounting: crashes within CRASH_WINDOW
    CRASHES[$s]="${CRASHES[$s]:-} $t"
    c=0; local keep="" x
    for x in ${CRASHES[$s]}; do
        if [ $(( t - x )) -le "$CRASH_WINDOW" ]; then keep+=" $x"; c=$((c + 1)); fi
    done
    CRASHES[$s]="$keep"
    RESTARTS[$s]=$(( ${RESTARTS[$s]:-0} + 1 ))
    if [ "$c" -ge "$MAX_CRASHES" ]; then
        STATE[$s]=FAILED; NOTE[$s]="crashed $c times in ${CRASH_WINDOW}s"
        sup_log "$s crashed $c times in ${CRASH_WINDOW}s (rc=$rc) - giving up; use 'start $s' after fixing"
        return
    fi
    if [ "$up" -ge "$HEALTHY_AFTER" ]; then BACKOFF[$s]=$BACKOFF_MIN
    else BACKOFF[$s]=$(( ${BACKOFF[$s]:-$BACKOFF_MIN / 2} * 2 )); fi
    [ "${BACKOFF[$s]}" -lt "$BACKOFF_MIN" ] && BACKOFF[$s]=$BACKOFF_MIN
    [ "${BACKOFF[$s]}" -gt "$BACKOFF_MAX" ] && BACKOFF[$s]=$BACKOFF_MAX
    NEXT_TRY[$s]=$(( t + BACKOFF[$s] ))
    STATE[$s]=BACKOFF; NOTE[$s]="exit rc=$rc after ${up}s; retry in ${BACKOFF[$s]}s"
    sup_log "$s died (rc=$rc, up ${up}s) - restart in ${BACKOFF[$s]}s [crash $c/$MAX_CRASHES]"
}

is_enabled() { local s; for s in $ENABLED; do [ "$s" = "$1" ] && return 0; done; return 1; }

reconcile() {  # one pass over all services
    local s t p reason age mt
    t=$(now)
    for s in "${SERVICES[@]}"; do
        # --- operator holds: "stop <svc>" ---
        if [ -f "$RUN/$s.hold" ] || ! is_enabled "$s"; then
            if [ -n "${PID[$s]:-}" ]; then kill_service "$s"; sup_log "$s stopped"; fi
            STATE[$s]=STOPPED; NOTE[$s]=$([ -f "$RUN/$s.hold" ] && echo "stopped by operator" || echo "not in ENABLED")
            write_state "$s"; continue
        fi
        # --- "start <svc>" clears FAILED ---
        if [ -f "$RUN/$s.retry" ]; then
            rm -f "$RUN/$s.retry"; CRASHES[$s]=""; BACKOFF[$s]=$BACKOFF_MIN; NEXT_TRY[$s]=0
            [ "${STATE[$s]:-}" = FAILED ] && sup_log "$s: cleared FAILED by operator"
            STATE[$s]=""
        fi
        [ "${STATE[$s]:-}" = FAILED ] && { write_state "$s"; continue; }
        p="${PID[$s]:-}"
        if [ -n "$p" ]; then
            if ! alive "$p"; then
                on_exit_of "$s"
            elif [ "$STALL_SECS" -gt 0 ] && [ -f "$LOGS/$s.log" ]; then
                mt=$(stat -c %Y "$LOGS/$s.log"); age=$(( t - mt ))
                if [ "$age" -gt "$STALL_SECS" ] && [ $(( t - STARTED[$s] )) -gt "$STALL_SECS" ]; then
                    sup_log "$s silent for ${age}s (stall limit ${STALL_SECS}s) - killing for restart"
                    kill_service "$s"; touch "$LOGS/$s.log"
                    NEXT_TRY[$s]=$t; STATE[$s]=BACKOFF; NOTE[$s]="restarted after stall"
                    RESTARTS[$s]=$(( ${RESTARTS[$s]:-0} + 1 ))
                fi
            elif [ "${STATE[$s]:-}" = RUNNING ] && [ $(( t - STARTED[$s] )) -ge "$HEALTHY_AFTER" ]; then
                BACKOFF[$s]=$BACKOFF_MIN
            fi
        fi
        if [ -z "${PID[$s]:-}" ] && [ "${STATE[$s]:-}" != FAILED ]; then
            if [ "$t" -ge "${NEXT_TRY[$s]:-0}" ]; then
                reason="$(config_problem "$s")"
                if [ -n "$reason" ]; then
                    STATE[$s]=FAILED; NOTE[$s]="$reason"; sup_log "$s not started: $reason"
                else
                    launch "$s"
                fi
            fi
        fi
        write_state "$s"
    done
}

supervisor_cleanup() {
    trap '' TERM INT HUP
    sup_log "supervisor stopping - stopping services"
    local s
    for s in "${SERVICES[@]}"; do
        [ -n "${PID[$s]:-}" ] && kill "${PID[$s]}" 2>/dev/null
    done
    for s in "${SERVICES[@]}"; do
        [ -n "${PID[$s]:-}" ] && kill_service "$s"
        STATE[$s]=STOPPED; NOTE[$s]="supervisor stopped"; write_state "$s"
    done
    rm -f "$RUN/supervisor.pid"
    sup_log "supervisor stopped"
    [ "${#TAILERS[@]}" -gt 0 ] && { sleep 0.5; kill "${TAILERS[@]}" 2>/dev/null; }
    exit 0
}

adopt_running() {  # after a supervisor crash/kill -9, pick up services still running
    local s p
    for s in "${SERVICES[@]}"; do
        p="$(readpid "$RUN/$s.pid")"
        if alive "$p" && tr '\0' ' ' < "/proc/$p/cmdline" 2>/dev/null | grep -q "$(script_for "$s")"; then
            PID[$s]=$p; STARTED[$s]=$(now); STATE[$s]=RUNNING
            sup_log "adopted running $s (pid $p)"
        else
            rm -f "$RUN/$s.pid"
        fi
    done
    for s in "${RETIRED[@]}"; do                 # a retired service left running by an older supervisor
        p="$(readpid "$RUN/$s.pid")"
        if alive "$p"; then kill "$p" 2>/dev/null; sup_log "stopped retired service $s (pid $p)"; fi
        rm -f "$RUN/$s.pid" "$RUN/$s.state"
    done
}

supervisor_main() {
    mkdirs
    local old; old="$(readpid "$RUN/supervisor.pid")"
    if alive "$old" && [ "$old" != "$$" ]; then echo "supervisor already running (pid $old)"; exit 0; fi
    echo $$ > "$RUN/supervisor.pid"
    trap supervisor_cleanup TERM INT HUP
    trap ':' USR1
    sup_log "supervisor started (pid $$), services: $ENABLED, send=$SEND, callsign=$CALLSIGN"
    adopt_running
    TAILERS=()
    if [ "$LOG_STDOUT" = 1 ]; then      # Docker: make "docker logs" show every service's output
        local s
        for s in "${SERVICES[@]}"; do
            is_enabled "$s" || continue
            touch "$LOGS/$s.log"
            tail -n 0 -F "$LOGS/$s.log" 2>/dev/null > >(sed -u "s/^/[$s] /") &
            TAILERS+=($!)
        done
    fi
    local sl
    while :; do
        rotate_log "$LOGS/supervisor.log"
        reconcile
        sleep "$CHECK_EVERY" & sl=$!
        wait "$sl" 2>/dev/null
        kill "$sl" 2>/dev/null; wait "$sl" 2>/dev/null
    done
}

# ============================ commands =======================================
sup_pid() { local p; p="$(readpid "$RUN/supervisor.pid")"; alive "$p" && echo "$p"; }
poke() { local p; p="$(sup_pid)"; [ -n "$p" ] && kill -USR1 "$p" 2>/dev/null; return 0; }

ensure_conf() {
    [ -f "$CONF" ] || [ "${APRS_ENV_CONFIG:-0}" = 1 ] || die "No $CONF yet. Run: $0 init   then edit it."
}

start_supervisor() {
    mkdirs
    local p; p="$(sup_pid)"
    if [ -n "$p" ]; then poke; return 0; fi
    ( cd "$BASE" && APRS_CONF="$CONF" exec setsid nohup "$SELF" _daemon ) >/dev/null 2>&1 < /dev/null &
    disown 2>/dev/null || true
    local i
    for ((i = 0; i < 50; i++)); do [ -n "$(sup_pid)" ] && return 0; sleep 0.1; done
    die "supervisor did not start; see $LOGS/supervisor.log"
}

cmd_start() {
    ensure_conf; mkdirs
    local s="${1:-}"
    if [ -n "$s" ]; then
        valid_service "$s" || die "unknown service '$s' (choose: ${SERVICES[*]})"
        rm -f "$RUN/$s.hold"; touch "$RUN/$s.retry"
    else
        local x; for x in "${SERVICES[@]}"; do rm -f "$RUN/$x.hold"; touch "$RUN/$x.retry"; done
    fi
    if [ -n "$(sup_pid)" ]; then poke; echo "supervisor already running; asked it to start ${s:-all services}"
    else start_supervisor; echo "supervisor started (pid $(sup_pid))"; fi
    sleep 1.5; cmd_status || true
}

wait_gone() { local p="$1" i; for ((i = 0; i < (STOP_GRACE + 5) * 5; i++)); do alive "$p" || return 0; sleep 0.2; done; return 1; }

cmd_stop() {
    mkdirs
    local s="${1:-}" p
    if [ -n "$s" ]; then
        valid_service "$s" || die "unknown service '$s' (choose: ${SERVICES[*]})"
        touch "$RUN/$s.hold"
        if [ -n "$(sup_pid)" ]; then
            poke; local i; for ((i = 0; i < 100; i++)); do
                grep -q '^state=STOPPED' "$RUN/$s.state" 2>/dev/null && break; sleep 0.2; done
            echo "$s stopped (supervisor will not restart it until you run: $0 start $s)"
        else
            p="$(readpid "$RUN/$s.pid")"; alive "$p" && { kill "$p"; wait_gone "$p" || kill -9 "$p"; }
            rm -f "$RUN/$s.pid"; echo "$s stopped"
        fi
        return 0
    fi
    p="$(sup_pid)"
    if [ -n "$p" ]; then
        kill -TERM "$p"; wait_gone "$p" && echo "supervisor and all services stopped" || { kill -9 "$p"; echo "supervisor force-killed"; }
    else echo "supervisor not running"; fi
    # sweep any orphans (supervisor died uncleanly)
    local x q
    for x in "${SERVICES[@]}" "${RETIRED[@]}"; do
        q="$(readpid "$RUN/$x.pid")"
        if alive "$q"; then kill "$q" 2>/dev/null; wait_gone "$q" || kill -9 "$q" 2>/dev/null; echo "stopped orphan $x (pid $q)"; fi
        rm -f "$RUN/$x.pid" "$RUN/$x.hold" "$RUN/$x.retry"
    done
    return 0
}

fmt_dur() { local s=$1; if [ "$s" -ge 86400 ]; then echo "$((s/86400))d$((s%86400/3600))h"
    elif [ "$s" -ge 3600 ]; then echo "$((s/3600))h$((s%3600/60))m"
    elif [ "$s" -ge 60 ]; then echo "$((s/60))m$((s%60))s"; else echo "${s}s"; fi; }

cmd_status() {
    mkdirs
    local sp bad=0 s t=$(now) state pid started restarts note next up
    sp="$(sup_pid)"
    if [ -n "$sp" ]; then echo "supervisor: running (pid $sp)"; else echo "supervisor: NOT RUNNING"; bad=1; fi
    printf '%-8s %-9s %-8s %-8s %-9s %s\n' SERVICE STATE PID UPTIME RESTARTS NOTE
    for s in "${SERVICES[@]}"; do
        state=""; pid=""; started=0; restarts=0; note=""; next=0
        if [ -f "$RUN/$s.state" ]; then
            # shellcheck disable=SC1090
            while IFS='=' read -r k v; do
                case "$k" in state) state=$v;; pid) pid=$v;; started) started=$v;; restarts) restarts=$v;;
                             note) note=$v;; next) next=$v;; esac
            done < "$RUN/$s.state"
        fi
        [ -z "$sp" ] && [ "$state" != STOPPED ] && state="${state:-?}(stale)"
        up="-"
        if [ "$state" = RUNNING ] && alive "$pid"; then up="$(fmt_dur $((t - started)))"
        else pid="-"; fi
        if ! is_enabled "$s" && [ ! -f "$RUN/$s.state" ]; then state=DISABLED; fi
        [ "$state" = BACKOFF ] && note="$note"
        case "$state" in RUNNING|STOPPED|DISABLED) ;; *) bad=1;; esac
        [ "$state" = STOPPED ] && [ "$note" != "stopped by operator" ] && [ "$note" != "not in ENABLED" ] && [ -n "$sp" ] && bad=1
        printf '%-8s %-9s %-8s %-8s %-9s %s\n' "$s" "${state:-?}" "$pid" "$up" "$restarts" "$note"
    done
    [ "$bad" = 0 ]
}

cmd_logs() {
    local s="" follow=0 a
    for a in "$@"; do case "$a" in -f) follow=1;; *) s="$a";; esac; done
    local f="$LOGS/${s:-supervisor}.log"
    [ -n "$s" ] && [ "$s" != supervisor ] && { valid_service "$s" || die "unknown service '$s'"; }
    [ -f "$f" ] || die "no log yet: $f"
    if [ "$follow" = 1 ]; then tail -n 40 -F "$f"; else tail -n 60 "$f"; fi
}

symbols_block() {
    cat <<'SYMEOF'

# ---- APRS symbols ---------------------------------------------------------
# Two characters each: symbol table ("/" primary, "\" alternate) then symbol code.
# Use single quotes.  The one exception is a code that is itself a single quote
# (the crash-site symbol): write that in double quotes, as below.
# Symbol chart: https://www.aprs.org/symbols.html
#
# County CAD (montco, chesco, bucks)
SYM_FIRE='/d'            # fire department
SYM_EMS='/a'             # ambulance
SYM_TRAFFIC="\'"         # crash site
SYM_OTHER='\!'           # emergency
# Road feeds (nj511, pa511); the last three are used by nj511 only
SYM_ROAD_WORK='/.'       # red X: roadwork / lane restriction
SYM_ROAD_CRASH="\'"      # crash site
SYM_ROAD_MAJOR='\!'      # emergency: major incident / road closed
SYM_ROAD_DELAY='/.'      # delays
SYM_ROAD_EVENT='/E'      # venue events (games, concerts)
SYM_ROAD_OTHER='\<'      # anything else (fuel, truck restrictions)
# Weather (nws)
SYM_WX_TOR='\t'          # tornado warning/watch
SYM_WX_SVR='\T'          # severe thunderstorm warning/watch
SYM_WX_STORM='\T'        # tracked strong thunderstorm (weather statement)
SYM_WX_SMW='\T'          # special marine warning
SYM_WX_SQW='\*'          # snow squall
SYM_WX_TRP='\@'          # hurricane / tropical storm warnings
SYM_WX_HURRICANE='\@'    # tropical cyclone position (National Hurricane Center)
SYM_WX_FFW='\w'          # flash flood
SYM_WX_FLD='\w'          # flood
SYM_WX_ICE='\F'          # ice storm / freezing rain
SYM_WX_WIN='\*'          # winter storm / snow
SYM_WX_CLD='\F'          # cold / freeze / frost
SYM_WX_WND='\g'          # wind / gale
SYM_WX_FOG='\{'          # fog
SYM_WX_HOT='\U'          # heat
SYM_WX_FIR='/:'          # red flag / fire weather
SYM_WX_DST='\b'          # blowing dust
SYM_WX_SPS='\<'          # special weather statement
SYM_WX_WXA='\<'          # any other alert
SYMEOF
}

cmd_symbols() {
    ensure_conf
    if ! grep -q '^SYM_' "$CONF" 2>/dev/null; then
        symbols_block >> "$CONF"
        echo "Added the APRS symbol settings to $CONF (all set to the current defaults)."
    else
        local missing="" line key
        while IFS= read -r line; do
            key="${line%%=*}"
            if [[ "$line" == SYM_* ]] && ! grep -q "^$key=" "$CONF"; then missing+="$line"$'\n'; fi
        done < <(symbols_block)
        if [ -z "$missing" ]; then echo "$CONF already has every SYM_ setting - edit them there."; return 0; fi
        printf '\n# APRS symbols added later\n%s' "$missing" >> "$CONF"
        echo "Added the missing symbol settings to $CONF:"; printf '%s' "$missing" | sed 's/^/  /'
    fi
    echo "Edit them, then run: $0 restart"
}

cmd_init() {
    mkdirs
    if [ -f "$CONF" ]; then echo "$CONF already exists - leaving it alone."; return 0; fi
    cat > "$CONF" <<'EOF'
# aprs.env - settings for aprs_ctl.sh (sourced by bash; keep it private: chmod 600)

# Your amateur callsign-SSID. Required when SEND=1 (N0CALL is refused).
CALLSIGN="N0CALL"
# 0 = print packets only (safe test mode), 1 = really send to APRS-IS.
# With SEND=1 the services hand their packets to the "send" service (aprs_send.py), the only
# one that logs in to APRS-IS.  It sends an object when it is new, changed or killed, and
# re-sends an unchanged one every 26 minutes.
SEND=0
# 67 is generous; use 43 if clients truncate or drop the objects.
COMMENT_LENGTH=67

# Which services to run (remove any you don't want).
# "send" (the APRS-IS sender) always runs, listed here or not.
ENABLED="send montco chesco bucks nj511 pa511 nws web"
WEB_PORT=8080                     # web map/editor: http://this-machine:8080/ (no login - internal use only)

# Credentials
GOOGLE_MAPS_API_KEY=""            # montco + chesco (Google Geocoding)
PENNDOT_RCRS_USER=""              # pa511
PENNDOT_RCRS_PASSWORD=""
NWS_CONTACT=""                    # nws: your e-mail or website (NWS asks clients to identify themselves)
APRS_HOME=""                      # nws: centre of your area as "lat,lon" (blank = Norristown, PA)

# Web map: other people's APRS objects/stations heard around APRS_HOME (receive only)
#APRS_LISTEN=0                    # uncomment to switch the listener off
#APRS_LISTEN_MILES=50
#APRS_TRAIL_MINUTES=180           # how much of each moving object's path to keep and draw
#APRS_BACKFILL=0                  # uncomment to never fetch recent APRS history on a cold start

# Poll intervals (seconds). Chester and PA511 are kept >= 60 by the scripts.
INTERVAL_montco=120
INTERVAL_chesco=120
INTERVAL_bucks=120
INTERVAL_nj511=120
INTERVAL_pa511=180
INTERVAL_nws=120

# Extra command-line options per service, for example:
#   EXTRA_montco="--categories Fire,Traffic --max-age-hours 6"
#   EXTRA_pa511="--county Montgomery,Chester,Delaware,Bucks,Philadelphia"
#   EXTRA_chesco="--ignore-robots"     # only if YOU decide that is appropriate
#   EXTRA_nws="--radius-miles 200 --min-severity Severe"
#   EXTRA_nj511="--county Camden,Burlington,Gloucester --types incident,closure,delay"
EXTRA_send=""                     # e.g. "--resend-minutes 20"
EXTRA_montco=""
EXTRA_chesco=""
EXTRA_bucks=""
EXTRA_nj511=""
EXTRA_pa511=""
EXTRA_nws=""

# Supervisor tuning (defaults shown)
#CHECK_EVERY=5        # seconds between health checks
#MAX_CRASHES=5        # crashes within CRASH_WINDOW before a service is marked FAILED
#CRASH_WINDOW=600
#BACKOFF_MIN=5
#BACKOFF_MAX=300
#STALL_SECS=0         # >0: restart a service whose log has been silent this long
EOF
    symbols_block >> "$CONF"
    chmod 600 "$CONF"
    echo "Created $CONF - edit it (callsign, API key, PennDOT login), then run: $0 start"
}

usage() { sed -n '2,19p' "$SELF" | sed 's/^# \{0,1\}//'; }

case "${1:-}" in
    init)    cmd_init ;;
    symbols) cmd_symbols ;;
    start)   cmd_start "${2:-}" ;;
    stop)    cmd_stop "${2:-}" ;;
    restart) cmd_stop "${2:-}"; cmd_start "${2:-}" ;;
    status)  cmd_status ;;
    logs)    shift; cmd_logs "$@" ;;
    monitor) ensure_conf; FOREGROUND=1; supervisor_main ;;
    _daemon) supervisor_main ;;
    *)       usage; exit 1 ;;
esac
