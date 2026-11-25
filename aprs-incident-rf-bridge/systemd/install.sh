#!/bin/sh
# Install aprs-incident-rf-bridge as a systemd service. Run it from a copy of the aprs-incident-rf-bridge folder:
#
#     sudo sh systemd/install.sh
#
# It copies the script to /opt/aprs-incident-rf-bridge and installs the service. On a first
# install it asks for your callsign, your position and a radius, and writes them to
# /etc/aprs-incident-rf-bridge/aprs-incident-rf-bridge.conf. To answer ahead of time (or when
# there is nobody to ask, as in a script), give them as options:
#
#     sudo sh systemd/install.sh --callsign W3XYZ-10 --lat 40.00 --lon -75.00 --radius 10
#
# The radius is in miles; write 16km for kilometres. It never overwrites settings that are
# already there, and it does not start the service: you start it yourself, and it does not
# transmit until you set transmit = yes. Safe to run again to update.
set -eu

ASK_CALLSIGN="" ASK_LAT="" ASK_LON="" ASK_RADIUS=""
while [ $# -gt 0 ]; do
    case "$1" in
        --callsign) ASK_CALLSIGN="${2:-}"; shift 2 ;;
        --lat)      ASK_LAT="${2:-}"; shift 2 ;;
        --lon)      ASK_LON="${2:-}"; shift 2 ;;
        --radius|--miles) ASK_RADIUS="${2:-}"; shift 2 ;;
        -h|--help)  sed -n '2,13p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
        *) printf 'install.sh: unknown option %s (see --help)\n' "$1" >&2; exit 1 ;;
    esac
done

HERE="$(cd "$(dirname "$0")/.." && pwd)"
APP_DIR=/opt/aprs-incident-rf-bridge
CONF_DIR=/etc/aprs-incident-rf-bridge
CONF="$CONF_DIR/aprs-incident-rf-bridge.conf"
UNIT=/etc/systemd/system/aprs-incident-rf-bridge.service
DROPIN_DIR=/etc/systemd/system/aprs-incident-rf-bridge.service.d

say() { printf '%s\n' "$*"; }
fail() { printf 'install.sh: %s\n' "$*" >&2; exit 1; }

[ "$(id -u)" -eq 0 ] || fail "run this with sudo (it writes to /opt, /etc and installs a service)"
[ -f "$HERE/aprs_radio.py" ] || fail "aprs_radio.py is not in $HERE; run this from the aprs-incident-rf-bridge folder"
command -v python3 >/dev/null 2>&1 || fail "python3 is not installed (Debian/Ubuntu/Raspberry Pi OS: apt install python3)"
python3 -c 'import sys; sys.exit(0 if sys.version_info >= (3, 8) else 1)' \
    || fail "Python 3.8 or later is needed; this machine has $(python3 -V 2>&1)"
[ -x /usr/bin/python3 ] || fail "the service expects python3 at /usr/bin/python3; it is at $(command -v python3)"
command -v systemctl >/dev/null 2>&1 || fail "this machine does not use systemd (no systemctl)"

# Before 2026-11-25 this was installed under the name "aprs-radio". If that install is here,
# move its settings and its record of what was sent to the new name and remove the old service.
OLD=aprs-radio
WAS_ACTIVE=no
WAS_ENABLED=no
if [ -f "/etc/systemd/system/$OLD.service" ] || [ -d "/etc/$OLD" ] || [ -d "/opt/$OLD" ]; then
    say "==> Found the earlier install named $OLD; moving it to aprs-incident-rf-bridge"
    if [ -d /run/systemd/system ]; then
        systemctl is-active --quiet "$OLD" && WAS_ACTIVE=yes
        systemctl is-enabled --quiet "$OLD" 2>/dev/null && WAS_ENABLED=yes
        systemctl disable --now "$OLD" 2>/dev/null || true
    fi
    if [ -f "/etc/$OLD/$OLD.conf" ] && [ ! -f "$CONF" ]; then
        install -d -m 755 "$CONF_DIR"
        cp -p "/etc/$OLD/$OLD.conf" "$CONF"
        say "    settings moved to $CONF"
    fi
    if [ -f "/var/lib/private/$OLD/state.json" ] && [ ! -e /var/lib/private/aprs-incident-rf-bridge/state.json ]; then
        install -d -m 700 /var/lib/private
        install -d -m 755 /var/lib/private/aprs-incident-rf-bridge
        cp -p "/var/lib/private/$OLD/state.json" /var/lib/private/aprs-incident-rf-bridge/state.json
        say "    the record of what was sent moved too"
    fi
    rm -f "/etc/systemd/system/$OLD.service"
    rm -rf "/etc/systemd/system/$OLD.service.d" "/opt/$OLD" "/etc/$OLD" "/var/lib/private/$OLD" "/var/lib/$OLD"
