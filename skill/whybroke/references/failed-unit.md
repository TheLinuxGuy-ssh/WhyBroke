# Failed systemd unit

## Telltale evidence

The status code on the unit tells you which class of failure it is:

- `status=203/EXEC`: the `ExecStart` binary does not exist or is not executable. The most
  common failure by a wide margin.
- `status=200/CHDIR`: the `WorkingDirectory` does not exist.
- `status=226/NAMESPACE`: a sandboxing directive such as `ProtectSystem` or `PrivateTmp`
  blocks something the unit needs.
- `status=1/FAILURE`: the process ran and exited nonzero. Read the journal for the real
  error, the unit status alone will not say.
- `status=0/DEAD` after `systemctl stop`: the unit stopped normally, it did not fail.

## Commands to check

1. `systemctl status <unit> --no-pager -l` for the status code and the failed command.
2. `journalctl -u <unit> -n 100 --no-pager` for the reason from the service itself.
3. `systemctl cat <unit>` to see the unit file as systemd actually parsed it, which
   catches drop-in overrides that changed the effective configuration.
4. `systemctl show <unit> -p ExecStart -p User -p WorkingDirectory` when the file looks
   correct but systemd still fails.

## Common root causes, in order

1. A moved or renamed binary after a package upgrade, leaving `ExecStart` dangling.
2. A `User=` or `Group=` that does not exist, or a file the service cannot read as that
   user.
3. A dependency unit that is not ordered before this one, so the service starts before
   its database or network is ready.

## Fixes to suggest

- Point `ExecStart` at the correct absolute path, then `sudo systemctl daemon-reload`.
- Create the missing user, or correct the `User=` line.
- Add `After=` and `Requires=` for the dependency units.

## False positives to rule out

`systemctl status` shows cached output and exit status, not necessarily live state.
Confirm with `systemctl is-active <unit>` before claiming the unit is down.