# Crashed container

## Telltale evidence

- `docker ps -a` showing `Restarting` or `Exited (1)` a short time after start
- Exit code 137 with no application error, which indicates an OOM kill rather than a
  crash
- A dependency failing at startup, so the container exits before serving

## Commands to check

1. `docker ps -a` for state, exit code, and image.
2. `docker logs --tail 200 <container>` for the actual failure. This is the decisive step
   and it is the one most often skipped.
3. `docker inspect --format '{{.State.OOMKilled}} {{.State.ExitCode}}' <container>` to
   separate an OOM kill from an application crash.
4. `journalctl -k -n 50` if the exit code is 137.

## Common root causes, in order

1. The entrypoint command does not exist or is not executable in that image. Exit code
   127 or 126 points here.
2. A crashloop from the application exiting nonzero immediately, often a missing
   environment variable or an unmigrated database schema.
3. OOM killed under a `--memory` limit too small for the workload.

## Fixes to suggest

- Correct the command in the container definition and redeploy.
- Supply the missing environment variable or secret.
- Raise the memory limit if the inspect output reports OOMKilled true.

## False positives to rule out

An empty log does not mean the container is healthy. It often means the entrypoint failed
before writing anything. Check the exit code and inspect output before retrying.