fi

say "==> Script and documentation in $APP_DIR"
install -d -m 755 "$APP_DIR"
install -m 644 "$HERE/aprs_radio.py" "$APP_DIR/aprs_radio.py"
for doc in README.md INSTALL.md; do
    [ -f "$HERE/$doc" ] && install -m 644 "$HERE/$doc" "$APP_DIR/$doc"
done
install -m 644 "$HERE/systemd/aprs-incident-rf-bridge.conf.example" "$APP_DIR/aprs-incident-rf-bridge.conf.example"
install -m 755 "$HERE/systemd/uninstall.sh" "$APP_DIR/uninstall.sh"

# Check one answer with the script's own rules. Prints the value to store (the radius in
# miles, the callsign in capitals) or, on a mistake, a line starting with "!" that says why.
check() {
    python3 - "$HERE" "$1" "$2" <<'PY'
import contextlib, io, re, sys
sys.path.insert(0, sys.argv[1])
import aprs_radio as R
what, value = sys.argv[2], sys.argv[3].strip()
good = {"callsign": "W3XYZ-10", "lat": "40", "lon": "-75", "miles": "10"}
try:
    if what == "miles":
        m = re.fullmatch(r"([0-9.]+)\s*(km|kilometres|kilometers|mi|miles)?", value.lower())
        if not m:
            raise ValueError("write a number of miles, or kilometres as 16km")
        number = float(m.group(1)) * (0.621371 if (m.group(2) or "").startswith("k") else 1)
        value = f"{number:.1f}".rstrip("0").rstrip(".")
    elif what in ("lat", "lon"):
        float(value)
    good[what] = value
    err = io.StringIO()
    with contextlib.redirect_stderr(err):
        try:
            a = R.parse_args([f"--{k}={v}" for k, v in good.items()])
        except SystemExit:
            raise ValueError(err.getvalue().strip().splitlines()[-1].split("error: ")[-1])
    print(a.callsign if what == "callsign" else value)
except ValueError as e:
    print("! " + (str(e) if "could not convert" not in str(e) else "write a number in decimal degrees, such as 40.00 or -75.00"))
PY
}

# Take an answer from its option, or ask for it. Sets ANSWER; empty when there is nobody to ask.
answer() {      # answer <what> <value from the option> <question>
    ANSWER=""
    value="$2"
    while :; do
        if [ -z "$value" ]; then
            [ -t 0 ] || return 0
            printf '    %s ' "$3"
            read -r value || return 0
            [ -n "$value" ] || continue
        fi
        result="$(check "$1" "$value")"
        case "$result" in
            "!"*) say "      ${result#! }"
                  [ -t 0 ] || fail "$1: ${result#! }"
                  value="" ;;
            *)    ANSWER="$result"; return 0 ;;
        esac
    done
}

say "==> Settings in $CONF"
install -d -m 755 "$CONF_DIR"
CONFIGURED=no
if [ -f "$CONF" ]; then
    say "    kept your existing settings"
    CONFIGURED=yes
