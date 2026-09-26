#!/usr/bin/env bash
# Sets up a fresh Ubuntu 24.04 server for Buddy Network - see server/README.md.
# Run as root, after push.sh has copied the code to /opt/buddy-network/server:
#
#   bash /opt/buddy-network/server/deploy/setup.sh chat.example.com
#
# Safe to run again: every step checks what's already there.
set -euo pipefail

DOMAIN="${1:?usage: setup.sh <domain, e.g. chat.example.com>}"
APP=/opt/buddy-network
DATA=/var/lib/buddy-network
BACKUPS=/var/backups/buddy-network
SERVICE_USER=buddynet
export DEBIAN_FRONTEND=noninteractive

step() { echo; echo "==> $*"; }

[ -f "$APP/server/__main__.py" ] || { echo "The code isn't in $APP/server yet - run push.sh first."; exit 1; }

step "1/9 System updates and packages (Python, Caddy, firewall - all from Ubuntu's own archive)"
apt-get update -q
apt-get upgrade -yq
apt-get install -yq python3-venv sqlite3 caddy ufw unattended-upgrades

step "2/9 Swap file (room for installs on a small plan)"
if ! swapon --show | grep -q '^/swapfile'; then
    fallocate -l 1G /swapfile
    chmod 600 /swapfile
    mkswap /swapfile
    swapon /swapfile
fi
grep -q '^/swapfile ' /etc/fstab || echo '/swapfile none swap sw 0 0' >> /etc/fstab

step "3/9 Service account and folders"
id "$SERVICE_USER" >/dev/null 2>&1 || useradd --system --home-dir "$DATA" --shell /usr/sbin/nologin "$SERVICE_USER"
install -d -o root -g root -m 755 "$APP"
install -d -o "$SERVICE_USER" -g "$SERVICE_USER" -m 700 "$DATA"
install -d -o root -g root -m 700 "$BACKUPS"

step "4/9 Python environment"
[ -x "$APP/venv/bin/python" ] || python3 -m venv "$APP/venv"
"$APP/venv/bin/pip" install -q --upgrade pip
"$APP/venv/bin/pip" install -q -r "$APP/server/requirements.txt"

step "5/9 The Buddy Network service (restarts itself if it ever stops)"
cat > /etc/systemd/system/buddy-network.service <<EOF
[Unit]
Description=Buddy Network chat server
After=network-online.target
Wants=network-online.target

[Service]
User=$SERVICE_USER
Group=$SERVICE_USER
WorkingDirectory=$APP
ExecStart=$APP/venv/bin/python -m server --behind-proxy --host 127.0.0.1 --port 8765 --db $DATA/network.db
Restart=always
RestartSec=3
# It only ever needs to write its own database.
NoNewPrivileges=true
ProtectSystem=strict
ProtectHome=true
PrivateTmp=true
ReadWritePaths=$DATA

[Install]
WantedBy=multi-user.target
EOF
mkdir -p /etc/systemd/system/buddy-network.service.d
cp "$APP/server/deploy/hardening.conf" /etc/systemd/system/buddy-network.service.d/hardening.conf
systemctl daemon-reload
systemctl enable buddy-network
systemctl restart buddy-network

step "6/9 Caddy: https/wss with a free Let's Encrypt certificate, no IP addresses logged"
cat > /etc/caddy/Caddyfile <<EOF
# Buddy Network (see server/README.md). Caddy writes no access log unless
# one is configured - none is - and the filter strips addresses from
# what it does log (errors, and certificate checks, whose "remote" is
# Let's Encrypt's own servers), so no IP address is kept here either.
{
	log default {
		format filter {
			wrap console
			fields {
				remote delete
				request>remote_ip delete
				request>remote_port delete
				request>client_ip delete
				request>headers>X-Forwarded-For delete
			}
		}
	}
}

$DOMAIN {
	reverse_proxy 127.0.0.1:8765
}
EOF
caddy validate --config /etc/caddy/Caddyfile --adapter caddyfile
systemctl enable caddy
systemctl restart caddy

step "7/9 Firewall: only SSH, HTTP and HTTPS in"
ufw allow OpenSSH
ufw allow 80/tcp
ufw allow 443/tcp
ufw logging off    # it would otherwise log the addresses of blocked connections
ufw --force enable

step "8/9 SSH: keys only, no passwords"
cat > /etc/ssh/sshd_config.d/00-buddy-network.conf <<EOF
# Read before 50-cloud-init.conf, and sshd keeps the first value it reads.
PasswordAuthentication no
KbdInteractiveAuthentication no
PermitRootLogin prohibit-password
EOF
sshd -t
systemctl reload ssh

step "9/9 Automatic security updates and nightly backups"
cat > /etc/apt/apt.conf.d/20auto-upgrades <<EOF
APT::Periodic::Update-Package-Lists "1";
APT::Periodic::Unattended-Upgrade "1";
EOF
cat > /etc/apt/apt.conf.d/52buddy-network-reboot <<EOF
// Kernel updates need a restart: do it in the quiet hours (server time, UTC).
Unattended-Upgrade::Automatic-Reboot "true";
Unattended-Upgrade::Automatic-Reboot-Time "04:30";
EOF
cat > /usr/local/bin/buddy-network-backup <<'EOF'
#!/bin/sh
# Nightly copy of the Buddy Network database, the last 7 kept. Readable by
# root only - it holds the same 30 days of messages as the live one.
set -e
dir=/var/backups/buddy-network
sqlite3 /var/lib/buddy-network/network.db ".backup '$dir/network-$(date +%F).db'"
chmod 600 "$dir"/network-*.db
find "$dir" -name 'network-*.db' -mtime +6 -delete
EOF
chmod 755 /usr/local/bin/buddy-network-backup
cat > /etc/systemd/system/buddy-network-backup.service <<EOF
[Unit]
Description=Back up the Buddy Network database

[Service]
Type=oneshot
ExecStart=/usr/local/bin/buddy-network-backup
EOF
cat > /etc/systemd/system/buddy-network-backup.timer <<EOF
[Unit]
Description=Nightly Buddy Network backup

[Timer]
OnCalendar=*-*-* 03:30:00
Persistent=true

[Install]
WantedBy=timers.target
EOF
systemctl daemon-reload
systemctl enable --now buddy-network-backup.timer

step "Done"
echo "buddy-network: $(systemctl is-active buddy-network)   caddy: $(systemctl is-active caddy)"
echo "Chat address: wss://$DOMAIN"
