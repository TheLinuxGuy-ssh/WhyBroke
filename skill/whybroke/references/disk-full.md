# Disk full

## Telltale evidence

- `write error: No space left on device` in the unit journal
- `df -h` showing 100% or a very high Use% on the mount the service writes to
- `ENOSPC` in any application log

## Commands to check

1. `df -h` for space and `df -i` for inodes. A filesystem can be full of inodes with
   free space left, which produces the same ENOSPC symptom and fools a quick check.
2. Read the failing unit journal. The exact errno is usually there.
3. Confirm which mount the service writes to. `/var/log` is often a separate filesystem
   or a small tmpfs, so a root filesystem with free space does not rule this out.

## Common root causes, in order

1. Unbounded log growth. Journald caps its own files, but a service writing plain files
   under `/var/log` grows forever.
2. A large deleted file still held open by a running process. `df` shows full usage that
   does not drop after a delete, and `lsof +L1` shows the holder.
3. Cached data on a separate `/var` or `/var/log` mount filled by another workload.

## Fixes to suggest

- Clear rotated logs: `sudo journalctl --vacuum-size=200M`, or remove old rotated files.
- For unrotated logs, set a rotation rule in `/etc/logrotate.d/` rather than deleting
  files by hand.
- If a deleted file is held open, restart the holding process to release it.

## False positives to rule out

A read-only filesystem reports ENOSPC in some applications. Check `mount | grep ro`
before concluding the disk is full.