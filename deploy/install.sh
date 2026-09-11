#!/usr/bin/env bash
set -euo pipefail
umask 022
if [[ $EUID -ne 0 ]]; then echo 'Run as root'; exit 1; fi
PUBLIC_HOST="${1:-}"
if [[ ! "$PUBLIC_HOST" =~ ^[a-zA-Z0-9][a-zA-Z0-9.-]+$ ]]; then
  echo 'Usage: bash deploy/install.sh panel.example.com OR public-IP'; exit 1
fi
SOURCE_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
if [[ "$SOURCE_DIR" == /opt/fermde ]]; then echo 'Keep the Git checkout at /opt/fermde-src, outside the runtime'; exit 1; fi
if ! command -v nvidia-smi >/dev/null || [[ ! -e /dev/kvm ]]; then echo 'Install NVIDIA driver and enable KVM first'; exit 1; fi
if [[ ! -x /home/android/sdk/emulator/emulator ]]; then echo 'The prepared SDK is required at /home/android/sdk (see README)'; exit 1; fi
if ! systemctl cat android-display.service >/dev/null 2>&1; then echo 'The prepared NVIDIA android-display.service is required'; exit 1; fi
apt-get update
apt-get install -y python3-venv python3-pip curl unzip rsync nftables iptables iproute2 socat sudo ca-certificates gnupg debian-keyring debian-archive-keyring
getent group render >/dev/null || groupadd --system render
id fermde >/dev/null 2>&1 || useradd --system --create-home --home-dir /var/lib/fermde --shell /usr/sbin/nologin fermde
install -d -o fermde -g fermde -m 0700 /var/lib/fermde
install -d -m 0711 /var/lib/fermde-agent
install -d -o root -g fermde -m 0750 /etc/fermde
install -d -m 0755 /opt/fermde
systemctl stop fermde.service 2>/dev/null || true
rsync -a --exclude=.git --exclude=venv --exclude=vendor --exclude=__pycache__ --exclude='Start-Android.*' "$SOURCE_DIR/" /opt/fermde/
chown -R root:root /opt/fermde
python3 -m venv /opt/fermde/venv
/opt/fermde/venv/bin/pip install -r /opt/fermde/requirements.txt
python3 /opt/fermde/deploy/download-assets.py
if [[ -e /opt/fermde-sdk && ! -L /opt/fermde-sdk ]]; then echo '/opt/fermde-sdk already exists and is not a symlink'; exit 1; fi
ln -sfn /home/android/sdk /opt/fermde-sdk
# Android data under /home/android/.android remains untouched.
chmod o+x /home/android
chmod -R a+rX /home/android/sdk
if [[ ! -f /etc/fermde/secret.key ]]; then
  /opt/fermde/venv/bin/python -c 'from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())' > /etc/fermde/secret.key
fi
chown root:fermde /etc/fermde/secret.key
chmod 0640 /etc/fermde/secret.key
if [[ ! -f /etc/fermde/limits.json ]]; then
  echo '{"max_running":4,"reserve_mib":8192,"headroom_mib":1024,"device_budget_mib":1536}' > /etc/fermde/limits.json
fi
chmod 0600 /etc/fermde/limits.json
cat > /etc/sudoers.d/fermde <<'SUDO'
Defaults:fermde !requiretty
fermde ALL=(root) NOPASSWD: /opt/fermde/venv/bin/python /opt/fermde/agent.py
SUDO
chmod 0440 /etc/sudoers.d/fermde
visudo -cf /etc/sudoers.d/fermde
echo 'net.ipv4.ip_forward=1' > /etc/sysctl.d/80-fermde.conf
sysctl -w net.ipv4.ip_forward=1
cat > /etc/fermde/panel.env <<ENV
FERMDE_ORIGIN=https://$PUBLIC_HOST
FERMDE_DATA=/var/lib/fermde
ENV
chown root:fermde /etc/fermde/panel.env
chmod 0640 /etc/fermde/panel.env
cat > /etc/systemd/system/fermde.service <<'UNIT'
[Unit]
Description=Fermde Android panel
After=network-online.target fermde-adb.service
Wants=network-online.target fermde-adb.service
[Service]
User=fermde
Group=fermde
WorkingDirectory=/opt/fermde
Environment=HOME=/var/lib/fermde
Environment=ADB_SERVER_SOCKET=tcp:127.0.0.1:5039
EnvironmentFile=/etc/fermde/panel.env
ExecStart=/opt/fermde/venv/bin/uvicorn fermde.app:app --host 127.0.0.1 --port 8090 --workers 1 --proxy-headers --forwarded-allow-ips 127.0.0.1 --ws-max-size 32768
Restart=on-failure
RestartSec=3
UMask=0077
PrivateTmp=false
TimeoutStopSec=250
[Install]
WantedBy=multi-user.target
UNIT
# Do not enable NoNewPrivileges: the narrow sudo agent intentionally creates namespaces.
cat > /etc/systemd/system/fermde-adb.service <<'UNIT'
[Unit]
Description=Fermde private ADB server
After=network.target
[Service]
User=fermde
Group=fermde
Environment=HOME=/var/lib/fermde
UnsetEnvironment=ADB_SERVER_SOCKET
ExecStart=/opt/fermde-sdk/platform-tools/adb -L tcp:127.0.0.1:5039 nodaemon server
Restart=on-failure
RestartSec=2
UMask=0077
[Install]
WantedBy=multi-user.target
UNIT
systemctl daemon-reload
systemctl enable --now fermde-adb.service
adb_ready=false
for attempt in {1..30}; do
  if runuser -u fermde -- env HOME=/var/lib/fermde ADB_SERVER_SOCKET=tcp:127.0.0.1:5039 /opt/fermde-sdk/platform-tools/adb devices >/dev/null 2>&1; then
    adb_ready=true
    break
  fi
  sleep 1
