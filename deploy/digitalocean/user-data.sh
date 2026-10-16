#!/bin/bash
# PaperPump cloud-init user-data for DigitalOcean (Ubuntu 24.04). Contains NO secrets.
set -euxo pipefail
exec > >(tee -a /var/log/paperpump-setup.log) 2>&1
export DEBIAN_FRONTEND=noninteractive
REPO=https://github.com/Jparker9204/paperpump.git
APP=/opt/paperpump

apt-get update
apt-get install -y python3-venv python3-pip git curl ufw unattended-upgrades \
  debian-keyring debian-archive-keyring apt-transport-https gnupg

# Caddy official repo
curl -1sLf https://dl.cloudsmith.io/public/caddy/stable/gpg.key | gpg --dearmor --yes -o /usr/share/keyrings/caddy-stable-archive-keyring.gpg
curl -1sLf https://dl.cloudsmith.io/public/caddy/stable/debian.deb.txt > /etc/apt/sources.list.d/caddy-stable.list
apt-get update
apt-get install -y caddy

# User, code, venv
id paperpump >/dev/null 2>&1 || useradd --system --create-home --home-dir /home/paperpump --shell /usr/sbin/nologin paperpump
[ -d "$APP/.git" ] || git clone "$REPO" "$APP"
chown -R paperpump:paperpump "$APP"
sudo -u paperpump python3 -m venv "$APP/.venv"
sudo -u paperpump "$APP/.venv/bin/pip" install --upgrade pip
sudo -u paperpump "$APP/.venv/bin/pip" install -r "$APP/requirements.txt"
install -d -o paperpump -g paperpump -m 750 /var/lib/paperpump