else
    if [ -t 0 ]; then
        say "    Three questions. Your station transmits under this callsign, and asks the feeder for"
        say "    the objects within the radius of this position."
    fi
    answer callsign "$ASK_CALLSIGN" "Your callsign and SSID (for example W3XYZ-10):"; SET_CALLSIGN="$ANSWER"
    answer lat "$ASK_LAT" "Latitude of the centre of your area, in decimal degrees (north is positive):"; SET_LAT="$ANSWER"
    answer lon "$ASK_LON" "Longitude, in decimal degrees (west is negative, for example -75.00):"; SET_LON="$ANSWER"
    answer miles "$ASK_RADIUS" "Radius around that point, in miles (or kilometres as 16km):"; SET_MILES="$ANSWER"
    install -m 644 "$HERE/systemd/aprs-incident-rf-bridge.conf.example" "$CONF"
    if [ -n "$SET_CALLSIGN" ] && [ -n "$SET_LAT" ] && [ -n "$SET_LON" ] && [ -n "$SET_MILES" ]; then
        sed -i -e "s|^callsign *=.*|callsign = $SET_CALLSIGN|" -e "s|^lat *=.*|lat = $SET_LAT|" \
               -e "s|^lon *=.*|lon = $SET_LON|" -e "s|^miles *=.*|miles = $SET_MILES|" "$CONF"
        for want in "callsign = $SET_CALLSIGN" "lat = $SET_LAT" "lon = $SET_LON" "miles = $SET_MILES"; do
            grep -qxF "$want" "$CONF" || fail "could not write \"$want\" to $CONF; edit that file by hand"
        done
        say "    written: $SET_CALLSIGN at $SET_LAT, $SET_LON, $SET_MILES miles. Transmitting is off (transmit = no)."
        CONFIGURED=yes
    else
        say "    created without a callsign or position, because there was nobody to ask."
        say "    Edit it before starting the service, or run this again with --callsign, --lat, --lon and --radius."
    fi
fi

say "==> Service in $UNIT"
install -m 644 "$HERE/systemd/aprs-incident-rf-bridge.service" "$UNIT"

# A hardware TNC on a serial port is owned by a group, named dialout on Debian-family
# systems and uucp on Arch-family ones. Give the service that group if this machine has it.
install -d -m 755 "$DROPIN_DIR"
SERIAL_GROUP=""
for group in dialout uucp; do
    if getent group "$group" >/dev/null 2>&1; then SERIAL_GROUP="$group"; break; fi
done
if [ -n "$SERIAL_GROUP" ]; then
    printf '# Written by install.sh: lets the service open serial ports for a hardware TNC.\n[Service]\nSupplementaryGroups=%s\n' \
        "$SERIAL_GROUP" > "$DROPIN_DIR/serial.conf"
    say "    serial ports: the service is in the $SERIAL_GROUP group"
else
    rm -f "$DROPIN_DIR/serial.conf"
    say "    serial ports: no dialout or uucp group here; see INSTALL.md if you use a hardware TNC"
fi

if [ -d /run/systemd/system ]; then
    systemctl daemon-reload
    if systemctl is-active --quiet aprs-incident-rf-bridge; then
        systemctl restart aprs-incident-rf-bridge
        say "==> The service was running, so it was restarted with the new version"
    fi
    if [ "$WAS_ENABLED" = yes ]; then
        systemctl enable --quiet aprs-incident-rf-bridge
        say "==> The earlier service started at boot, so this one does too"
    fi
    if [ "$WAS_ACTIVE" = yes ]; then
        systemctl start aprs-incident-rf-bridge
        say "==> The earlier service was running, so this one was started"
    fi
else
    say "==> systemd is not running here (a container or chroot?); skipped reloading it"
fi

say ""
say "Installed. Next:"
if [ "$CONFIGURED" = yes ]; then
    say "  1. Check the settings:     sudo nano $CONF"
    say "     (your TNC is assumed to be Direwolf on this machine; leave transmit = no for now)"
else
    say "  1. Edit the settings:      sudo nano $CONF"
    say "     (your callsign, position, TNC; leave transmit = no for now)"
fi
say "  2. Start it and watch:     sudo systemctl start aprs-incident-rf-bridge && journalctl -u aprs-incident-rf-bridge -f"
say "  3. When the list looks right, set transmit = yes and:  sudo systemctl restart aprs-incident-rf-bridge"
say "  4. Start it at every boot: sudo systemctl enable aprs-incident-rf-bridge"
