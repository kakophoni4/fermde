#!/usr/bin/env bash
# Run on the Linux server only. Does not create or stop phones.
set -euo pipefail
cd /opt/fermde
echo '=== Python import and syntax ==='
venv/bin/python -m compileall -q fermde agent.py
echo '=== Unit tests (server only) ==='
venv/bin/python -m unittest discover -s tests -v
echo '=== Services ==='
systemctl is-active fermde caddy android-display
echo '=== HTTP health ==='
curl -fsS http://127.0.0.1:8090/healthz
echo
echo '=== Agent and GPU ==='
echo '{"action":"stats"}' | runuser -u fermde -- sudo -n /opt/fermde/venv/bin/python /opt/fermde/agent.py
echo '=== Existing listeners ==='
ss -lntp | grep -E ':(80|443|8090|5037|5039|5554|5555|15555)\b' || true
echo '=== Latest panel log ==='
journalctl -u fermde -n 25 --no-pager
echo 'Next: log in, configure FloppyData, create ONE phone and use docs/ACCEPTANCE.md.'
