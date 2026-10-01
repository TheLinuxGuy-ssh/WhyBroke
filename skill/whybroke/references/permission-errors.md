# Permission errors

## Telltale evidence

- `Permission denied` from a syscall, with the errno in the journal
- `status=13/PERMISSION` or `status=200/CHDIR` on a unit that should be readable
- A container failing with a bind mount error, where the host path exists but is not
  visible inside the container

## Commands to check

1. Read the journal for the exact path that was denied. The error names the path.
2. `ls -l <path>` for the mode and owner.
3. `namei -l <path>` to walk every directory component. Most permission failures are in
   a parent directory, not the file itself, so checking only the file misleads you.
4. `systemctl show <unit> -p User -p Group` to learn which identity the service runs as.
5. `getenforce` on SELinux systems, where denial happens despite correct POSIX modes.

## Common root causes, in order

1. A parent directory lacks execute permission for the service user, so the file is
   unreachable no matter its own mode.
2. The unit runs as a different user than the file owner, which is common after a package
   changes its service account.
3. SELinux or AppArmor denying an access that POSIX permissions allow.
4. An immutable file, reported as `Operation not permitted` rather than
   `Permission denied`.

## Fixes to suggest

- Grant the narrowest permission that works: the specific group, or `chmod o+x` on a
  parent directory.
- Correct `User=` and `Group=` in the unit to match the file ownership.
- For SELinux, restore the default context with `restorecon`, or check the audit log with
  `ausearch -m avc` before changing policy.

## False positives to rule out

`Permission denied` from `sudo` means the user is not in `sudoers`, which is a different
problem from a service unable to read a file. Check which user produced the error.