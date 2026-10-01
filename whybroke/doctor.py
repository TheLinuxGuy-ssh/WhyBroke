"""Environment and self-test check that needs no model.

This exists so the project stays demonstrable in the worst case: if Ollama is not
installed, the model never arrives, or no time is left, `python -m whybroke --doctor`
still proves the safety layer and every tool actually work against the real machine.
"""

import os
import shutil
import subprocess
import sys
import tempfile

from . import config, safety, tools

PASS = "PASS"
WARN = "WARN"
FAIL = "FAIL"
SKIP = "SKIP"


class Result:
    def __init__(self, name, status, detail):
        self.name = name
        self.status = status
        self.detail = detail

    def line(self):
        return "{:<5} {:<34} {}".format(self.status, self.name, self.detail)


def _probe_units():
    candidates = (
        "sshd.service",
        "cron.service",
        "systemd-journald.service",
        "systemd-logind.service",
        "dbus.service",
    )
    available = set()
    try:
        proc = subprocess.run(
            ["systemctl", "list-units", "--type=service", "--no-legend", "--no-pager"],
            capture_output=True,
            text=True,
            timeout=10,
        )
        for line in proc.stdout.splitlines():
            parts = line.split()
            if parts and parts[0].endswith(".service"):
                available.add(parts[0])
    except (OSError, subprocess.TimeoutExpired):
        pass
    for candidate in candidates:
        if candidate in available:
            return candidate
    return candidates[0]


