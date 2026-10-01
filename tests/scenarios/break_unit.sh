#!/usr/bin/env bash
# Install a unit whose ExecStart points at a binary that does not exist.
set -euo pipefail

UNIT=whybroke-broken-unit

echo "==> installing ${UNIT}.service with a nonexistent ExecStart"
sudo tee /etc/systemd/system/${UNIT}.service >/dev/null <<EOF
[Unit]
Description=whybroke demo unit with a dangling ExecStart

[Service]
Type=oneshot
ExecStart=/usr/local/bin/whybroke-does-not-exist --serve
EOF
sudo systemctl daemon-reload

echo "==> starting it"
sudo systemctl start ${UNIT} || true

echo
echo "EXPECTED QUESTION: why is ${UNIT} failing?"
echo "EXPECTED DIAGNOSIS: ExecStart points at /usr/local/bin/whybroke-does-not-exist,"
echo "  which does not exist, so systemd reports status=203/EXEC."
echo "EXPECTED EVIDENCE: systemctl status ${UNIT} showing status=203/EXEC and"
echo "  'Failed to execute', plus journalctl -u ${UNIT} naming the missing path"
echo
echo "cleanup: sudo ./fix_all.sh"