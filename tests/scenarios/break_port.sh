#!/usr/bin/env bash
# Hold port 8099 with one process, then try to bind it again with a second.
set -euo pipefail

PORT=8099
UNIT=whybroke-port-holder

echo "==> starting a listener that holds ${PORT}"
nohup python3 -m http.server "$PORT" --bind 0.0.0.0 \
  >/var/tmp/whybroke-port-holder.log 2>&1 &
echo $! > /var/tmp/whybroke-port-holder.pid
sleep 1

echo "==> trying to bind ${PORT} a second time (this will fail)"
python3 - "$PORT" <<'PY' || true
import socket, sys
port = int(sys.argv[1])
s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
try:
    s.bind(("0.0.0.0", port))
except OSError as exc:
    print("bind failed: {}".format(exc))
else:
    print("bind unexpectedly succeeded")
finally:
    s.close()
PY

echo
echo "EXPECTED QUESTION: why does my app get 'Address already in use' on port ${PORT}?"
echo "EXPECTED DIAGNOSIS: another process is already listening on ${PORT}."
echo "EXPECTED EVIDENCE: ss -tulnp showing the port bound with its owning PID and"
echo "  process name (python3)"
echo
echo "cleanup: sudo ./fix_all.sh"