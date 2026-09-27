#!/usr/bin/env bash
# Sets the GIPHY API key that Buddy Network's GIF search uses, and restarts
# the server - see server/README.md. Run as root on the server:
#
#   bash /opt/buddy-network/server/deploy/set-giphy-key.sh
#
# It asks for the key (it isn't shown or kept in the shell's history). An
# empty answer removes it, which turns GIF search off.
set -euo pipefail

FILE=/etc/buddy-network/giphy.env
read -rsp "GIPHY API key (empty to turn GIF search off): " KEY
echo
install -d -o root -g root -m 700 /etc/buddy-network
if [ -z "$KEY" ]; then
    rm -f "$FILE"
    echo "GIF search is off."
else
    case "$KEY" in *[!A-Za-z0-9]*) echo "That doesn't look like a GIPHY key (letters and digits only)."; exit 1;; esac
    ( umask 077; printf 'GIPHY_API_KEY=%s\n' "$KEY" > "$FILE" )
    echo "Key saved in $FILE."
fi
DROPIN=/etc/systemd/system/buddy-network.service.d
mkdir -p "$DROPIN"
cp /opt/buddy-network/server/deploy/giphy.conf "$DROPIN/giphy.conf"
systemctl daemon-reload
systemctl restart buddy-network
sleep 2
journalctl -u buddy-network -n 20 --no-pager | grep -o "GIF search: .*" | tail -1 || true
