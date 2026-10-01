# OOM kill

## Telltale evidence

- The kernel ring buffer reporting `Out of memory: Killed process <pid> (<comm>)`
- The unit exiting with status 137, which is 128 plus SIGKILL
- A container exiting with code 137 while `docker ps` shows no application error
- `dmesg` or `journalctl -k` reporting `oom-kill:constraint=...`

## Commands to check

1. `journalctl -k -n 100` for the OOM line. This is the decisive evidence.
2. `systemctl status <unit>` for the exit status.
3. `docker inspect --format '{{.State.OOMKilled}}' <container>` for containers.
4. `free -h` for current memory and swap. Note that a container can be OOM killed by a
   cgroup limit while the host still has free memory, so check the limit too.

## Common root causes, in order

1. A real memory limit being hit: a systemd `MemoryMax`, a Docker `--memory` flag, or
   the physical host running out.
2. An unbounded cache or array in the application that grew until it hit the ceiling.
3. A memory limit that was correct when written and is now too small for real usage.

## Fixes to suggest

- Raise the limit to match measured need: `MemoryMax=` in the unit, `--memory` for the
  container.
- Add swap if physical memory is the ceiling, with a swap file on a real filesystem.
- Bound the application's own growth, such as a cache size or a batch size.

## False positives to rule out

SIGKILL can also come from `TimeoutStopSec` expiry, a manual `kill -9`, or systemd
stopping the unit. Confirm the kernel logged the OOM kill before concluding.