# Hostname via metadata
IP=$(curl -fsS --retry 10 --retry-delay 2 http://169.254.169.254/metadata/v1/interfaces/public/0/ipv4/address)
HOST="${IP//./-}.sslip.io"

# Secrets generated on the server
if [ ! -f /etc/paperpump.env ]; then
  SESSION_SECRET=$(openssl rand -hex 32)
  SITE_PASSWORD=$(openssl rand -base64 18 | tr -dc 'A-Za-z0-9' | cut -c1-20)
  umask 077
  cat > /etc/paperpump.env <<ENV
BASE_URL=https://$HOST
SESSION_SECRET=$SESSION_SECRET
SITE_PASSWORD=$SITE_PASSWORD
DB_PATH=/var/lib/paperpump/paperpump.db
ENV
  printf '%s\n' "$SITE_PASSWORD" > /root/SITE_PASSWORD.txt
  umask 022
fi
chown root:root /etc/paperpump.env /root/SITE_PASSWORD.txt
chmod 600 /etc/paperpump.env /root/SITE_PASSWORD.txt
install -d -m 755 /etc/paperpump

# Caddy
cat > /etc/caddy/Caddyfile <<CADDY
$HOST {
	encode gzip
	reverse_proxy 127.0.0.1:8000
}
CADDY

# systemd units
cat > /etc/systemd/system/paperpump-web.service <<'UNIT'
[Unit]
Description=PaperPump web
After=network-online.target
Wants=network-online.target

[Service]
User=paperpump
WorkingDirectory=/opt/paperpump
EnvironmentFile=/etc/paperpump.env
ExecStart=/opt/paperpump/.venv/bin/uvicorn paperpump.web:app --host 127.0.0.1 --port 8000 --proxy-headers
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
UNIT

cat > /etc/systemd/system/paperpump-bot.service <<'UNIT'
[Unit]
Description=PaperPump Telegram bot
After=network-online.target
Wants=network-online.target
# Will not start (or loop) until paperpump-set-token creates this marker
ConditionPathExists=/etc/paperpump/token-set

[Service]
User=paperpump
WorkingDirectory=/opt/paperpump
EnvironmentFile=/etc/paperpump.env
ExecStartPre=/bin/rm -f /opt/paperpump/bot.heartbeat
ExecStart=/opt/paperpump/.venv/bin/python -m paperpump.bot
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
UNIT

# Heartbeat watchdog
cat > /usr/local/bin/paperpump-heartbeat-check <<'SH'
#!/bin/bash
# Restart the bot if its heartbeat is older than 30s (only when it should be running).
HB=/opt/paperpump/bot.heartbeat
systemctl is-active --quiet paperpump-bot || exit 0
# grace period right after (re)start
since=$(systemctl show -p ActiveEnterTimestampMonotonic --value paperpump-bot)
now=$(awk '{print int($1*1000000)}' /proc/uptime)
[ $(( (now - since) / 1000000 )) -lt 45 ] && exit 0
if [ ! -f "$HB" ] || [ $(( $(date +%s) - $(stat -c %Y "$HB") )) -gt 30 ]; then
  logger -t paperpump-watchdog "bot heartbeat stale; restarting paperpump-bot"
  systemctl restart paperpump-bot
fi
SH
chmod 755 /usr/local/bin/paperpump-heartbeat-check

cat > /etc/systemd/system/paperpump-watchdog.service <<'UNIT'
[Unit]
Description=PaperPump bot heartbeat check

[Service]
Type=oneshot
ExecStart=/usr/local/bin/paperpump-heartbeat-check
UNIT

cat > /etc/systemd/system/paperpump-watchdog.timer <<'UNIT'
[Unit]
Description=Run PaperPump heartbeat check every 30s

[Timer]
OnBootSec=60
OnUnitActiveSec=30
AccuracySec=1

[Install]
WantedBy=timers.target
UNIT

# Helpers
cat > /usr/local/bin/paperpump-set-token <<'SH'
#!/bin/bash
set -euo pipefail
[ "$(id -u)" -eq 0 ] || { echo "Run as root (sudo)." >&2; exit 1; }
read -r -s -p "Paste TELEGRAM_BOT_TOKEN (input hidden): " TOKEN; echo
[ -n "$TOKEN" ] || { echo "Empty token, aborting." >&2; exit 1; }
tmp=$(mktemp); chmod 600 "$tmp"
grep -v '^TELEGRAM_BOT_TOKEN=' /etc/paperpump.env > "$tmp" || true
printf 'TELEGRAM_BOT_TOKEN=%s\n' "$TOKEN" >> "$tmp"
install -o root -g root -m 600 "$tmp" /etc/paperpump.env; rm -f "$tmp"
install -d -m 755 /etc/paperpump; touch /etc/paperpump/token-set
systemctl daemon-reload
systemctl restart paperpump-bot
sleep 5
systemctl --no-pager --lines=0 status paperpump-bot | head -3
echo "Done. Bot logs: journalctl -u paperpump-bot -f"
SH

cat > /usr/local/bin/paperpump-update <<'SH'
#!/bin/bash
set -euo pipefail
[ "$(id -u)" -eq 0 ] || { echo "Run as root (sudo)." >&2; exit 1; }
cd /opt/paperpump
sudo -u paperpump git pull --ff-only
sudo -u paperpump /opt/paperpump/.venv/bin/pip install -r requirements.txt
systemctl restart paperpump-web
systemctl restart paperpump-bot
systemctl --no-pager is-active paperpump-web paperpump-bot || true
SH
chmod 755 /usr/local/bin/paperpump-set-token /usr/local/bin/paperpump-update

# Firewall + auto updates
ufw allow OpenSSH
ufw allow 80/tcp
ufw allow 443/tcp
ufw --force enable
cat > /etc/apt/apt.conf.d/20auto-upgrades <<'APT'
APT::Periodic::Update-Package-Lists "1";
APT::Periodic::Unattended-Upgrade "1";
APT
systemctl enable --now unattended-upgrades

systemctl daemon-reload
systemctl enable --now paperpump-web
systemctl enable paperpump-bot
systemctl enable --now paperpump-watchdog.timer
systemctl restart caddy
echo "PaperPump setup complete: https://$HOST"
