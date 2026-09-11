#!/usr/bin/env bash
# No credentials or proxy URLs are printed.
set -euo pipefail
PHONE_ID="${1:-1}"
[[ "$PHONE_ID" =~ ^[0-9]+$ ]] && (( PHONE_ID >= 1 && PHONE_ID <= 200 )) || exit 1
echo '=== Phone / proxy / DNS ==='
for kind in phone proxy dns; do
  systemctl show "fermde-$kind-$PHONE_ID" -p Id -p ActiveState -p SubState -p ExecMainStatus
done
echo '=== DNS ==='
ip netns exec "fermde-$PHONE_ID" /opt/fermde/venv/bin/python - <<'PY'
import socket, time
started=time.monotonic()
try:
    result=socket.getaddrinfo('api.ipify.org',443,socket.AF_INET,socket.SOCK_STREAM)
    print('DNS OK', sorted({r[4][0] for r in result}), round(time.monotonic()-started,2),'s')
except OSError as error:
    print('DNS FAILED:',error)
PY
echo '=== Exit IP through phone namespace ==='
printf '{"action":"check_proxy","id":%s}\n' "$PHONE_ID" | /opt/fermde/venv/bin/python /opt/fermde/agent.py
