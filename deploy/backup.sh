#!/usr/bin/env bash
set -euo pipefail
umask 077
install -d -m 0700 /var/backups/fermde
db_file="$(runuser -u fermde -- env HOME=/var/lib/fermde PYTHONPATH=/opt/fermde /opt/fermde/venv/bin/python -m fermde.admin backup)"
backup_file="/var/backups/fermde/config-$(date +%Y%m%d-%H%M%S).tar.gz"
tar -czf "$backup_file" /etc/fermde /etc/caddy/fermde.caddy /etc/systemd/system/fermde.service "$db_file"
echo "$backup_file"
echo 'Configuration and database only. Device disks require an explicit stopped-device backup.'
