# Port already in use

## Telltale evidence

- `bind() to 0.0.0.0:8080 failed (98: Address already in use)` in the journal
- `EADDRINUSE` from the application
- `connection refused` on a port that a socket listing shows as not listening, which
  points at a dead process rather than a conflict

## Commands to check

1. `ss -tulnp` to list listeners and their owning PIDs. Run it before concluding.
2. `ps -p <pid> -o pid,comm,args` to identify the holder.
3. The failing unit journal, to see which bind attempt failed and on which address.

## Common root causes, in order

1. A previous instance of the same service still running. Check the PID's start time
   against the last restart.
2. A different service legitimately bound to that port, often a default on 80 or 443.
3. The process holding the port is in `TIME_WAIT`. This shows as a socket with no owning
   process; it clears on its own and does not need intervention.

## Fixes to suggest

- Stop the holder if it is a stale instance: `sudo systemctl stop <other-unit>`.
- Change the listening port in the service configuration and restart.
- For `TIME_WAIT`, wait it out rather than killing anything.

## False positives to rule out

`ss` without `-p` hides owning processes for sockets you do not own. If the listing
looks empty but the bind still fails, re-run as root before ruling out a conflict.