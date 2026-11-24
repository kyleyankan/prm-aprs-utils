#!/usr/bin/env bash
# Optional: start the APRS services at boot with systemd.
#   sudo ./install-service.sh            install + enable + start
#   sudo ./install-service.sh remove     stop + disable + remove
set -eu
[ "$(id -u)" = 0 ] || { echo "Run with sudo: sudo $0 $*"; exit 1; }
DIR="$(dirname "$(readlink -f "$0")")"
RUN_AS="${SUDO_USER:-$(stat -c %U "$DIR")}"
UNIT=/etc/systemd/system/aprs-feeds.service

if [ "${1:-}" = remove ]; then
    systemctl disable --now aprs-feeds.service 2>/dev/null || true
    rm -f "$UNIT"; systemctl daemon-reload
    echo "Removed aprs-feeds.service"; exit 0
fi
[ -f "$DIR/aprs.env" ] || { echo "No aprs.env yet. Run ./aprs_ctl.sh init and edit it first."; exit 1; }
sudo -u "$RUN_AS" "$DIR/aprs_ctl.sh" stop >/dev/null 2>&1 || true     # hand over from a manual start

cat > "$UNIT" <<UNITEOF
[Unit]
Description=APRS object feeds (CAD, traffic, weather) and web map
Wants=network-online.target
After=network-online.target

[Service]
Type=simple
User=$RUN_AS
WorkingDirectory=$DIR
ExecStart=$DIR/aprs_ctl.sh monitor
# "aprs_ctl.sh stop" exits cleanly and stays stopped; a crash of the supervisor is restarted.
Restart=on-failure
RestartSec=10
TimeoutStopSec=40

[Install]
WantedBy=multi-user.target
UNITEOF
systemctl daemon-reload
systemctl enable --now aprs-feeds.service
echo "Installed. It runs as '$RUN_AS' from $DIR and starts at boot."
echo "  sudo systemctl status aprs-feeds     ./aprs_ctl.sh status     ./aprs_ctl.sh logs nj511 -f"
