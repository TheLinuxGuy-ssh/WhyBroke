#!/usr/bin/env bash
# Undo everything the break_*.sh scripts did.
set -uo pipefail

MOUNT=/mnt/whybroke-test
IMAGE=/var/tmp/whybroke-test.img

echo "==> stopping and removing demo units"
for UNIT in whybroke-demo whybroke-broken-unit; do
  sudo systemctl stop "$UNIT" 2>/dev/null || true
  sudo systemctl disable "$UNIT" 2>/dev/null || true
  sudo rm -f "/etc/systemd/system/${UNIT}.service"
done
sudo systemctl daemon-reload || true

echo "==> removing demo containers"
for NAME in whybroke-broken whybroke-oom; do
  docker rm -f "$NAME" >/dev/null 2>&1 || true
done

echo "==> killing the port holder"
if [ -f /var/tmp/whybroke-port-holder.pid ]; then
  kill "$(cat /var/tmp/whybroke-port-holder.pid)" 2>/dev/null || true
  rm -f /var/tmp/whybroke-port-holder.pid
fi
rm -f /var/tmp/whybroke-port-holder.log

echo "==> unmounting and deleting the loop filesystem"
if mountpoint -q "$MOUNT" 2>/dev/null; then
  LOOP=$(findmnt -no SOURCE "$MOUNT" 2>/dev/null || true)
  sudo umount "$MOUNT" || sudo umount -l "$MOUNT" || true
  if [ -n "${LOOP:-}" ]; then
    sudo losetup -d "$LOOP" 2>/dev/null || true
  fi
fi
sudo rm -f "$IMAGE"
sudo rmdir "$MOUNT" 2>/dev/null || true

echo
echo "done. verify with: df -h | grep whybroke; systemctl list-units 'whybroke*'"