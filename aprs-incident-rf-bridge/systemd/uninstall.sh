#!/bin/sh
# Remove the aprs-incident-rf-bridge service. Run with sudo:
#
#     sudo sh /opt/aprs-incident-rf-bridge/uninstall.sh            keeps your settings and the sent-list
#     sudo sh /opt/aprs-incident-rf-bridge/uninstall.sh --purge    removes those too
#
# Objects this station put on the air are NOT killed by stopping the service. To take them
# off first, see "Taking your objects off the air" in INSTALL.md.
set -eu

[ "$(id -u)" -eq 0 ] || { echo "uninstall.sh: run this with sudo" >&2; exit 1; }
PURGE=no
[ "${1:-}" = "--purge" ] && PURGE=yes

if [ -d /run/systemd/system ]; then
    systemctl disable --now aprs-incident-rf-bridge 2>/dev/null || true
fi
rm -f /etc/systemd/system/aprs-incident-rf-bridge.service
rm -rf /etc/systemd/system/aprs-incident-rf-bridge.service.d
[ -d /run/systemd/system ] && systemctl daemon-reload
rm -rf /opt/aprs-incident-rf-bridge
echo "Removed the service and /opt/aprs-incident-rf-bridge."

if [ "$PURGE" = yes ]; then
    rm -rf /etc/aprs-incident-rf-bridge /var/lib/aprs-incident-rf-bridge /var/lib/private/aprs-incident-rf-bridge
    echo "Removed the settings (/etc/aprs-incident-rf-bridge) and the sent-list (/var/lib/aprs-incident-rf-bridge)."
else
    echo "Kept your settings in /etc/aprs-incident-rf-bridge and the sent-list in /var/lib/aprs-incident-rf-bridge."
    echo "Run again with --purge to remove them."
fi
