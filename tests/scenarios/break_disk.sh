#!/usr/bin/env bash
# Fill a small loopback filesystem to 100%, then make a unit fail writing to it.
set -euo pipefail

MOUNT=/mnt/whybroke-test
IMAGE=/var/tmp/whybroke-test.img
SIZE_MB=64
UNIT=whybroke-demo

echo "==> creating ${SIZE_MB}MB loop filesystem at ${MOUNT}"
sudo mkdir -p "$MOUNT"
sudo dd if=/dev/zero of="$IMAGE" bs=1M count="$SIZE_MB" status=none
LOOP=$(sudo losetup --find --show "$IMAGE")
sudo mkfs.ext4 -q -F "$LOOP"
sudo mount "$LOOP" "$MOUNT"

echo "==> filling it to 100%"
sudo dd if=/dev/zero of="${MOUNT}/ballast" bs=1M count=$((SIZE_MB - 4)) status=none

echo "==> installing ${UNIT}.service which writes to the full mount"
sudo tee /etc/systemd/system/${UNIT}.service >/dev/null <<EOF
[Unit]
Description=whybroke demo writer

[Service]
Type=oneshot
ExecStart=/bin/sh -c 'for i in \$(seq 1 50); do echo "line \$i" >> ${MOUNT}/writer.log; done'
EOF
sudo systemctl daemon-reload

echo "==> starting ${UNIT}, it should fail with ENOSPC"
sudo systemctl start ${UNIT} || true

echo
echo "EXPECTED QUESTION: why did ${UNIT} fail?"
echo "EXPECTED DIAGNOSIS: the loop filesystem at ${MOUNT} is 100% full, so the unit's"
echo "  log write failed with 'No space left on device'."
echo "EXPECTED EVIDENCE: systemctl status ${UNIT} -> failed, and journalctl -u ${UNIT}"
echo "  showing the write error; df -h showing 100% on ${MOUNT}"
echo
echo "cleanup: sudo ./fix_all.sh"