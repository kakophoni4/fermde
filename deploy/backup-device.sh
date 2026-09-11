#!/usr/bin/env bash
set -euo pipefail
umask 077
device_id="${1:-}"
[[ "$device_id" =~ ^[0-9]+$ ]] && (( device_id >= 1 && device_id <= 200 )) || { echo 'Device ID must be 1..200'; exit 1; }
exec 9>"/run/lock/fermde-device-$device_id.lock"
flock -x 9
if systemctl is-active --quiet "fermde-phone-$device_id.service"; then echo 'Stop this device explicitly in the panel first'; exit 1; fi
test -f "/var/lib/fermde-agent/$device_id/manifest.json"
install -d -m 0700 /var/backups/fermde
tar -czf "/var/backups/fermde/device-$device_id-$(date +%Y%m%d-%H%M%S).tar.gz" -C /var/lib/fermde-agent "$device_id"
echo 'Stopped device backup created. No running devices were stopped.'
