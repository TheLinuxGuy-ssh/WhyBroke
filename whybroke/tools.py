import os
import shutil
import subprocess
from dataclasses import dataclass, field
from typing import Callable, Dict, List, Optional

from . import config, safety


@dataclass(frozen=True)
class ToolSpec:
    name: str
    description: str
    parameters: dict
    read_only: bool
    build: Callable[[dict], List[str]]
    validators: Dict[str, Callable] = field(default_factory=dict)
    timeout_s: int = config.TOOL_TIMEOUT_S
    execute: Optional[Callable[[dict], str]] = None
    postprocess: Optional[Callable[[str, dict], str]] = None


def run_tool(spec, args):
    """Validate, execute, redact, truncate. Returns (text, redaction_count)."""
    cleaned = safety.validate(spec, args)

    if spec.execute is not None:
        raw = spec.execute(cleaned)
    else:
        raw = run_argv(spec.build(cleaned), spec.timeout_s)

    if spec.postprocess is not None:
        raw = spec.postprocess(raw, cleaned)

    cleaned_text, redacted = safety.redact(raw)
    return safety.truncate(cleaned_text), redacted


def run_argv(argv, timeout):
    try:
        proc = subprocess.run(
            argv,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except FileNotFoundError:
        return "TOOL UNAVAILABLE: {} is not installed on this host".format(argv[0])
    except subprocess.TimeoutExpired:
        return "TOOL TIMEOUT: exceeded {}s limit for {}".format(timeout, argv[0])
    except PermissionError:
        return "TOOL ERROR: permission denied running {}".format(argv[0])

    if proc.returncode == 0:
        return proc.stdout.strip() or "(command succeeded with no output)"

    detail = (proc.stderr or proc.stdout).strip()
    return "TOOL ERROR (exit {}): {}".format(proc.returncode, detail or "no output")


def tail_lines(text, count):
    lines = text.splitlines()
    if len(lines) <= count:
        return text
    return "\n".join(lines[-count:])


def filter_port(text, port):
    if not port:
        return text
    needle = ":{}".format(port)
    matches = [line for line in text.splitlines() if needle in line]
    return "\n".join(matches) or "(no sockets found on port {})".format(port)


def read_log_file(args):
    path = args["path"]
    lines = args.get("tail_lines", 50)
    try:
        with open(path, "r", errors="replace") as handle:
            content = handle.read()
    except OSError as exc:
        return "TOOL ERROR: {}".format(exc)
    if not content.strip():
        return "(file is empty)"
    return tail_lines(content, lines)


def _human(num):
    value = float(num)
    for unit in ("B", "K", "M", "G", "T"):
        if value < 1024 or unit == "T":
            return "{:.1f}{}".format(value, unit) if unit != "B" else "{:.0f}B".format(value)
        value /= 1024
    return "{:.1f}T".format(value)


def socket_inodes_for_port(port):
    """Inodes of sockets bound to `port`, read from /proc so we need no privileges."""
    wanted = format(port, "04X")
    inodes = []
    for table in ("/proc/net/tcp", "/proc/net/tcp6"):
        try:
            with open(table) as handle:
                rows = handle.read().splitlines()[1:]
        except OSError:
            continue
        for row in rows:
            fields = row.split()
            if len(fields) < 10:
                continue
            local = fields[1].rsplit(":", 1)[-1]
            if local == wanted and fields[3] == "0A":
                inodes.append(fields[9])
    return inodes


def pid_owning_inodes(inodes):
    """Map socket inodes to (pid, command) by walking /proc.

    `ss -p` hides owners it cannot see, which silently turns a useful answer
    into a generic one. Doing the lookup ourselves keeps the evidence honest
    even when the port belongs to another user.
    """
    if not inodes:
        return []
    wanted = {"socket:[{}]".format(inode) for inode in inodes}
    owners = []
    for entry in os.listdir("/proc"):
        if not entry.isdigit():
            continue
        fd_dir = os.path.join("/proc", entry, "fd")
        try:
            fds = os.listdir(fd_dir)
        except OSError:
            continue
        for fd in fds:
            try:
                target = os.readlink(os.path.join(fd_dir, fd))
            except OSError:
                continue
            if target in wanted:
                owners.append(entry)
                break
    resolved = []
    for pid in owners:
        try:
            with open("/proc/{}/cmdline".format(pid), "rb") as handle:
                cmd = handle.read().replace(b"\0", b" ").decode(errors="replace").strip()
        except OSError:
            cmd = "?"
        resolved.append((pid, cmd))
    return resolved


def annotate_socket_owners(text, port):
    if not port:
        return text
    inodes = socket_inodes_for_port(port)
    owners = pid_owning_inodes(inodes)
    note = ["", "port {} owner:".format(port)]
    if owners:
        for pid, cmd in owners[:5]:
            note.append("  pid {} -> {}".format(pid, cmd or "(unknown)"))
    else:
        note.append(
            "  no owning process visible to this user; the listener belongs to "
            "another user or a process that just exited"
        )
    return text + "\n" + "\n".join(note)


def run_argv_quiet(argv, timeout, drop_stderr=True):
    """Run argv and keep stdout even when the exit code is nonzero.

    Needed for du and find: a single unreadable subdirectory must not abort
    the whole listing, because partial results are still useful evidence.
    """
    try:
        proc = subprocess.run(
            argv,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL if drop_stderr else subprocess.PIPE,
            text=True,
            timeout=timeout,
        )
    except FileNotFoundError:
        return "TOOL UNAVAILABLE: {} is not installed on this host".format(argv[0])
    except subprocess.TimeoutExpired:
        return "TOOL TIMEOUT: exceeded {}s limit for {}".format(timeout, argv[0])
    except PermissionError:
        return "TOOL ERROR: permission denied running {}".format(argv[0])

    out = proc.stdout.strip()
    if not out:
        detail = (proc.stderr or "").strip()
        if detail and not drop_stderr:
            return "TOOL ERROR (exit {}): {}".format(proc.returncode, detail)
        return ""
    return out


def scan_disk_usage(args):
    """`du` sorted in Python. No pipe, so there is nothing for the model to inject."""
    path = args["path"]
    depth = args.get("depth", 1)
    limit = args.get("limit", 20)

    raw = run_argv_quiet(
        ["du", "-x", "--max-depth", str(depth), "-B1", path], 20
    )
    if raw.startswith("TOOL "):
        return raw
    if not raw:
        return "(no readable entries found under {})".format(path)

    rows = []
    grand_total = None
    for line in raw.splitlines():
        parts = line.split("\t", 1)
        if len(parts) != 2:
            continue
        try:
            size = int(parts[0].strip())
        except ValueError:
            continue
        target = parts[1].strip()
        if target == path:
            grand_total = size
            continue
        rows.append((size, target))

    if grand_total is None:
        grand_total = sum(size for size, _ in rows)

    # du at depth 1 reports subdirectories only, so loose files at the top
    # level are invisible to it. Merge them in or the total lies.
    loose = run_argv_quiet(
        ["find", path, "-maxdepth", "1", "-xdev", "-type", "f", "-printf", "%s\t%p\n"],
        10,
    )
    for line in loose.splitlines() if loose else []:
        parts = line.split("\t", 1)
        if len(parts) != 2:
            continue
        try:
            rows.append((int(parts[0].strip()), parts[1].strip()))
        except ValueError:
            continue

    rows.sort(reverse=True)
    lines = ["total {} in {}".format(_human(grand_total), path), ""]
    if not rows:
        lines.append("(nothing found, directory may be empty or unreadable)")
    for size, target in rows[:limit]:
        lines.append("{:>9}  {}".format(_human(size), target))
    return "\n".join(lines)


def largest_files(args):
    """Largest individual files under a directory, newest-independent, sorted by size."""
    path = args["path"]
    limit = args.get("limit", 10)
    min_kb = args.get("min_size_kb", 0)

    raw = run_argv_quiet(
        [
            "find", path,
            "-xdev",
            "-type", "f",
            "-size", "+{}k".format(min_kb),
            "-printf", "%s\t%TY-%Tm-%Td\t%p\n",
        ],
        20,
    )
    if raw.startswith("TOOL "):
        return raw
    if not raw:
        return "(no files larger than {}KB found under {})".format(min_kb, path)

    rows = []
    for line in raw.splitlines():
        fields = line.split("\t")
        if len(fields) != 3:
            continue
        try:
            rows.append((int(fields[0]), fields[1], fields[2]))
        except ValueError:
            continue

    rows.sort(reverse=True)
    lines = [
        "largest files under {} ({} found, showing {}):".format(path, len(rows), min(len(rows), limit)),
        "",
    ]
    if not rows:
        lines.append("(nothing readable)")
    for size, mtime, target in rows[:limit]:
        lines.append("{:>9}  {}  {}".format(_human(size), mtime, target))
    return "\n".join(lines)


def _obj(properties, required):
    return {"type": "object", "properties": properties, "required": required}


def _string(description):
    return {"type": "string", "description": description}


def _integer(description):
    return {"type": "integer", "description": description}


REGISTRY: Dict[str, ToolSpec] = {}


def register(spec):
    REGISTRY[spec.name] = spec
    return spec


register(
    ToolSpec(
        name="service_status",
        description="Show the current status of a systemd unit. Use this first when the problem involves a service.",
        parameters=_obj(
            {"unit": _string("unit name, for example nginx.service")}, ["unit"]
        ),
        read_only=True,
        build=lambda a: ["systemctl", "status", a["unit"], "--no-pager", "-l"],
        validators={"unit": safety.check_unit},
    )
)

register(
    ToolSpec(
        name="unit_logs",
        description="Read the most recent journal entries for a systemd unit. Use this right after service_status.",
        parameters=_obj(
            {
                "unit": _string("unit name, for example nginx.service"),
                "lines": _integer("recent lines to return, 1 to 200"),
            },
            ["unit"],
        ),
        read_only=True,
        build=lambda a: [
            "journalctl",
            "-u", a["unit"],
            "-n", str(a.get("lines", 50)),
            "--no-pager",
            "--output", "short-iso",
        ],
        validators={
            "unit": safety.check_unit,
            "lines": safety.int_arg("lines", 1, config.MAX_LOG_LINES),
        },
        timeout_s=15,
    )
)

register(
    ToolSpec(
        name="error_logs",
        description="Read system wide error level journal entries. Use when the failing component is unknown.",
        parameters=_obj(
            {
                "since": _string(
                    "time window, one of " + ", ".join(config.JOURNAL_SINCE_CHOICES)
                ),
                "lines": _integer("recent lines to return, 1 to 200"),
            },
            [],
        ),
        read_only=True,
        build=lambda a: [
            "journalctl",
            "-p", "err",
            "--since", a.get("since", "1h"),
            "-n", str(a.get("lines", 80)),
            "--no-pager",
            "--output", "short-iso",
        ],
        validators={
            "since": safety.check_since,
            "lines": safety.int_arg("lines", 1, config.MAX_LOG_LINES),
        },
        timeout_s=15,
    )
)

register(
    ToolSpec(
        name="kernel_logs",
        description="Read recent kernel messages. Use for OOM kills, disk errors, and hardware faults.",
        parameters=_obj(
            {"lines": _integer("recent lines to return, 1 to 200")}, []
        ),
        read_only=True,
        build=lambda a: [
            "journalctl",
            "-k",
            "-n", str(a.get("lines", 60)),
            "--no-pager",
            "--output", "short-iso",
        ],
        validators={"lines": safety.int_arg("lines", 1, config.MAX_LOG_LINES)},
        timeout_s=15,
    )
)

register(
    ToolSpec(
        name="disk_usage",
        description="Show filesystem usage and inode usage for all mounted filesystems. Use when disk space or failed writes are suspected.",
        parameters=_obj({}, []),
        read_only=True,
        build=lambda a: ["df", "-h"],
        validators={},
    )
)

register(
    ToolSpec(
        name="scan_disk_usage",
        description="Find what is taking the most space inside one directory, largest first. Use when the user asks what is filling a folder such as their home directory or Downloads.",
        parameters=_obj(
            {
                "path": _string(
                    "absolute path to an existing directory, for example "
                    + os.path.expanduser("~")
                ),
                "depth": _integer("how deep to descend, 1 or 2"),
                "limit": _integer("how many entries to report, 1 to 50"),
            },
            ["path"],
        ),
        read_only=True,
        build=lambda a: [],
        validators={
            "path": safety.check_disk_scan_path,
            "depth": safety.int_arg("depth", 1, 2),
            "limit": safety.int_arg("limit", 1, 50),
        },
        execute=scan_disk_usage,
        timeout_s=25,
    )
)

register(
    ToolSpec(
        name="largest_files",
        description="List the largest individual files inside a directory, biggest first, with size and modified date. Use when the user asks which file is taking the most space, such as in their Downloads folder.",
        parameters=_obj(
            {
                "path": _string(
                    "absolute path to an existing directory, for example "
                    + os.path.expanduser("~")
                    + "/Downloads"
                ),
                "limit": _integer("how many files to list, 1 to 50"),
                "min_size_kb": _integer("ignore files smaller than this, in KB"),
            },
            ["path"],
        ),
        read_only=True,
        build=lambda a: [],
        validators={
            "path": safety.check_disk_scan_path,
            "limit": safety.int_arg("limit", 1, 50),
            "min_size_kb": safety.int_arg("min_size_kb", 0, 1024 * 1024),
        },
        execute=largest_files,
        timeout_s=25,
    )
)

register(
    ToolSpec(
        name="memory_usage",
        description="Show total, used, and available memory plus swap. Use when memory pressure is suspected.",
        parameters=_obj({}, []),
        read_only=True,
        build=lambda a: ["free", "-h"],
    )
)

register(
    ToolSpec(
        name="system_load",
        description="Show uptime and load averages. Use when the host feels slow.",
        parameters=_obj({}, []),
        read_only=True,
        build=lambda a: ["uptime"],
    )
)

register(
    ToolSpec(
        name="top_processes",
        description="List the most memory and CPU hungry processes. Use when the host is slow or unresponsive.",
        parameters=_obj({"limit": _integer("how many processes, 1 to 30")}, []),
        read_only=True,
        build=lambda a: [
            "ps", "-eo", "pid,ppid,comm,%mem,%cpu,etime", "--sort=-%mem", "--no-headers",
        ],
        validators={"limit": safety.int_arg("limit", 1, 30)},
        postprocess=lambda text, args: tail_lines(text, args.get("limit", 25)),
    )
)

register(
    ToolSpec(
        name="listening_ports",
        description="List listening TCP and UDP sockets with owning processes. Use for connection refused or port conflict problems.",
        parameters=_obj(
            {"port": _integer("optional port number to filter, 1 to 65535")}, []
        ),
        read_only=True,
        build=lambda a: ["ss", "-tulnp"],
        validators={"port": safety.optional_port},
        postprocess=lambda text, args: tail_lines(
            annotate_socket_owners(filter_port(text, args.get("port")), args.get("port")),
            60,
        ),
    )
)

register(
    ToolSpec(
        name="docker_containers",
        description="List all docker containers with state and exit codes. Use when the problem involves containers.",
        parameters=_obj({}, []),
        read_only=True,
        build=lambda a: [
            "docker", "ps", "-a",
            "--format", "table {{.Names}}\t{{.Status}}\t{{.Image}}\t{{.Ports}}",
        ],
        validators={},
    )
)

register(
    ToolSpec(
        name="container_logs",
        description="Read the last lines of a docker container log. Use after docker_containers finds a failing container.",
        parameters=_obj(
            {
                "container": _string("container name"),
                "lines": _integer("trailing log lines, 1 to 200"),
            },
            ["container"],
        ),
        read_only=True,
        build=lambda a: [
            "docker", "logs", "--tail", str(a.get("lines", 50)), a["container"],
        ],
        validators={
            "container": safety.check_container,
            "lines": safety.int_arg("lines", 1, config.MAX_LOG_LINES),
        },
        timeout_s=15,
    )
)

register(
    ToolSpec(
        name="read_log_file",
        description="Read the tail of a log file under /var/log. Use when the relevant log is not a systemd unit journal.",
        parameters=_obj(
            {
                "path": _string("absolute path to a file under /var/log"),
                "tail_lines": _integer("trailing lines to return, 1 to 200"),
            },
            ["path"],
        ),
        read_only=True,
        build=lambda a: [],
        validators={
            "path": safety.check_log_path,
            "tail_lines": safety.int_arg("tail_lines", 1, config.MAX_FILE_LINES),
        },
        execute=read_log_file,
    )
)


def schemas():
    return [
        {
            "type": "function",
            "function": {
                "name": spec.name,
                "description": spec.description,
                "parameters": spec.parameters,
            },
        }
        for spec in REGISTRY.values()
    ]


def docker_available():
    return shutil.which("docker") is not None