done
if [[ "$adb_ready" != true ]]; then
  journalctl -u fermde-adb.service -n 30 --no-pager
  echo 'Private ADB server did not become ready; installation stopped.'
  exit 1
fi
runuser -u fermde -- env HOME=/var/lib/fermde PYTHONPATH=/opt/fermde /opt/fermde/venv/bin/python -m fermde.admin bootstrap
if ! command -v caddy >/dev/null; then
  curl -fsSL https://dl.cloudsmith.io/public/caddy/stable/gpg.key | gpg --dearmor --yes -o /usr/share/keyrings/caddy-stable-archive-keyring.gpg
  curl -fsSL https://dl.cloudsmith.io/public/caddy/stable/debian.deb.txt > /etc/apt/sources.list.d/caddy-stable.list
  apt-get update
  apt-get install -y caddy
fi
if [[ -f /etc/caddy/Caddyfile && ! -f /etc/caddy/Caddyfile.before-fermde ]]; then
  cp -a /etc/caddy/Caddyfile /etc/caddy/Caddyfile.before-fermde
fi
if [[ -f /etc/caddy/Caddyfile ]] && grep -qvE '^\s*(#.*)?$' /etc/caddy/Caddyfile && ! grep -q 'import /etc/caddy/fermde.caddy' /etc/caddy/Caddyfile; then
  # Keep existing virtual hosts. Default distribution :80 block is harmless for domains,
  # but explicit sites with the same address must be resolved by the administrator.
  echo 'import /etc/caddy/fermde.caddy' >> /etc/caddy/Caddyfile
elif [[ ! -f /etc/caddy/Caddyfile ]] || ! grep -q 'import /etc/caddy/fermde.caddy' /etc/caddy/Caddyfile; then
  echo 'import /etc/caddy/fermde.caddy' >> /etc/caddy/Caddyfile
fi
TLS_LINE=''
if [[ "$PUBLIC_HOST" =~ ^[0-9]+\.[0-9]+\.[0-9]+\.[0-9]+$ ]]; then TLS_LINE='tls internal'; fi
cat > /etc/caddy/fermde.caddy <<CADDY
https://$PUBLIC_HOST {
    $TLS_LINE
    request_body {
        max_size 520MB
    }
    reverse_proxy 127.0.0.1:8090 {
        flush_interval -1
    }
}
CADDY
caddy validate --config /etc/caddy/Caddyfile
systemctl daemon-reload
systemctl enable --now fermde.service
systemctl reload-or-restart caddy.service
install -m 0750 /opt/fermde/deploy/backup.sh /usr/local/sbin/fermde-backup
cat > /etc/systemd/system/fermde-backup.service <<'UNIT'
[Unit]
Description=Fermde configuration backup
[Service]
Type=oneshot
ExecStart=/usr/local/sbin/fermde-backup
UNIT
cat > /etc/systemd/system/fermde-backup.timer <<'UNIT'
[Unit]
Description=Daily Fermde backup
[Timer]
OnCalendar=daily
Persistent=true
[Install]
WantedBy=timers.target
UNIT
systemctl daemon-reload
systemctl enable --now fermde-backup.timer
echo "Panel: https://$PUBLIC_HOST"
echo 'Existing phone was not changed. No managed devices start automatically after server reboot.'
if [[ -n "$TLS_LINE" ]]; then
  echo 'IP mode uses a private CA. Trust its public root certificate on each client (README).'
  echo 'Certificate: /var/lib/caddy/.local/share/caddy/pki/authorities/local/root.crt'
fi
echo 'Next: bash /opt/fermde/deploy/check-server.sh'
