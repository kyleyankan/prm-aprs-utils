#!/bin/sh
# Install aprs-incident-rf-bridge as a systemd service. Run it from a copy of the aprs-incident-rf-bridge folder:
#
#     sudo sh systemd/install.sh
#
# It copies the script to /opt/aprs-incident-rf-bridge, creates /etc/aprs-incident-rf-bridge/aprs-incident-rf-bridge.conf if there
# is none, and installs the service. It never overwrites your settings, and it does not
# start the service: you start it once the settings are yours. Safe to run again to update.
set -eu

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

say "==> Settings in $CONF"
install -d -m 755 "$CONF_DIR"
if [ -f "$CONF" ]; then
    say "    kept your existing settings"
else
    install -m 644 "$HERE/systemd/aprs-incident-rf-bridge.conf.example" "$CONF"
    say "    created from the example; edit it before starting the service"
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
say "  1. Edit the settings:      sudo nano $CONF"
say "     (your callsign, position, TNC; leave transmit = no for now)"
say "  2. Start it and watch:     sudo systemctl start aprs-incident-rf-bridge && journalctl -u aprs-incident-rf-bridge -f"
say "  3. When the list looks right, set transmit = yes and:  sudo systemctl restart aprs-incident-rf-bridge"
say "  4. Start it at every boot: sudo systemctl enable aprs-incident-rf-bridge"