def _container_name():
    if not tools.docker_available():
        return None
    try:
        proc = subprocess.run(
            ["docker", "ps", "-a", "--format", "{{.Names}}"],
            capture_output=True,
            text=True,
            timeout=10,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    names = [line.strip() for line in proc.stdout.splitlines() if line.strip()]
    return names[0] if names else None


def check_python():
    version = "{}.{}.{}".format(*sys.version_info[:3])
    if sys.version_info >= (3, 11):
        return Result("python", PASS, version + " (3.11+ required)")
    return Result("python", FAIL, version + " is too old, need 3.11+")


def check_platform():
    if os.path.exists("/run/systemd/system"):
        return Result("systemd", PASS, "/run/systemd/system present")
    if shutil.which("systemctl"):
        return Result("systemd", WARN, "systemctl found but systemd is not running")
    return Result("systemd", FAIL, "no systemd, this project targets systemd hosts")


def check_ollama():
    try:
        import ollama
    except ImportError:
        return Result(
            "ollama client",
            WARN,
            "not installed, run pip install -r requirements.txt",
        )

    try:
        client = ollama.Client(host=config.OLLAMA_HOST)
        listing = client.list()
    except Exception as exc:
        return Result("ollama server", FAIL, "unreachable: {}".format(exc))

    names = []
    for entry in listing.get("models", []):
        name = entry.get("model") or entry.get("name") or ""
        names.append(name)

    result = Result("ollama server", PASS, "reachable, {} model(s)".format(len(names)))

    wanted = config.MODEL
    if any(name.split(":")[0] == wanted.split(":")[0] for name in names):
        return Result("model " + wanted, PASS, "present")
    if names:
        return Result(
            "model " + wanted,
            FAIL,
            "not pulled, available: {}. run: ollama pull {}".format(
                ", ".join(names), wanted
            ),
        )
    return Result(
        "model " + wanted,
        FAIL,
        "no models on the server, run: ollama pull {}".format(wanted),
    )


def check_prompt_and_schema():
    from . import agent, report

    prompt = agent.system_prompt(config.MAX_STEPS)
    missing = [h for h in report.HEADINGS if h not in prompt]
    if missing:
        return Result("system prompt", FAIL, "missing headings: {}".format(missing))
    return Result(
        "system prompt",
        PASS,
        "{} chars, all {} report headings".format(len(prompt), len(report.HEADINGS)),
    )


def check_schemas():
    schemas = tools.schemas()
    if len(schemas) != len(tools.REGISTRY):
        return Result("tool schemas", FAIL, "schema count mismatch")
    bad = [s["function"]["name"] for s in schemas if not s["function"]["parameters"]]
    if bad:
        return Result("tool schemas", FAIL, "no parameters for: {}".format(bad))
    return Result(
        "tool schemas",
        PASS,
        "{} tools, all with JSON schemas".format(len(schemas)),
    )


def check_read_only():
    writable = [n for n, s in tools.REGISTRY.items() if not s.read_only]
    if writable:
        return Result("allowlist is read-only", FAIL, "writable: {}".format(writable))
    return Result(
        "allowlist is read-only", PASS, "all {} tools read-only".format(len(tools.REGISTRY))
    )


def check_injection():
    """Try the three classic escapes. All three must be rejected."""
    problems = []
    try:
        tools.run_tool(tools.REGISTRY["service_status"], {"unit": "sshd; rm -rf /"})
        problems.append("unit injection executed")
    except safety.SafetyError:
        pass

    try:
        tools.run_tool(
            tools.REGISTRY["listening_ports"], {"port": "8080; whoami"}
        )
        problems.append("port injection executed")
    except safety.SafetyError:
        pass

    try:
        tools.run_tool(
            tools.REGISTRY["read_log_file"], {"path": "/var/log/../../etc/shadow"}
        )
        problems.append("path traversal executed")
    except safety.SafetyError:
        pass

    if problems:
        return Result("injection blocked", FAIL, "; ".join(problems))
    return Result(
        "injection blocked",
        PASS,
        "unit, port and path traversal all rejected",
    )


def check_unknown_tool():
    from . import agent as agent_module

    called = []

    def confirm(command):
        called.append(command)
        return True

    diag = agent_module.Agent(
        client=object(), model="doctor", transcript_dir=None, confirm=confirm
    )
    output, _redacted, blocked = diag._execute("rm", {"path": "/"})
    if not blocked or "not in the read-only allowlist" not in output:
        return Result("unknown tool denied", FAIL, "unknown tool was not blocked")
    if called:
        return Result("unknown tool denied", FAIL, "confirm gate fired too early")
    return Result(
        "unknown tool denied", PASS, "'rm' blocked before any process started"
    )


def check_redaction():
    sample = (
        "db password=hunter2 host=10.0.0.5\n"
        "Authorization: Bearer abcdefghijklmnopqrstuvwxyz\n"
        "-----BEGIN RSA PRIVATE KEY-----\nMIIEow\n-----END RSA PRIVATE KEY-----\n"
        "dsn postgres://app:s3cr3t@db.internal:5432/main\n"
    )
    cleaned, count = safety.redact(sample)
    leaked = [
        marker
        for marker in ("hunter2", "abcdefghijklmnopqrstuvwxyz", "MIIEow", "s3cr3t")
        if marker in cleaned
    ]
    if leaked:
        return Result("redaction", FAIL, "leaked: {}".format(leaked))
    if count < 4:
        return Result("redaction", FAIL, "only {} redactions from 4 secrets".format(count))
    return Result("redaction", PASS, "{} secrets redacted".format(count))


def check_truncation():
    body = "HEAD" + ("x" * 20000) + "TAIL"
    out = safety.truncate(body, config.MAX_OUTPUT_CHARS)
    ok = "HEAD" in out and "TAIL" in out and len(out) <= config.MAX_OUTPUT_CHARS + 60
    if not ok:
        return Result("truncation", FAIL, "head or tail lost, len={}".format(len(out)))
    return Result(
        "truncation",
        PASS,
        "20000 chars -> {} keeping head and tail".format(len(out)),
    )


def check_every_tool():
    unit = _probe_units()
    container = _container_name()
    with tempfile.TemporaryDirectory(prefix="whybroke-doctor-") as tmp:
        sample = os.path.join(tmp, "sample.log")
        with open(sample, "w") as handle:
            handle.write("line one\n") * 50
        saved = config.LOG_DIRS
        config.LOG_DIRS = saved + (tmp,)
        try:
            plan = [
                ("service_status", {"unit": unit}),
                ("unit_logs", {"unit": unit, "lines": 5}),
                ("error_logs", {"since": "1h", "lines": 5}),
                ("kernel_logs", {"lines": 5}),
                ("disk_usage", {}),
                ("memory_usage", {}),
                ("system_load", {}),
                ("top_processes", {"limit": 5}),
                ("listening_ports", {"port": 22}),
                ("docker_containers", {}),
                ("read_log_file", {"path": sample, "tail_lines": 5}),
            ]
            if container:
                plan.append(("container_logs", {"container": container, "lines": 5}))

            failures = []
            executed = 0
            for name, args in plan:
                try:
                    output, _ = tools.run_tool(tools.REGISTRY[name], args)
                except safety.SafetyError as exc:
                    failures.append("{} rejected its own args: {}".format(name, exc))
                    continue
                except Exception as exc:
                    failures.append("{} raised {}".format(name, exc))
                    continue
                if not output:
                    failures.append("{} returned nothing".format(name))
                    continue
                if len(output) > config.MAX_OUTPUT_CHARS + 60:
                    failures.append("{} returned {} chars, over the cap".format(name, len(output)))
                    continue
                executed += 1
        finally:
            config.LOG_DIRS = saved

    detail = "{}/{} tools executed live".format(executed, len(plan))
    if container:
        detail += ", including docker container '{}'".format(container)
    if failures:
        return Result("tools execute live", FAIL, "{}: {}".format(detail, failures))
    return Result("tools execute live", PASS, detail)


def check_timeout():
    text = tools.run_argv(["sleep", "30"], 1)
    if "TOOL TIMEOUT" in text:
        return Result("timeout enforced", PASS, "sleep 30 killed after 1s")
    return Result("timeout enforced", FAIL, "no timeout fired: {}".format(text))


def check_transcript_dir():
    try:
        os.makedirs(config.TRANSCRIPT_DIR, exist_ok=True)
        probe = os.path.join(config.TRANSCRIPT_DIR, ".doctor-probe")
        with open(probe, "w") as handle:
            handle.write("x")
        os.remove(probe)
    except OSError as exc:
        return Result("transcript dir", WARN, "{} not writable: {}".format(
            config.TRANSCRIPT_DIR, exc
        ))
    return Result("transcript dir", PASS, config.TRANSCRIPT_DIR)


CHECKS = (
    check_python,
    check_platform,
    check_ollama,
    check_prompt_and_schema,
    check_schemas,
    check_read_only,
    check_injection,
    check_unknown_tool,
    check_redaction,
    check_truncation,
    check_every_tool,
    check_timeout,
    check_transcript_dir,
)


def run(stream=None):
    stream = stream or sys.stdout
    results = []
    for check in CHECKS:
        try:
            results.append(check())
        except Exception as exc:
            results.append(Result(check.__name__.replace("check_", ""), FAIL, repr(exc)))

    print("whybroke doctor", file=stream)
    print("model tag: {}  step cap: {}".format(config.MODEL, config.MAX_STEPS), file=stream)
    print("", file=stream)
    for result in results:
        print(result.line(), file=stream)

    failures = [r for r in results if r.status == FAIL]
    warnings = [r for r in results if r.status == WARN]
    print("", file=stream)
    print(
        "{} passed, {} warnings, {} failed".format(
            len([r for r in results if r.status == PASS]),
            len(warnings),
            len(failures),
        ),
        file=stream,
    )
    if failures:
        print("", file=stream)
        for result in failures:
            print("  fix: {}".format(result.detail), file=stream)
    elif warnings:
        print(
            "warnings do not block the safety layer, only the model run.",
            file=stream,
        )
    return 1 if failures else 0