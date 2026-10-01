# Expected diagnoses

Each `break_*.sh` script prints its own expected question and diagnosis. This file is the
reference copy plus the scoring table.

## Scoring

A diagnosis is correct when all three hold:

1. The **root cause** names the actual condition, not the symptom.
2. The **evidence** quotes a real line from the machine, and the report attributes it to a
   tool.
3. The **fix** is something the user can run themselves, and it addresses the root cause
   rather than restarting the service and hoping.

## Scenarios

### break_disk.sh

- Question: why did `whybroke-demo` fail?
- Root cause: the 64MB loop filesystem mounted at `/mnt/whybroke-test` is 100% full, so
  the unit's log write failed.
- Decisive evidence: `disk_usage` showing 100% Use% on `/mnt/whybroke-test`; `unit_logs`
  for `whybroke-demo` showing the write failing with `No space left on device`.
- Acceptable fix: remove the ballast file, or `journalctl --vacuum-size`, and add log
  rotation so it does not recur.
- Common failure mode: the agent reports "the disk is full" without attributing the
  percent to the right mount, or suggests deleting logs when the mount is a test loop
  device.

### break_unit.sh

- Question: why is `whybroke-broken-unit` failing?
- Root cause: `ExecStart` points at `/usr/local/bin/whybroke-does-not-exist`, which does
  not exist, so systemd reports `status=203/EXEC`.
- Decisive evidence: `service_status` showing `status=203/EXEC` and `Failed to execute`;
  `unit_logs` naming the missing path.
- Acceptable fix: correct the `ExecStart` path, then `systemctl daemon-reload`.
- Common failure mode: the agent blames the unit file syntax rather than the missing
  binary, which the status code rules out.

### break_port.sh

- Question: why does my app get "Address already in use" on port 8099?
- Root cause: a python3 process is already listening on 8099.
- Decisive evidence: `listening_ports` with `port: 8099` showing the socket bound with its
  owning PID and process name.
- Acceptable fix: stop the holder if stale, or change the port.
- Common failure mode: concluding there is no conflict, which happens if the agent never
  runs `ss`. This scenario is specifically a test of whether it reaches step 4 of the
  investigation order.

### break_container.sh

- Question: why is the `whybroke-broken` container restarting?
- Root cause: the container command exits with status 1 immediately after starting, and
  the restart policy turns that into a crashloop.
- Decisive evidence: `docker_containers` showing `Restarting`; `container_logs` showing
  `starting up` with nothing after it.
- Acceptable fix: correct the command in the container definition, or remove the restart
  policy while debugging.
- Common failure mode: calling it an OOM kill. Exit 137 plus OOMKilled distinguishes it,
  and here the exit code is 1.

### break_oom.sh

- Question: why did the `whybroke-oom` container exit with code 137?
- Root cause: it exceeded its 48MB cgroup memory limit and the kernel OOM killed it.
- Decisive evidence: exit code 137, `OOMKilled=true` from inspect, and the kernel log
  showing `Out of memory: Killed process`.
- Acceptable fix: raise `--memory` to match measured need, or bound the allocation.
- Common failure mode: reporting a generic crash without mentioning the memory limit.
  Exit 137 is SIGKILL and this is the case where it matters.

## Results

Fill this in from actual runs and paste into the README.

| Scenario | Root cause correct | Evidence quoted | Fix correct | Notes |
|---|---|---|---|---|
| disk-full | | | | |
| failed unit | | | | |
| port in use | | | | |
| crashed container | | | | |
| OOM kill | | | | |

## Environment note

The container scenarios need Docker. It was not installed on the development machine, so
both scripts detect that and exit cleanly rather than failing. Run them only if
`docker --version` works.

Disk filling needs `losetup`, `mkfs.ext4` and `mount`, which require sudo. The other two
scenarios do not.