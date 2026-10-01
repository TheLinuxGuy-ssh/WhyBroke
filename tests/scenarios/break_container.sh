#!/usr/bin/env bash
# Run a container that exits immediately, so it sits in a crashloop.
set -euo pipefail

NAME=whybroke-broken

if ! command -v docker >/dev/null; then
  echo "docker is not installed on this host."
  echo "this scenario is optional, skip it if you have not installed docker."
  exit 0
fi

echo "==> removing any previous ${NAME}"
docker rm -f "$NAME" >/dev/null 2>&1 || true

echo "==> starting a container that exits with code 1 immediately"
docker run -d --name "$NAME" --restart always busybox:latest \
  sh -c 'echo "starting up" >&2; exit 1' >/dev/null

sleep 3

echo "==> container state:"
docker ps -a --filter "name=${NAME}"

echo
echo "EXPECTED QUESTION: why is the ${NAME} container restarting?"
echo "EXPECTED DIAGNOSIS: the container command exits with status 1 immediately after"
echo "  starting, and the restart policy turns that into a crashloop."
echo "EXPECTED EVIDENCE: docker ps -a showing 'Restarting', and docker logs ${NAME}"
echo "  showing 'starting up' with no further output and a nonzero exit"
echo
echo "cleanup: sudo ./fix_all.sh"