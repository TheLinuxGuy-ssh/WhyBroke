#!/usr/bin/env bash
# Run a container with a hard memory ceiling and allocate past it, forcing an OOM kill.
set -euo pipefail

NAME=whybroke-oom

if ! command -v docker >/dev/null; then
  echo "docker is not installed on this host."
  echo "this scenario is optional, skip it if you have not installed docker."
  exit 0
fi

echo "==> removing any previous ${NAME}"
docker rm -f "$NAME" >/dev/null 2>&1 || true

echo "==> starting a container capped at 48MB that tries to allocate 512MB"
docker run -d --name "$NAME" --memory 48m --memory-swap 48m \
  python:3.12-alpine \
  python3 -c 'x = bytearray(512 * 1024 * 1024); print("allocated", len(x))' >/dev/null

echo "==> waiting for it to be killed"
for _ in $(seq 1 20); do
  STATE=$(docker inspect --format '{{.State.OOMKilled}} {{.State.ExitCode}}' "$NAME" 2>/dev/null || echo "running")
  echo "  oomkilled/exitcode: ${STATE}"
  if ! echo "$STATE" | grep -q '^false 0$'; then
    break
  fi
  sleep 1
done

echo "==> final state:"
docker ps -a --filter "name=${NAME}"
echo "--- logs ---"
docker logs --tail 20 "$NAME" || true

echo
echo "EXPECTED QUESTION: why did the ${NAME} container exit with code 137?"
echo "EXPECTED DIAGNOSIS: it exceeded its 48MB cgroup memory limit and the kernel"
echo "  OOM killed it, which docker reports as exit code 137."
echo "EXPECTED EVIDENCE: docker inspect reporting OOMKilled=true with exit code 137,"
echo "  and the kernel log showing 'Out of memory: Killed process'"
echo
echo "cleanup: sudo ./fix_all.sh"