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
        description="Show filesystem and inode usage for all mounted filesystems. Use when disk space or failed writes are suspected.",
        parameters=_obj({}, []),
        read_only=True,
        build=lambda a: ["df", "-h"],
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
            filter_port(text, args.get("port")), 60
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