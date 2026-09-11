#!/usr/bin/env bash
# Update an existing installation. Phone services keep running.
set -euo pipefail
[[ $EUID -eq 0 ]] || { echo 'Run as root'; exit 1; }
SOURCE_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
[[ "$SOURCE_DIR" != /opt/fermde ]] || { echo 'Run from /opt/fermde-src'; exit 1; }
cd "$SOURCE_DIR"
/opt/fermde/venv/bin/python -m compileall -q fermde agent.py dns_forwarder.py
/opt/fermde/venv/bin/python -m unittest discover -s tests -v
# Retain the last deployed code; configuration, secrets and AVDs are separate.
BACKUP_DIR="$(mktemp -d /opt/fermde-code-backup.XXXXXXXX)"
cp -a /opt/fermde/fermde /opt/fermde/static /opt/fermde/agent.py "$BACKUP_DIR/"
[[ ! -f /opt/fermde/dns_forwarder.py ]] || cp -a /opt/fermde/dns_forwarder.py "$BACKUP_DIR/"
systemctl stop fermde
trap 'systemctl start fermde' EXIT
install -o root -g root -m 0644 agent.py dns_forwarder.py /opt/fermde/
rsync -a --chown=root:root --exclude=__pycache__ fermde/ /opt/fermde/fermde/
rsync -a --chown=root:root static/ /opt/fermde/static/
rsync -a --chown=root:root --exclude=__pycache__ tests/ /opt/fermde/tests/
rsync -a --chown=root:root deploy/ /opt/fermde/deploy/
systemctl start fermde
trap - EXIT
for attempt in {1..20}; do
  if curl -fsS http://127.0.0.1:8090/healthz; then
    printf '\nUpdated. Previous code: %s\n' "$BACKUP_DIR"
    echo 'Stop and start a phone from the panel to apply its new DNS settings.'
    exit 0
  fi
  sleep 1
done
echo 'Panel health check failed. Inspect journalctl -u fermde -n 50 --no-pager.'
exit 1
