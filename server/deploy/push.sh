#!/usr/bin/env bash
# Copies server/ (this repo's) to the Buddy Network server, and restarts
# the service if it's already set up - see server/README.md. Run from bash
# (Git Bash on Windows) in the repo:
#
#   bash server/deploy/push.sh root@<server IP>
set -euo pipefail

HOST="${1:?usage: push.sh root@<server address>}"
cd "$(dirname "$0")/../.."

# Never local-only files (*.local.*), databases or caches.
tar --exclude='__pycache__' --exclude='*.db' --exclude='*.db-*' --exclude='*.local.*' \
    -czf - server | ssh "$HOST" '
    set -e
    mkdir -p /opt/buddy-network/incoming
    rm -rf /opt/buddy-network/incoming/*
    tar -xzf - -C /opt/buddy-network/incoming
    chown -R root:root /opt/buddy-network/incoming
    rm -rf /opt/buddy-network/server.old
    [ -d /opt/buddy-network/server ] && mv /opt/buddy-network/server /opt/buddy-network/server.old
    mv /opt/buddy-network/incoming/server /opt/buddy-network/server
    echo "Code copied."
    if [ -f /etc/systemd/system/buddy-network.service ]; then
        /opt/buddy-network/venv/bin/pip install -q -r /opt/buddy-network/server/requirements.txt
        DROPIN=/etc/systemd/system/buddy-network.service.d
        mkdir -p "$DROPIN"
        rm -f "$DROPIN/hardening.conf.prev"
        [ -f "$DROPIN/hardening.conf" ] && cp -a "$DROPIN/hardening.conf" "$DROPIN/hardening.conf.prev"
        cp /opt/buddy-network/server/deploy/hardening.conf "$DROPIN/hardening.conf"
        cp /opt/buddy-network/server/deploy/giphy.conf "$DROPIN/giphy.conf"
        systemctl daemon-reload
        systemctl restart buddy-network
        sleep 2
        if ! systemctl is-active --quiet buddy-network; then
            # Never carry on without the limits (memory, processes, umask):
            # say why it failed, put back what was running before, and fail.
            echo "It did not start with this release and its limits (hardening.conf). Why:"
            journalctl -u buddy-network -n 30 --no-pager || true
            rm -rf /opt/buddy-network/server.failed
            mv /opt/buddy-network/server /opt/buddy-network/server.failed
            [ -d /opt/buddy-network/server.old ] && mv /opt/buddy-network/server.old /opt/buddy-network/server
            if [ -f "$DROPIN/hardening.conf.prev" ]; then
                mv "$DROPIN/hardening.conf.prev" "$DROPIN/hardening.conf"
            else
                rm -f "$DROPIN/hardening.conf"
            fi
            systemctl daemon-reload
            systemctl restart buddy-network
            sleep 2
            echo "Rolled back to the release that was running (the failed one is in server.failed)."
            echo "buddy-network: $(systemctl is-active buddy-network || true)"
            exit 1
        fi
        rm -f "$DROPIN/hardening.conf.prev"
        echo "buddy-network: $(systemctl is-active buddy-network)"
    fi
'
