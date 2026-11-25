#!/bin/sh
# Remove the aprs-radio service. Run with sudo:
#
#     sudo sh /opt/aprs-radio/uninstall.sh            keeps your settings and the sent-list
#     sudo sh /opt/aprs-radio/uninstall.sh --purge    removes those too
#
# Objects this station put on the air are NOT killed by stopping the service. To take them
# off first, see "Taking your objects off the air" in INSTALL.md.
set -eu

[ "$(id -u)" -eq 0 ] || { echo "uninstall.sh: run this with sudo" >&2; exit 1; }
PURGE=no
[ "${1:-}" = "--purge" ] && PURGE=yes

if [ -d /run/systemd/system ]; then
    systemctl disable --now aprs-radio 2>/dev/null || true
fi
rm -f /etc/systemd/system/aprs-radio.service
rm -rf /etc/systemd/system/aprs-radio.service.d
[ -d /run/systemd/system ] && systemctl daemon-reload
rm -rf /opt/aprs-radio
echo "Removed the service and /opt/aprs-radio."

if [ "$PURGE" = yes ]; then
    rm -rf /etc/aprs-radio /var/lib/aprs-radio /var/lib/private/aprs-radio
    echo "Removed the settings (/etc/aprs-radio) and the sent-list (/var/lib/aprs-radio)."
else
    echo "Kept your settings in /etc/aprs-radio and the sent-list in /var/lib/aprs-radio."
    echo "Run again with --purge to remove them."
fi
